#!/usr/bin/env python3
"""
NRFI Backtest
==============
Closes the loop on predictions:
  1. Fetch actual 1st-inning outcomes for any predictions that don't yet have one
  2. Cache outcomes in outcomes.csv (append-only, joined to predictions on game_pk)
  3. Compute calibration metrics: hit rate by tier, decile buckets, Brier score
  4. Compute per-component utility: how much each adjustment actually correlates
     with real NRFI outcomes (so we can identify dead-weight signals)

A prediction is a "hit" when the model said NRFI (high score) AND no run was
scored in the 1st by either side. Since the model outputs a 0-100 score, we
treat score/100 as a (very rough) probability for Brier scoring — this is not
calibrated yet, but the relative comparison across thresholds is still useful.

Usage:
    python backtest.py                # update outcomes + print full report
    python backtest.py --update-only  # just fetch new outcomes, no report
    python backtest.py --report-only  # skip fetching, just report on what's there
"""

import csv
import os
import sys
import math
from datetime import datetime, date
from collections import defaultdict
import requests

MLB_BASE = "https://statsapi.mlb.com/api/v1"

OUTCOMES_COLUMNS = [
    "game_date",          # YYYY-MM-DD slate date (for human-readable ordering)
    "game_pk",
    "matchup",            # e.g. "LAA @ CIN" — away @ home
    "fetched_at",
    "game_status",        # Final / Postponed / In Progress / Scheduled
    "away_runs_1st",
    "home_runs_1st",
    "nrfi_actual",        # 1 = no run in 1st, 0 = at least one run, blank = unknown
    "final_away_runs",
    "final_home_runs",
    # ---- F5 actuals ----
    "away_runs_f5",       # away runs scored in innings 1-5 combined
    "home_runs_f5",       # home runs scored in innings 1-5 combined
    "f5_total_actual",    # total runs (away + home) innings 1-5
    "f5_ml_winner_side",  # "away" / "home" / "tie" — which side led after 5
    "f5_innings_complete",# 1 if both sides completed 5 innings, 0 otherwise
]

# Components we'll evaluate for predictive utility.
COMPONENT_COLUMNS = [
    "pitcher_component",
    "lineup_component",
    "fi_adj",
    "bvp_adj",
    "platoon_adj",
    "streak_adj",
    "park_adj",
    "weather_adj",
    "team_tendency_adj",
]


# ---------------------------------------------------------------------------
# OUTCOME FETCHING
# ---------------------------------------------------------------------------
def fetch_outcome(game_pk: str) -> dict:
    """
    Pull the linescore for a single game and extract the 1st inning result.
    Returns a dict matching OUTCOMES_COLUMNS, or None if the game isn't final.
    """
    if not game_pk:
        return None
    try:
        r = requests.get(f"{MLB_BASE}/game/{game_pk}/linescore", timeout=10)
        r.raise_for_status()
        ls = r.json()
    except Exception as e:
        print(f"  ! Could not fetch linescore for {game_pk}: {e}")
        return None

    innings = ls.get("innings", []) or []
    if not innings:
        # No innings yet — game hasn't started or no data
        return {
            "game_pk": game_pk,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "game_status": "No Linescore",
            "away_runs_1st": "",
            "home_runs_1st": "",
            "nrfi_actual": "",
            "final_away_runs": "",
            "final_home_runs": "",
            "away_runs_f5": "",
            "home_runs_f5": "",
            "f5_total_actual": "",
            "f5_ml_winner_side": "",
            "f5_innings_complete": 0,
        }

    first = innings[0]
    away_1st = (first.get("away") or {}).get("runs")
    home_1st = (first.get("home") or {}).get("runs")

    # We need BOTH sides of the 1st to be played to call NRFI.
    # If the game ended in walk-off in the 9th, both halves of 1st are still complete.
    if away_1st is None or home_1st is None:
        return {
            "game_pk": game_pk,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "game_status": "Incomplete 1st",
            "away_runs_1st": away_1st if away_1st is not None else "",
            "home_runs_1st": home_1st if home_1st is not None else "",
            "nrfi_actual": "",
            "final_away_runs": "",
            "final_home_runs": "",
            "away_runs_f5": "",
            "home_runs_f5": "",
            "f5_total_actual": "",
            "f5_ml_winner_side": "",
            "f5_innings_complete": 0,
        }

    nrfi_actual = 1 if (away_1st == 0 and home_1st == 0) else 0

    teams = ls.get("teams", {}) or {}
    final_away = (teams.get("away") or {}).get("runs", "")
    final_home = (teams.get("home") or {}).get("runs", "")

    # ------------------------------------------------------------------
    # F5 actuals: sum away and home runs across innings 1 through 5.
    # Both sides must have completed the inning for it to count.
    # The home team may not have batted in the bottom of the 5th if the
    # game was called or ended via walk-off before that half-inning, but
    # in practice F5 bets settle when both teams complete 5 innings.
    # ------------------------------------------------------------------
    away_f5 = 0
    home_f5 = 0
    f5_complete = 0  # 1 if both teams completed all 5 innings

    # innings list is 0-indexed; innings[0] = 1st inning
    f5_innings = innings[:5]  # up to 5 innings
    both_completed = 0
    if len(f5_innings) >= 5:
        valid = True
        for inn in f5_innings:
            a = (inn.get("away") or {}).get("runs")
            h = (inn.get("home") or {}).get("runs")
            if a is None or h is None:
                valid = False
                break
            away_f5 += a
            home_f5 += h
        if valid:
            both_completed = 1

    if both_completed:
        f5_total = away_f5 + home_f5
        if away_f5 > home_f5:
            f5_winner = "away"
        elif home_f5 > away_f5:
            f5_winner = "home"
        else:
            f5_winner = "tie"
    else:
        away_f5 = ""
        home_f5 = ""
        f5_total = ""
        f5_winner = ""

    return {
        "game_pk": game_pk,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "game_status": "Final",
        "away_runs_1st": away_1st,
        "home_runs_1st": home_1st,
        "nrfi_actual": nrfi_actual,
        "final_away_runs": final_away,
        "final_home_runs": final_home,
        "away_runs_f5": away_f5,
        "home_runs_f5": home_f5,
        "f5_total_actual": f5_total,
        "f5_ml_winner_side": f5_winner,
        "f5_innings_complete": both_completed,
    }


