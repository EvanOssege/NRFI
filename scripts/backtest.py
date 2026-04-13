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
import re
from datetime import datetime, date
from collections import defaultdict
from statistics import median
import requests

from f5_analyzer import (
    F5_SIDE_BASE_RUNS,
    F5_PITCHER_SCALE,
    F5_LINEUP_SCALE,
    F5_FI_ADJ_SCALE,
    F5_BVP_ADJ_SCALE,
    F5_PLATOON_ADJ_SCALE,
    F5_STREAK_ADJ_SCALE,
    F5_REST_ADJ_SCALE,
)

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

F5_PITCHER_TIERS = [
    ("ELITE", 70.0, float("inf")),
    ("STRONG", 60.0, 70.0),
    ("AVERAGE", 50.0, 60.0),
    ("WEAK", float("-inf"), 50.0),
]

F5_CAL_MIN_SIDE_SAMPLES = 80
F5_CAL_MIN_TIER_SAMPLES = 12
F5_CAL_ITERATIONS = 4
F5_CAL_MULTIPLIERS = [0.80, 0.90, 0.95, 1.00, 1.05, 1.10, 1.20]

F5_CAL_BOUNDS = {
    "base": (1.50, 3.25),
    "pitcher_scale": (0.20, 1.10),
    "lineup_scale": (0.10, 0.90),
    "fi_scale": (0.00, 0.10),
    "bvp_scale": (0.00, 0.16),
    "platoon_scale": (0.00, 0.16),
    "streak_scale": (0.00, 0.16),
    "rest_scale": (0.00, 0.16),
}


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
            date_str = _normalize_date(row.get("game_date") or "")
            if not (len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-"):
                date_str = "9999-12-31"
            try:
                gpk_int = int(row.get("game_pk", 0))
            except (TypeError, ValueError):
                gpk_int = 10**18
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


def _safe_float_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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

def _f5_token_key(value) -> str:
    """Uppercase token key for robust team/side matching."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _f5_matchup_sides(row: dict) -> tuple[str, str]:
    """Parse matchup into (away_tag, home_tag)."""
    matchup = str(row.get("matchup", "") or "").strip()
    if "@" not in matchup:
        return "", ""
    away_tag, home_tag = matchup.split("@", 1)
    return away_tag.strip(), home_tag.strip()


def _normalize_f5_side(raw_side: str, row: dict) -> str:
    """
    Canonicalize side labels to away/home/tie where possible.

    Supports direct labels ('away', 'home', 'tie'), matchup abbreviations,
    and team full names.
    """
    raw = str(raw_side or "").strip()
    if not raw:
        return "unknown"

    direct = {
        "away": "away",
        "road": "away",
        "visitor": "away",
        "visiting": "away",
        "home": "home",
        "host": "home",
        "tie": "tie",
        "tied": "tie",
        "draw": "tie",
        "push": "tie",
    }
    key = raw.lower()
    if key in direct:
        return direct[key]

    raw_key = _f5_token_key(raw)
    away_tag, home_tag = _f5_matchup_sides(row)
    away_tag_key = _f5_token_key(away_tag)
    home_tag_key = _f5_token_key(home_tag)
    if raw_key and away_tag_key and raw_key == away_tag_key:
        return "away"
    if raw_key and home_tag_key and raw_key == home_tag_key:
        return "home"

    away_name_key = _f5_token_key(row.get("away_team", ""))
    home_name_key = _f5_token_key(row.get("home_team", ""))
    if raw_key and away_name_key and (raw_key == away_name_key or raw_key in away_name_key):
        return "away"
    if raw_key and home_name_key and (raw_key == home_name_key or raw_key in home_name_key):
        return "home"

    return "unknown"


def _f5_pick_side(row: dict) -> str:
    """
    Determine whether the F5 ML pick is for the 'away' team, 'home' team,
    or 'pick' (no actionable call).

    The pick is stored as a team abbreviation (e.g. "NYY") and the matchup
    is stored as "NYY @ TB" (away @ home).  We parse the matchup to classify.
    """
    pick = str(row.get("f5_ml_pick", "")).strip()
    if not pick or pick.upper() in ("PICK", "PICKEM", "PICK'EM", "PK", ""):
        return "pick"
    return _normalize_f5_side(pick, row)


def _f5_winner_side(row: dict) -> str:
    """
    Canonical winner side for F5 ML grading.

    Uses f5_ml_winner_side first; if missing/unparseable, derives from
    away_runs_f5/home_runs_f5.
    """
    winner = _normalize_f5_side(row.get("f5_ml_winner_side", ""), row)
    if winner != "unknown":
        return winner

    try:
        away_f5 = float(row.get("away_runs_f5", "") or "")
        home_f5 = float(row.get("home_runs_f5", "") or "")
    except (TypeError, ValueError):
        return "unknown"

    if away_f5 > home_f5:
        return "away"
    if home_f5 > away_f5:
        return "home"
    return "tie"


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
        winner = _f5_winner_side(r)
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


def _f5_default_coeffs() -> dict:
    return {
        "base": F5_SIDE_BASE_RUNS,
        "pitcher_scale": F5_PITCHER_SCALE,
        "lineup_scale": F5_LINEUP_SCALE,
        "fi_scale": F5_FI_ADJ_SCALE,
        "bvp_scale": F5_BVP_ADJ_SCALE,
        "platoon_scale": F5_PLATOON_ADJ_SCALE,
        "streak_scale": F5_STREAK_ADJ_SCALE,
        "rest_scale": F5_REST_ADJ_SCALE,
    }


def _f5_pitcher_tier(pitcher_score: float) -> str:
    for label, lo, hi in F5_PITCHER_TIERS:
        if lo <= pitcher_score < hi:
            return label
    return "UNKNOWN"


def _build_f5_side_samples(joined: list) -> list:
    """
    Convert completed games into side-level rows for calibration.
    Each game contributes up to two samples:
      - away pitcher vs home lineup (actual runs allowed = home_runs_f5)
      - home pitcher vs away lineup (actual runs allowed = away_runs_f5)
    """
    rows = []
    for r in joined:
        if not r.get("f5_innings_complete"):
            continue

        away_pitcher_score = _safe_float_or_none(r.get("away_pitcher_score"))
        home_pitcher_score = _safe_float_or_none(r.get("home_pitcher_score"))
        home_lineup_threat = _safe_float_or_none(r.get("home_lineup_threat"))
        away_lineup_threat = _safe_float_or_none(r.get("away_lineup_threat"))
        home_runs_f5 = _safe_float_or_none(r.get("home_runs_f5"))
        away_runs_f5 = _safe_float_or_none(r.get("away_runs_f5"))
        if None in (
            away_pitcher_score, home_pitcher_score,
            home_lineup_threat, away_lineup_threat,
            home_runs_f5, away_runs_f5,
        ):
            continue

        fi_adj = _safe_float(r.get("fi_adj")) * 0.5
        bvp_adj = _safe_float(r.get("bvp_adj")) * 0.5
        platoon_adj = _safe_float(r.get("platoon_adj")) * 0.5
        streak_adj = _safe_float(r.get("streak_adj")) * 0.5
        rest_adj = _safe_float(r.get("rest_adj")) * 0.5

        rows.append({
            "tier": _f5_pitcher_tier(away_pitcher_score),
            "pitcher_score": away_pitcher_score,
            "lineup_threat": home_lineup_threat,
            "fi_adj": fi_adj,
            "bvp_adj": bvp_adj,
            "platoon_adj": platoon_adj,
            "streak_adj": streak_adj,
            "rest_adj": rest_adj,
            "actual_runs_allowed": home_runs_f5,
        })
        rows.append({
            "tier": _f5_pitcher_tier(home_pitcher_score),
            "pitcher_score": home_pitcher_score,
            "lineup_threat": away_lineup_threat,
            "fi_adj": fi_adj,
            "bvp_adj": bvp_adj,
            "platoon_adj": platoon_adj,
            "streak_adj": streak_adj,
            "rest_adj": rest_adj,
            "actual_runs_allowed": away_runs_f5,
        })
    return rows


def _project_f5_runs_allowed(sample: dict, coeffs: dict) -> float:
    base = coeffs["base"]
    pitcher_delta = (50 - sample["pitcher_score"]) / 50.0
    runs_from_pitcher = base * (1 + pitcher_delta * coeffs["pitcher_scale"])

    lineup_delta = (sample["lineup_threat"] - 50) / 50.0
    runs_from_lineup = runs_from_pitcher * (1 + lineup_delta * coeffs["lineup_scale"])

    adj_runs = 0.0
    adj_runs -= sample["fi_adj"] * coeffs["fi_scale"]
    adj_runs -= sample["bvp_adj"] * coeffs["bvp_scale"]
    adj_runs -= sample["platoon_adj"] * coeffs["platoon_scale"]
    adj_runs -= sample["streak_adj"] * coeffs["streak_scale"]
    adj_runs -= sample["rest_adj"] * coeffs["rest_scale"]

    return max(0.5, round(runs_from_lineup + adj_runs, 2))


def _tier_median_bias(side_samples: list, coeffs: dict) -> list:
    tier_actual = defaultdict(list)
    tier_proj = defaultdict(list)
    for row in side_samples:
        tier = row["tier"]
        tier_actual[tier].append(row["actual_runs_allowed"])
        tier_proj[tier].append(_project_f5_runs_allowed(row, coeffs))

    out = []
    for tier, _, _ in F5_PITCHER_TIERS:
        actual = tier_actual.get(tier, [])
        proj = tier_proj.get(tier, [])
        n = len(actual)
        if n == 0:
            out.append({
                "tier": tier,
                "n": 0,
                "actual_median": float("nan"),
                "projected_median": float("nan"),
                "bias": float("nan"),
                "eligible": False,
            })
            continue
        actual_med = median(actual)
        proj_med = median(proj)
        out.append({
            "tier": tier,
            "n": n,
            "actual_median": round(actual_med, 2),
            "projected_median": round(proj_med, 2),
            "bias": round(proj_med - actual_med, 2),
            "eligible": n >= F5_CAL_MIN_TIER_SAMPLES,
        })
    return out


def _tier_weighted_abs_bias(tier_rows: list) -> float:
    eligible = [r for r in tier_rows if r.get("eligible")]
    if not eligible:
        return float("inf")
    total_n = sum(r["n"] for r in eligible)
    if total_n == 0:
        return float("inf")
    return sum(abs(r["bias"]) * r["n"] for r in eligible) / total_n


def _search_f5_coefficients(side_samples: list, base_coeffs: dict) -> tuple[dict, float]:
    best = dict(base_coeffs)
    best_loss = _tier_weighted_abs_bias(_tier_median_bias(side_samples, best))

    for _ in range(F5_CAL_ITERATIONS):
        improved = False
        for key, (lo, hi) in F5_CAL_BOUNDS.items():
            current = best[key]
            candidates = {round(max(lo, min(hi, current * m)), 4) for m in F5_CAL_MULTIPLIERS}
            candidates.add(round(max(lo, min(hi, current)), 4))
            local_best = current
            local_loss = best_loss

            for candidate in sorted(candidates):
                trial = dict(best)
                trial[key] = candidate
                loss = _tier_weighted_abs_bias(_tier_median_bias(side_samples, trial))
                if loss + 1e-9 < local_loss:
                    local_loss = loss
                    local_best = candidate

            if local_best != current:
                best[key] = local_best
                best_loss = local_loss
                improved = True
        if not improved:
            break

    return best, best_loss


def f5_coefficient_calibration(joined: list) -> dict:
    side_samples = _build_f5_side_samples(joined)
    if len(side_samples) < F5_CAL_MIN_SIDE_SAMPLES:
        return {
            "ready": False,
            "side_samples": len(side_samples),
            "min_side_samples": F5_CAL_MIN_SIDE_SAMPLES,
        }

    baseline = _f5_default_coeffs()
    before = _tier_median_bias(side_samples, baseline)
    before_loss = _tier_weighted_abs_bias(before)

    tuned, after_loss = _search_f5_coefficients(side_samples, baseline)
    after = _tier_median_bias(side_samples, tuned)

    # No auto-write: output recommendation-only constants for manual update.
    constants_block = {
        "F5_SIDE_BASE_RUNS": round(tuned["base"], 4),
        "F5_PITCHER_SCALE": round(tuned["pitcher_scale"], 4),
        "F5_LINEUP_SCALE": round(tuned["lineup_scale"], 4),
        "F5_FI_ADJ_SCALE": round(tuned["fi_scale"], 4),
        "F5_BVP_ADJ_SCALE": round(tuned["bvp_scale"], 4),
        "F5_PLATOON_ADJ_SCALE": round(tuned["platoon_scale"], 4),
        "F5_STREAK_ADJ_SCALE": round(tuned["streak_scale"], 4),
        "F5_REST_ADJ_SCALE": round(tuned["rest_scale"], 4),
    }

    return {
        "ready": True,
        "side_samples": len(side_samples),
        "before": before,
        "after": after,
        "before_loss": before_loss,
        "after_loss": after_loss,
        "constants_block": constants_block,
    }


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

        # F5 coefficient calibration (recommendation-only; no auto-write)
        f5_cal = f5_coefficient_calibration(joined)
        print("F5 coefficient calibration by pitcher tier (runs allowed through 5 inn):")
        if not f5_cal.get("ready"):
            print(f"  Not enough side samples yet: {f5_cal.get('side_samples', 0)} "
                  f"(need {f5_cal.get('min_side_samples', F5_CAL_MIN_SIDE_SAMPLES)}).")
            print("  Keep logging predictions/outcomes; rerun backtest after more games complete.\n")
        else:
            before_map = {r["tier"]: r for r in f5_cal["before"]}
            after_map = {r["tier"]: r for r in f5_cal["after"]}
            print(f"  Side samples: {f5_cal['side_samples']}")
            print(f"  Objective (weighted abs tier bias): "
                  f"{f5_cal['before_loss']:.3f} → {f5_cal['after_loss']:.3f}")
            print(f"  {'Tier':<9} {'N':>5} {'Actual Med':>11} {'Proj Med (old)':>14} "
                  f"{'Proj Med (new)':>14} {'Bias old':>9} {'Bias new':>9}")
            print(f"  {'-' * 80}")
            for tier, _, _ in F5_PITCHER_TIERS:
                b = before_map.get(tier)
                a = after_map.get(tier)
                if not b or b["n"] == 0:
                    print(f"  {tier:<9} {'-':>5} {'-':>11} {'-':>14} {'-':>14} {'-':>9} {'-':>9}")
                    continue
                actual_med = f"{b['actual_median']:.2f}"
                old_proj = f"{b['projected_median']:.2f}"
                new_proj = f"{a['projected_median']:.2f}" if a else "-"
                old_bias = f"{b['bias']:+.2f}"
                new_bias = f"{a['bias']:+.2f}" if a else "-"
                print(f"  {tier:<9} {b['n']:>5} {actual_med:>11} {old_proj:>14} {new_proj:>14} "
                      f"{old_bias:>9} {new_bias:>9}")

            print("\n  Recommended constants for scripts/f5_analyzer.py:")
            for name, value in f5_cal["constants_block"].items():
                print(f"  {name} = {value}")
            print("  (Recommendation-only output; apply manually after reviewing sample quality.)\n")


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