def load_outcomes(csv_path: str) -> dict:
    """Load existing outcomes keyed by game_pk."""
    if not os.path.exists(csv_path):
        return {}
    out = {}
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out[str(row.get("game_pk", ""))] = row
    return out


def save_outcomes(csv_path: str, outcomes: dict):
    """Rewrite the outcomes file from scratch (small file, simpler than dedup append)."""
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTCOMES_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        # Sort by date first, then game_pk so each day's games are grouped together.
        def sort_key(row):
            date_str = row.get("game_date") or ""
            try:
                gpk_int = int(row.get("game_pk", 0))
            except (TypeError, ValueError):
                gpk_int = 0
            return (date_str, gpk_int)
        for row in sorted(outcomes.values(), key=sort_key):
            writer.writerow(row)


def _normalize_date(raw: str) -> str:
    """
    Convert a prediction_date string (possibly M/D/YY or YYYY-MM-DD) to
    YYYY-MM-DD so outcomes sort chronologically as plain strings.
    """
    raw = raw.strip()
    if not raw:
        return ""
    # Already in ISO format
    if len(raw) == 10 and raw[4] == "-":
        return raw
    # M/D/YY or M/D/YYYY (e.g. "4/10/26" or "4/10/2026")
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw  # return as-is if unrecognised


def update_outcomes(predictions_csv: str, outcomes_csv: str) -> dict:
    """
    For every prediction whose date is on/before today and whose outcome is
    missing or non-final, fetch the linescore and update the outcomes file.
    """
    if not os.path.exists(predictions_csv):
        print(f"No predictions log found at {predictions_csv}")
        return {"checked": 0, "updated": 0}

    outcomes = load_outcomes(outcomes_csv)
    today_iso = date.today().isoformat()

    # Build lookup maps from predictions: game_pk → ISO date and matchup.
    # Used to backfill columns on existing outcome rows added before these
    # fields existed, without needing to re-fetch from the API.
    gpk_to_date: dict[str, str] = {}
    gpk_to_matchup: dict[str, str] = {}
    with open(predictions_csv, "r", newline="") as f:
        for row in csv.DictReader(f):
            gpk = str(row.get("game_pk", ""))
            pdate_iso = _normalize_date(row.get("prediction_date", ""))
            matchup = row.get("matchup", "")
            if gpk:
                if pdate_iso:
                    gpk_to_date[gpk] = pdate_iso
                if matchup:
                    gpk_to_matchup[gpk] = matchup

    # Backfill game_date and matchup on existing outcomes that are missing them.
    for gpk, outcome_row in outcomes.items():
        if not outcome_row.get("game_date") and gpk in gpk_to_date:
            outcome_row["game_date"] = gpk_to_date[gpk]
        if not outcome_row.get("matchup") and gpk in gpk_to_matchup:
            outcome_row["matchup"] = gpk_to_matchup[gpk]

    checked = 0
    updated = 0
    with open(predictions_csv, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            pdate_raw = row.get("prediction_date", "")
            pdate_iso = _normalize_date(pdate_raw)
            gpk = str(row.get("game_pk", ""))
            if not gpk or not pdate_iso:
                continue
            # Only fetch games whose slate date is today or earlier
            if pdate_iso > today_iso:
                continue
            existing = outcomes.get(gpk)
            # Skip if we already have a Final outcome WITH F5 data populated.
            # If f5_innings_complete is missing/empty, re-fetch so the new
            # F5 columns get backfilled for older outcome rows.
            if existing and existing.get("game_status") == "Final":
                if str(existing.get("f5_innings_complete", "")).strip() in ("0", "1"):
                    continue  # F5 already resolved (0 = not enough innings, 1 = complete)
            checked += 1
            outcome = fetch_outcome(gpk)
            if outcome is None:
                continue
            outcome["game_date"] = pdate_iso              # stamp the date for ordering
            outcome["matchup"] = gpk_to_matchup.get(gpk, "")  # stamp the matchup
            outcomes[gpk] = outcome
            if outcome.get("game_status") == "Final":
                updated += 1

    save_outcomes(outcomes_csv, outcomes)
    return {"checked": checked, "updated": updated, "total_outcomes": len(outcomes)}


# ---------------------------------------------------------------------------
# REPORTING
# ---------------------------------------------------------------------------
def load_joined(predictions_csv: str, outcomes_csv: str) -> list:
    """
    Inner-join predictions with outcomes on game_pk, returning only rows where
    we have a Final outcome (i.e., a 0/1 nrfi_actual to score against).
    F5 outcome fields are included when available (f5_innings_complete == 1).
    """
    outcomes = load_outcomes(outcomes_csv)
    if not os.path.exists(predictions_csv):
        return []

    rows = []
    with open(predictions_csv, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            gpk = str(row.get("game_pk", ""))
            o = outcomes.get(gpk)
            if not o or o.get("nrfi_actual") in ("", None):
                continue
            joined = dict(row)
            joined.update({
                "nrfi_actual": int(o["nrfi_actual"]),
                "game_status": o.get("game_status", ""),
                "away_runs_1st": o.get("away_runs_1st", ""),
                "home_runs_1st": o.get("home_runs_1st", ""),
                # F5 actuals — present when f5_innings_complete == 1
                "away_runs_f5": o.get("away_runs_f5", ""),
                "home_runs_f5": o.get("home_runs_f5", ""),
                "f5_total_actual": o.get("f5_total_actual", ""),
                "f5_ml_winner_side": o.get("f5_ml_winner_side", ""),
                "f5_innings_complete": int(o.get("f5_innings_complete", 0) or 0),
            })
            rows.append(joined)
    return rows


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def hit_rate_by_tier(joined: list) -> dict:
    buckets = defaultdict(lambda: {"n": 0, "hits": 0})
    for r in joined:
        tier = r.get("tier", "?")
        buckets[tier]["n"] += 1
        buckets[tier]["hits"] += r["nrfi_actual"]
    out = {}
    for tier, b in buckets.items():
        rate = (b["hits"] / b["n"]) if b["n"] else 0
        out[tier] = {"n": b["n"], "hits": b["hits"], "rate": rate}
    return out


def calibration_buckets(joined: list, n_buckets: int = 10) -> list:
    """
    Bucket predictions into deciles by score and compute the actual NRFI rate
    in each. A well-calibrated model should show monotonically rising rates.
    """
    if not joined:
        return []
    sorted_rows = sorted(joined, key=lambda r: _safe_float(r.get("nrfi_score")))
    n = len(sorted_rows)
    bucket_size = max(1, n // n_buckets)

    buckets = []
    i = 0
    bucket_idx = 0
    while i < n:
        chunk = sorted_rows[i:i + bucket_size]
        if not chunk:
            break
        bucket_idx += 1
        scores = [_safe_float(r.get("nrfi_score")) for r in chunk]
        actuals = [r["nrfi_actual"] for r in chunk]
        buckets.append({
            "bucket": bucket_idx,
            "n": len(chunk),
            "score_min": min(scores),
            "score_max": max(scores),
            "score_mean": sum(scores) / len(scores),
            "actual_rate": sum(actuals) / len(actuals),
        })
        i += bucket_size
    return buckets


def brier_score(joined: list) -> float:
    """
    Brier score using nrfi_score/100 as a (rough) probability.
    Lower is better. A constant prediction of the base rate is the floor
    we want to beat.
    """
    if not joined:
        return float("nan")
    se = 0.0
    for r in joined:
        p = _safe_float(r.get("nrfi_score")) / 100.0
        actual = r["nrfi_actual"]
        se += (p - actual) ** 2
    return se / len(joined)


def base_rate(joined: list) -> float:
    if not joined:
        return float("nan")
    return sum(r["nrfi_actual"] for r in joined) / len(joined)


def pearson_correlation(xs: list, ys: list) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    denx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    deny = math.sqrt(sum((y - my) ** 2 for y in ys))
    if denx == 0 or deny == 0:
        return float("nan")
    return num / (denx * deny)


def component_utility(joined: list) -> list:
    """
    For each component column, compute the Pearson correlation between the
    component value and the actual NRFI outcome (0/1).
    Positive correlation = the component pushes scores in a useful direction.
    Near-zero correlation = the component is noise.
    Negative correlation = the component is actively hurting predictions.
    """
    if not joined:
        return []
    out = []
    actuals = [float(r["nrfi_actual"]) for r in joined]
    for col in COMPONENT_COLUMNS:
        vals = [_safe_float(r.get(col)) for r in joined]
        # Skip components that never moved (all zeros)
        if all(v == 0 for v in vals):
            out.append({"component": col, "n": len(vals), "corr": float("nan"),
                        "note": "all zero — never fired"})
            continue
        corr = pearson_correlation(vals, actuals)
        out.append({"component": col, "n": len(vals), "corr": corr, "note": ""})
    out.sort(key=lambda r: -r["corr"] if not math.isnan(r["corr"]) else 99)
    return out


# ---------------------------------------------------------------------------
# F5 REPORTING HELPERS
# ---------------------------------------------------------------------------

def _f5_pick_side(row: dict) -> str:
    """
    Determine whether the F5 ML pick is for the 'away' team, 'home' team,
    or 'pick' (no actionable call).

    The pick is stored as a team abbreviation (e.g. "NYY") and the matchup
    is stored as "NYY @ TB" (away @ home).  We parse the matchup to classify.
    """
    pick = str(row.get("f5_ml_pick", "")).strip()
    if not pick or pick.upper() in ("PICK", ""):
        return "pick"
    matchup = str(row.get("matchup", ""))
    if "@" in matchup:
        parts = matchup.split("@", 1)
        away_abbr = parts[0].strip()
        home_abbr = parts[1].strip()
        if pick == away_abbr:
            return "away"
        if pick == home_abbr:
            return "home"
    return "unknown"


def f5_ml_results(joined: list) -> dict:
    """
    F5 ML hit rate by confidence tier.

    A hit is when the model's pick side matches the actual winner side.
    Ties (f5_ml_winner_side == 'tie') are excluded — the bet pushes.
    PICK'em games (no directional call) are also excluded.
    """
    # Only use rows with completed 5 innings AND a directional pick
    buckets = defaultdict(lambda: {"n": 0, "hits": 0, "pushes": 0})
    for r in joined:
        if not r.get("f5_innings_complete"):
            continue
        winner = r.get("f5_ml_winner_side", "")
        pick_side = _f5_pick_side(r)
        confidence = r.get("f5_ml_confidence", "?") or "?"
        if not winner or pick_side in ("pick", "unknown"):
            continue
        if winner == "tie":
            buckets[confidence]["pushes"] += 1
            continue
        buckets[confidence]["n"] += 1
        if pick_side == winner:
            buckets[confidence]["hits"] += 1
    out = {}
    for conf, b in buckets.items():
        rate = (b["hits"] / b["n"]) if b["n"] else 0
        out[conf] = {"n": b["n"], "hits": b["hits"], "pushes": b["pushes"], "rate": rate}
    return out


def f5_spread_results(joined: list) -> dict:
    """
    F5 Spread hit rate by confidence tier.

    Spread label examples: "NYY -0.5", "NYY -1.5", "No spread play".
    A -0.5 spread covers when the pick side wins by any margin (>=1 run).
    A -1.5 spread covers when the pick side wins by 2+ runs.
    Ties are pushes for -0.5 (technically impossible on half-run lines, but
    treated as pushes here); -1.5 pushes if the pick wins by exactly 1.
    """
    buckets = defaultdict(lambda: {"n": 0, "hits": 0, "pushes": 0})
    for r in joined:
        if not r.get("f5_innings_complete"):
            continue
        label = str(r.get("f5_spread_label", "") or "").strip()
        if not label or label == "No spread play":
            continue
        confidence = r.get("f5_spread_confidence", "?") or "?"
        # Parse the line from the label ("NYY -0.5" → -0.5)
        line = None
        if "-1.5" in label:
            line = -1.5
        elif "-0.5" in label:
            line = -0.5
        if line is None:
            continue

        # Determine pick side from the label
        # The label is "<abbr> -0.5" or "<abbr> -1.5"
        pick_abbr = label.split()[0] if label.split() else ""
        pick_side = "unknown"
        matchup = str(r.get("matchup", ""))
        if "@" in matchup:
            parts = matchup.split("@", 1)
            if pick_abbr == parts[0].strip():
                pick_side = "away"
            elif pick_abbr == parts[1].strip():
                pick_side = "home"

        if pick_side == "unknown":
            continue

        winner = r.get("f5_ml_winner_side", "")
        try:
            away_f5 = int(r.get("away_runs_f5", 0) or 0)
            home_f5 = int(r.get("home_runs_f5", 0) or 0)
        except (TypeError, ValueError):
            continue

        margin = away_f5 - home_f5  # positive = away leads

        if pick_side == "away":
            pick_margin = margin
        else:
            pick_margin = -margin  # positive = picked team leads

        buckets[confidence]["n"] += 1
        if line == -0.5:
            if pick_margin >= 1:
                buckets[confidence]["hits"] += 1
            elif pick_margin == 0:
                buckets[confidence]["pushes"] += 1
                buckets[confidence]["n"] -= 1  # push doesn't count toward n
        elif line == -1.5:
            if pick_margin >= 2:
                buckets[confidence]["hits"] += 1
            elif pick_margin == 1:
                buckets[confidence]["pushes"] += 1
                buckets[confidence]["n"] -= 1
    out = {}
    for conf, b in buckets.items():
        rate = (b["hits"] / b["n"]) if b["n"] else 0
        out[conf] = {"n": b["n"], "hits": b["hits"], "pushes": b["pushes"], "rate": rate}
    return out


def f5_total_results(joined: list) -> dict:
    """
    F5 Total hit rate by lean direction (OVER / UNDER) and confidence.

    Uses f5_total_lean (OVER/UNDER/PUSH), f5_total_line (the line), and
    f5_total_actual (actual runs scored in 5 innings) to evaluate.
    """
    # Group by (lean, confidence)
    buckets = defaultdict(lambda: {"n": 0, "hits": 0, "pushes": 0})
    for r in joined:
        if not r.get("f5_innings_complete"):
            continue
        lean = str(r.get("f5_total_lean", "") or "").strip().upper()
        if lean in ("", "PUSH"):
            continue
        confidence = r.get("f5_total_confidence", "?") or "?"
        try:
            actual = float(r.get("f5_total_actual", "") or "")
            line = float(r.get("f5_total_line", "") or "")
        except (TypeError, ValueError):
            continue

        key = f"{lean} ({confidence})"
        buckets[key]["n"] += 1
        if lean == "OVER":
            if actual > line:
                buckets[key]["hits"] += 1
            elif actual == line:
                buckets[key]["pushes"] += 1
                buckets[key]["n"] -= 1
        elif lean == "UNDER":
            if actual < line:
                buckets[key]["hits"] += 1
            elif actual == line:
                buckets[key]["pushes"] += 1
                buckets[key]["n"] -= 1
    out = {}
    for key, b in buckets.items():
        rate = (b["hits"] / b["n"]) if b["n"] else 0
        out[key] = {"n": b["n"], "hits": b["hits"], "pushes": b["pushes"], "rate": rate}
    return out


def f5_total_calibration(joined: list) -> list:
    """
    Compare projected F5 total vs actual total: how biased is the model?
    Returns a sorted list of (projected_bucket, avg_actual, n) so we can see
    where the model over- or under-projects.
    """
    rows_with_data = []
    for r in joined:
        if not r.get("f5_innings_complete"):
            continue
        try:
            proj = float(r.get("f5_total_projected", "") or "")
            actual = float(r.get("f5_total_actual", "") or "")
        except (TypeError, ValueError):
            continue
        rows_with_data.append((proj, actual))

    if not rows_with_data:
        return []

    # Bucket by projected total (round to nearest 0.5)
    bucket_map = defaultdict(list)
    for proj, actual in rows_with_data:
        bucket = round(proj * 2) / 2  # nearest 0.5
        bucket_map[bucket].append(actual)

    result = []
    for bucket in sorted(bucket_map.keys()):
        actuals = bucket_map[bucket]
        result.append({
            "proj_bucket": bucket,
            "n": len(actuals),
            "avg_actual": round(sum(actuals) / len(actuals), 2),
            "bias": round(sum(actuals) / len(actuals) - bucket, 2),
        })
    return result


# ---------------------------------------------------------------------------
# REPORT PRINTING
# ---------------------------------------------------------------------------
def print_report(joined: list):
    print(f"\n{'=' * 60}")
    print(f"  NRFI BACKTEST REPORT")
    print(f"{'=' * 60}\n")

    if not joined:
        print("No completed games to score yet.")
        print("Run the daily pipeline for a few days, then try again.\n")
        return

    n = len(joined)
    br = base_rate(joined)
    bs = brier_score(joined)
    print(f"Sample: {n} completed games")
    print(f"Base rate (actual NRFI %): {br:.1%}")
    print(f"Brier score (lower=better): {bs:.4f}")
    print(f"Brier of constant base-rate predictor: {br * (1 - br):.4f}")
    print()

    if n < 30:
        print("⚠️  Sample size is small. Treat the numbers below as directional, not conclusive.")
        print("    Wait for ~50+ completed games before drawing strong conclusions.\n")

    # Hit rate by tier
    print("Hit rate by tier:")
    print(f"  {'Tier':<10} {'N':>5} {'Hits':>6} {'Rate':>8}")
    print(f"  {'-' * 32}")
    for tier in ("STRONG", "LEAN", "TOSS-UP", "FADE"):
        bucket = hit_rate_by_tier(joined).get(tier)
        if bucket:
            print(f"  {tier:<10} {bucket['n']:>5} {bucket['hits']:>6} {bucket['rate']:>7.1%}")
        else:
            print(f"  {tier:<10} {'-':>5} {'-':>6} {'-':>8}")
    print()

    # Calibration deciles
    print("Calibration (decile buckets, low score → high score):")
    print(f"  {'Bucket':>6} {'N':>4} {'Score range':>14} {'Mean score':>11} {'Actual NRFI':>12}")
    print(f"  {'-' * 50}")
    for b in calibration_buckets(joined):
        print(f"  {b['bucket']:>6} {b['n']:>4} {b['score_min']:>5.1f}-{b['score_max']:<5.1f}     "
              f"{b['score_mean']:>10.1f}  {b['actual_rate']:>11.1%}")
    print("  → Well-calibrated = actual NRFI rate climbs monotonically.")
    print()

    # Component utility
    print("Component utility (Pearson correlation with NRFI outcome):")
    print(f"  {'Component':<22} {'N':>5} {'Corr':>8}  Notes")
    print(f"  {'-' * 60}")
    for c in component_utility(joined):
        corr_str = f"{c['corr']:>+8.3f}" if not math.isnan(c["corr"]) else f"{'nan':>8}"
        sig = ""
        if not math.isnan(c["corr"]):
            if c["corr"] >= 0.15:
                sig = " ← strong useful signal"
            elif c["corr"] >= 0.05:
                sig = " ← mild signal"
            elif c["corr"] <= -0.05:
                sig = " ← actively WRONG direction"
            else:
                sig = " ← noise (consider removing)"
        print(f"  {c['component']:<22} {c['n']:>5} {corr_str}{sig}")
    print()
    print("  Reminder: positive correlation means the component value rises when")
    print("  NRFI actually happens — i.e., it's pushing the model in the right direction.")
    print()

    # ------------------------------------------------------------------
    # F5 SECTION
    # ------------------------------------------------------------------
    f5_rows = [r for r in joined if r.get("f5_innings_complete")]
    f5_n = len(f5_rows)

    print(f"{'=' * 60}")
    print(f"  F5 (FIRST 5 INNINGS) BACKTEST")
    print(f"{'=' * 60}\n")

    if f5_n == 0:
        print("No F5-complete games yet (need at least 1 completed game with 5+ innings logged).")
        print("Re-run after a few days of games complete.\n")
    else:
        if f5_n < 30:
            print(f"⚠️  F5 sample: {f5_n} games. Directional only — wait for 50+ before acting.\n")
        else:
            print(f"F5 sample: {f5_n} completed games\n")

        # F5 ML
        ml_results = f5_ml_results(joined)
        if ml_results:
            print("F5 Moneyline hit rate by confidence:")
            print(f"  {'Confidence':<12} {'N':>5} {'Hits':>6} {'Pushes':>8} {'Rate':>8}")
            print(f"  {'-' * 42}")
            for conf in ("STRONG", "MODERATE", "LEAN", "TOSS-UP"):
                b = ml_results.get(conf)
                if b:
                    print(f"  {conf:<12} {b['n']:>5} {b['hits']:>6} {b['pushes']:>8} {b['rate']:>7.1%}")
                else:
                    print(f"  {conf:<12} {'-':>5} {'-':>6} {'-':>8} {'-':>8}")
            print("  (Pushes = tie after 5 inn; excluded from N and rate.)\n")
        else:
            print("  No F5 ML picks with completed outcomes yet.\n")

        # F5 Spread
        spread_results = f5_spread_results(joined)
        if spread_results:
            print("F5 Spread hit rate by confidence:")
            print(f"  {'Confidence':<12} {'N':>5} {'Hits':>6} {'Pushes':>8} {'Rate':>8}")
            print(f"  {'-' * 42}")
            for conf in ("STRONG", "LEAN", "SLIGHT", "TOSS-UP"):
                b = spread_results.get(conf)
                if b:
                    print(f"  {conf:<12} {b['n']:>5} {b['hits']:>6} {b['pushes']:>8} {b['rate']:>7.1%}")
                else:
                    print(f"  {conf:<12} {'-':>5} {'-':>6} {'-':>8} {'-':>8}")
            print("  (Pushes = exact margin on -1.5 line; excluded from N and rate.)\n")
        else:
            print("  No F5 Spread picks with completed outcomes yet.\n")

        # F5 Total
        total_results = f5_total_results(joined)
        if total_results:
            print("F5 Total hit rate by lean & confidence (line 4.5 unless noted):")
            print(f"  {'Lean (Confidence)':<22} {'N':>5} {'Hits':>6} {'Pushes':>8} {'Rate':>8}")
            print(f"  {'-' * 52}")
            for key in sorted(total_results.keys()):
                b = total_results[key]
                print(f"  {key:<22} {b['n']:>5} {b['hits']:>6} {b['pushes']:>8} {b['rate']:>7.1%}")
            print()
        else:
            print("  No F5 Total calls with completed outcomes yet.\n")

        # F5 Total calibration (projection accuracy)
        cal = f5_total_calibration(joined)
        if cal:
            print("F5 Total projection calibration (projected bucket → actual avg):")
            print(f"  {'Proj':>6} {'N':>4} {'Avg Actual':>11} {'Bias':>7}")
            print(f"  {'-' * 32}")
            for row in cal:
                bias_str = f"{row['bias']:>+7.2f}"
                flag = " ← overprojects" if row["bias"] < -0.3 else (" ← underprojects" if row["bias"] > 0.3 else "")
                print(f"  {row['proj_bucket']:>6.1f} {row['n']:>4} {row['avg_actual']:>11.2f} {bias_str}{flag}")
            print("  → Bias = avg_actual − projection. Negative = model overestimates runs.")
            print()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    update_only = "--update-only" in args
    report_only = "--report-only" in args

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(os.path.dirname(script_dir), "output")
    predictions_csv = os.path.join(output_dir, "predictions.csv")
    outcomes_csv = os.path.join(output_dir, "outcomes.csv")

    if not report_only:
        print("Updating outcomes...")
        summary = update_outcomes(predictions_csv, outcomes_csv)
        print(f"Checked {summary['checked']} games, updated {summary['updated']} to Final, "
              f"{summary['total_outcomes']} total outcomes on file.")

    if update_only:
        return

    joined = load_joined(predictions_csv, outcomes_csv)
    print_report(joined)


if __name__ == "__main__":
    main()
