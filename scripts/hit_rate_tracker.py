#!/usr/bin/env python3
"""
Hit Rate Tracker
=================
Reads predictions.csv + outcomes.csv, computes cumulative and 7-day rolling
hit rates per market and confidence tier, and generates a self-contained HTML
dashboard with SVG line charts showing how accuracy evolves over time.

Push rules (pushes count as losses — sportsbooks run three-way F5 markets):
  - F5 ML: tie after 5 innings = loss
  - F5 Spread -0.5: win by exactly 0 = loss
  - F5 Spread -1.5: win by exactly 1 = loss
  - F5 Total: actual == line = loss

Usage (called automatically by run_nrfi.py):
    from hit_rate_tracker import generate_hit_rate_dashboard
    generate_hit_rate_dashboard(predictions_csv, outcomes_csv, output_path)
"""

import csv
import json
import os
import re
from collections import defaultdict


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_csv(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Hit determination — pushes = losses
# ---------------------------------------------------------------------------

def _token_key(value):
    """Uppercase token key, preserving alphanumerics."""
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def _parse_matchup_sides(pred):
    """Return (away_tag, home_tag) parsed from matchup like 'STL @ WSH'."""
    matchup = str(pred.get("matchup", "") or "").strip()
    if "@" not in matchup:
        return "", ""
    away_tag, home_tag = matchup.split("@", 1)
    return away_tag.strip(), home_tag.strip()


def _normalize_side(raw_side, pred):
    """
    Canonicalize side labels to 'away'/'home'/'tie' when possible.

    Accepts direct side labels, team abbreviations from matchup, and full team
    names (including compacted forms with punctuation removed).
    """
    raw = str(raw_side or "").strip()
    if not raw:
        return None

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

    raw_key = _token_key(raw)
    away_tag, home_tag = _parse_matchup_sides(pred)
    away_tag_key = _token_key(away_tag)
    home_tag_key = _token_key(home_tag)
    if raw_key and away_tag_key and raw_key == away_tag_key:
        return "away"
    if raw_key and home_tag_key and raw_key == home_tag_key:
        return "home"

    away_name_key = _token_key(pred.get("away_team", ""))
    home_name_key = _token_key(pred.get("home_team", ""))
    if raw_key and away_name_key and (raw_key == away_name_key or raw_key in away_name_key):
        return "away"
    if raw_key and home_name_key and (raw_key == home_name_key or raw_key in home_name_key):
        return "home"

    return None


def _resolve_pick_side(pred):
    """Map F5 ML pick to canonical side label."""
    pick = str(pred.get("f5_ml_pick", "") or "").strip()
    if not pick or pick.upper() in {"PICK", "PICKEM", "PICK'EM", "PK"}:
        return None
    return _normalize_side(pick, pred)


def _winner_from_f5_runs(away_f5, home_f5):
    """Infer winner side from away/home F5 runs when winner-side text is absent."""
    try:
        away = float(away_f5)
        home = float(home_f5)
    except (TypeError, ValueError):
        return None
    if away > home:
        return "away"
    if home > away:
        return "home"
    return "tie"


def _f5_ml_hit(pred, winner_side, away_f5=None, home_f5=None):
    """True = hit, False = loss (including ties). None = skip (no pick)."""
    pick = pred.get("f5_ml_pick", "").strip()
    if not pick:
        return None
    winner = _normalize_side(winner_side, pred)
    if winner is None:
        winner = _winner_from_f5_runs(away_f5, home_f5)
    if winner is None:
        return None
    if winner == "tie":
        return False  # push = loss
    pick_side = _resolve_pick_side(pred)
    if pick_side is None:
        return None
    return pick_side == winner


def _f5_spread_hit(pred, away_f5, home_f5):
    """True = covers, False = miss/push. None = no pick or unparseable."""
    label = pred.get("f5_spread_label", "").strip()
    pick = pred.get("f5_ml_pick", "").strip()
    if not label or not pick:
        return None
    # Parse line from last token, e.g. "NYY -0.5" -> -0.5
    parts = label.split()
    try:
        line = float(parts[-1])
    except (ValueError, IndexError):
        return None
    pick_side = _resolve_pick_side(pred)
    if pick_side is None:
        return None
    if pick_side == "away":
        margin = away_f5 - home_f5
    else:
        margin = home_f5 - away_f5
    # Covers if margin strictly exceeds the spread threshold
    # -0.5: need margin >= 1   → margin > 0.5 ✓
    # -1.5: need margin >= 2   → margin > 1.5 ✓
    # push (margin = 0 or 1 depending on line) falls through to False
    return margin > abs(line)


def _f5_total_hit(pred, actual_total):
    """True = correct side, False = wrong side or push. None = no data."""
    lean = pred.get("f5_total_lean", "").strip()
    line_str = pred.get("f5_total_line", "").strip()
    if not lean or not line_str:
        return None
    try:
        line = float(line_str)
        actual = float(actual_total)
    except (ValueError, TypeError):
        return None
    if lean == "OVER":
        return actual > line  # exact = False (loss)
    if lean == "UNDER":
        return actual < line  # exact = False (loss)
    return None


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_hit_rates(predictions_csv, outcomes_csv):
    """
    Join predictions + outcomes on game_pk and compute per-day cumulative
    hit-rate series for all four markets.

    Returns:
    {
      "nrfi":      { "STRONG": [{date, day_hits, day_n, cum_hits, cum_n, cum_rate}, ...], ... },
      "f5_ml":     { "STRONG": [...], ... },
      "f5_spread": { "STRONG": [...], ... },
      "f5_total":  { "STRONG OVER": [...], ... },
      "summary":   { total_resolved, overall_nrfi_hits, overall_nrfi_n, overall_nrfi_rate, days }
    }
    """
    preds = _load_csv(predictions_csv)
    outcomes = _load_csv(outcomes_csv)
    if not preds or not outcomes:
        return {}

    outcome_map = {
        str(r.get("game_pk", "")).strip(): r
        for r in outcomes
        if str(r.get("game_pk", "")).strip()
    }

    # Inner-join; only Final games count
    joined = []
    for p in preds:
        pk = str(p.get("game_pk", "")).strip()
        o = outcome_map.get(pk)
        if not o or o.get("game_status") != "Final":
            continue
        game_date = o.get("game_date", "").strip()
        if not game_date:
            continue
        joined.append((game_date, p, o))

    if not joined:
        return {}

    # Bucket raw results by (market, tier, date)
    # Structure: market_buckets[tier][date] -> [True/False, ...]
    nrfi_b      = defaultdict(lambda: defaultdict(list))
    f5_ml_b     = defaultdict(lambda: defaultdict(list))
    f5_spread_b = defaultdict(lambda: defaultdict(list))
    f5_total_b  = defaultdict(lambda: defaultdict(list))

    for game_date, p, o in joined:
        # --- NRFI ---
        nrfi_raw = o.get("nrfi_actual", "").strip()
        if nrfi_raw in ("0", "1"):
            tier = p.get("tier", "").strip()
            if tier:
                nrfi_b[tier][game_date].append(nrfi_raw == "1")

        # --- F5 markets (only when both sides completed 5 innings) ---
        if str(o.get("f5_innings_complete", "")).strip() != "1":
            continue
        try:
            away_f5 = float(o.get("away_runs_f5", ""))
            home_f5 = float(o.get("home_runs_f5", ""))
            total_f5 = float(o.get("f5_total_actual", ""))
        except (ValueError, TypeError):
            continue

        winner = o.get("f5_ml_winner_side", "").strip()

        # F5 ML
        ml_conf = p.get("f5_ml_confidence", "").strip()
        if ml_conf:
            h = _f5_ml_hit(p, winner, away_f5, home_f5)
            if h is not None:
                f5_ml_b[ml_conf][game_date].append(h)

        # F5 Spread
        sp_conf = p.get("f5_spread_confidence", "").strip()
        if sp_conf:
            h = _f5_spread_hit(p, away_f5, home_f5)
            if h is not None:
                f5_spread_b[sp_conf][game_date].append(h)

        # F5 Total
        tot_conf = p.get("f5_total_confidence", "").strip()
        tot_lean = p.get("f5_total_lean", "").strip()
        if tot_conf and tot_lean:
            key = f"{tot_conf} {tot_lean}"
            h = _f5_total_hit(p, total_f5)
            if h is not None:
                f5_total_b[key][game_date].append(h)

    def build_series(buckets):
        """Convert bucketed results into sorted cumulative series per tier."""
        result = {}
        for tier, date_hits in buckets.items():
            cum_hits = cum_n = 0
            series = []
            for d in sorted(date_hits):
                hits = date_hits[d]
                day_h = sum(1 for x in hits if x)
                day_n = len(hits)
                cum_hits += day_h
                cum_n += day_n
                series.append({
                    "date": d,
                    "day_hits": day_h,
                    "day_n": day_n,
                    "cum_hits": cum_hits,
                    "cum_n": cum_n,
                    "cum_rate": round(100.0 * cum_hits / cum_n, 1) if cum_n else 0.0,
                })
            if series:
                result[tier] = series
        return result

    series_nrfi     = build_series(nrfi_b)
    series_f5_ml    = build_series(f5_ml_b)
    series_f5_sp    = build_series(f5_spread_b)
    series_f5_tot   = build_series(f5_total_b)

    def _market_summary(series_dict):
        """Compute aggregate hits/n/rate across all tiers in a market."""
        total_hits = total_n = 0
        per_tier = {}
        for tier, series in series_dict.items():
            if not series:
                continue
            last = series[-1]
            per_tier[tier] = {
                "hits": last["cum_hits"],
                "n": last["cum_n"],
                "rate": last["cum_rate"],
            }
            total_hits += last["cum_hits"]
            total_n += last["cum_n"]
        return {
            "hits": total_hits,
            "n": total_n,
            "rate": round(100.0 * total_hits / total_n, 1) if total_n else 0.0,
            "per_tier": per_tier,
        }

    all_dates = sorted({game_date for game_date, _, _ in joined})

    return {
        "nrfi":      series_nrfi,
        "f5_ml":     series_f5_ml,
        "f5_spread": series_f5_sp,
        "f5_total":  series_f5_tot,
        "market_summaries": {
            "nrfi":      _market_summary(series_nrfi),
            "f5_ml":     _market_summary(series_f5_ml),
            "f5_spread": _market_summary(series_f5_sp),
            "f5_total":  _market_summary(series_f5_tot),
        },
        "summary": {
            "total_resolved":    len(joined),
            "days":              len(all_dates),
            "first_date":        all_dates[0] if all_dates else "",
            "last_date":         all_dates[-1] if all_dates else "",
        },
    }


# ---------------------------------------------------------------------------
# Placed bets summary (user's actual wagers, not model predictions)
# ---------------------------------------------------------------------------

def compute_placed_bets(placed_bets_csv):
    """
    Summarise placed_bets.csv into overall + per-market stats plus a cumulative
    $ P&L time series. Returns None when no placed-bet data exists.
    """
    rows = _load_csv(placed_bets_csv)
    if not rows:
        return None

    def _f(v, default=0.0):
        try:
            return float(v) if v not in (None, "") else default
        except (TypeError, ValueError):
            return default

    overall = {
        "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0,
        "units_wagered": 0.0, "dollars_wagered": 0.0,
        "units_pl": 0.0, "dollars_pl": 0.0,
    }
    by_market = defaultdict(lambda: {
        "total": 0, "wins": 0, "losses": 0, "pushes": 0, "pending": 0,
        "units_wagered": 0.0, "dollars_wagered": 0.0,
        "units_pl": 0.0, "dollars_pl": 0.0,
    })
    daily_pl = defaultdict(float)  # date -> dollars pl on that day (graded only)

    clean_rows = []
    for r in rows:
        market = (r.get("market") or "").strip().upper()
        if not market:
            continue
        result = (r.get("result") or "").strip().upper()
        units = _f(r.get("units"))
        wager = _f(r.get("wager_dollars"))
        units_pl = _f(r.get("units_pl"))
        dollars_pl = _f(r.get("dollars_pl"))

        overall["total"] += 1
        by_market[market]["total"] += 1
        overall["units_wagered"] += units
        overall["dollars_wagered"] += wager
        by_market[market]["units_wagered"] += units
        by_market[market]["dollars_wagered"] += wager

        if result == "WIN":
            overall["wins"] += 1; by_market[market]["wins"] += 1
        elif result == "LOSS":
            overall["losses"] += 1; by_market[market]["losses"] += 1
        elif result == "PUSH":
            overall["pushes"] += 1; by_market[market]["pushes"] += 1
        else:
            overall["pending"] += 1; by_market[market]["pending"] += 1

        if result in ("WIN", "LOSS", "PUSH"):
            overall["units_pl"] += units_pl
            overall["dollars_pl"] += dollars_pl
            by_market[market]["units_pl"] += units_pl
            by_market[market]["dollars_pl"] += dollars_pl
            date = (r.get("date") or "").strip()
            if date:
                daily_pl[date] += dollars_pl

        clean_rows.append(r)

    def _roi(pl, wagered):
        return round(100.0 * pl / wagered, 2) if wagered else 0.0

    overall["roi"] = _roi(overall["dollars_pl"], overall["dollars_wagered"])
    overall["graded"] = overall["wins"] + overall["losses"] + overall["pushes"]
    overall["decidable"] = overall["wins"] + overall["losses"]
    overall["win_rate"] = (
        round(100.0 * overall["wins"] / overall["decidable"], 1)
        if overall["decidable"] else 0.0
    )
    for m, v in by_market.items():
        v["roi"] = _roi(v["dollars_pl"], v["dollars_wagered"])
        v["graded"] = v["wins"] + v["losses"] + v["pushes"]
        v["decidable"] = v["wins"] + v["losses"]
        v["win_rate"] = (
            round(100.0 * v["wins"] / v["decidable"], 1)
            if v["decidable"] else 0.0
        )

    # Cumulative $ P&L series (sorted by date)
    cum = 0.0
    series = []
    for d in sorted(daily_pl):
        cum += daily_pl[d]
        series.append({"date": d, "day_pl": round(daily_pl[d], 2), "cum_pl": round(cum, 2)})

    # Recent bets (latest first), capped at 30
    clean_rows.sort(key=lambda r: (r.get("date", ""), r.get("logged_at", "")), reverse=True)
    recent = clean_rows[:30]

    return {
        "overall": overall,
        "by_market": dict(by_market),
        "daily_series": series,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

# Tier display order and colors
TIER_META = {
    # NRFI tiers
    "STRONG":    {"color": "#4ade80", "label": "Strong"},
    "LEAN":      {"color": "#facc15", "label": "Lean"},
    "TOSS-UP":   {"color": "#94a3b8", "label": "Toss-Up"},
    "FADE":      {"color": "#f87171", "label": "Fade"},
    # F5 ML confidence
    "MODERATE":  {"color": "#60a5fa", "label": "Moderate"},
    # F5 Total combinations
    "STRONG OVER":  {"color": "#4ade80", "label": "Strong Over"},
    "LEAN OVER":    {"color": "#86efac", "label": "Lean Over"},
    "STRONG UNDER": {"color": "#818cf8", "label": "Strong Under"},
    "LEAN UNDER":   {"color": "#a5b4fc", "label": "Lean Under"},
    "SLIGHT OVER":  {"color": "#d4d4d8", "label": "Slight Over"},
    "SLIGHT UNDER": {"color": "#a1a1aa", "label": "Slight Under"},
    # F5 Spread confidence (same as NRFI tiers, reuse)
}

MARKET_CONFIG = [
    {
        "key": "nrfi",
        "title": "NRFI Hit Rate",
        "subtitle": "No Run First Inning — by confidence tier",
        "tier_order": ["STRONG", "LEAN", "TOSS-UP", "FADE"],
    },
    {
        "key": "f5_ml",
        "title": "F5 Moneyline Hit Rate",
        "subtitle": "First 5 innings ML — by confidence tier (ties = loss)",
        "tier_order": ["STRONG", "MODERATE", "LEAN"],
    },
    {
        "key": "f5_spread",
        "title": "F5 Spread Hit Rate",
        "subtitle": "First 5 innings spread — by confidence (pushes = loss)",
        "tier_order": ["STRONG", "LEAN", "SLIGHT"],
    },
    {
        "key": "f5_total",
        "title": "F5 Total Hit Rate",
        "subtitle": "First 5 innings total — by lean & confidence (pushes = loss)",
        "tier_order": ["STRONG OVER", "LEAN OVER", "STRONG UNDER", "LEAN UNDER"],
    },
]


_CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0f1117;
    --surface:  #1a1d27;
    --surface2: #222636;
    --border:   #2e3347;
    --text:     #e2e8f0;
    --muted:    #94a3b8;
    --accent:   #4ade80;
    --green:    #4ade80;
    --red:      #f87171;
    --yellow:   #facc15;
    --blue:     #60a5fa;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.5;
    padding: 24px 20px 48px;
    min-height: 100vh;
    max-width: 1440px;
    margin: 0 auto;
  }

  .header {
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 8px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
  }
  .header-title { font-size: 1.5em; font-weight: 700; letter-spacing: 0.04em; }
  .header-sub   { color: var(--muted); font-size: 0.85em; }

  .date-range { color: var(--muted); font-size: 0.82em; margin-bottom: 20px; padding-top: 8px; }

  /* ---- Top-level summary cards ---- */
  .summary-row {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .stat-label { color: var(--muted); font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
  .stat-value { font-size: 1.5em; font-weight: 700; }
  .stat-sub   { color: var(--muted); font-size: 0.72em; margin-top: 3px; }
  .stat-value.green  { color: var(--green); }
  .stat-value.red    { color: var(--red); }
  .stat-value.yellow { color: var(--yellow); }
  .stat-value.blue   { color: var(--blue); }
  .stat-value.neutral { color: var(--text); }

  /* ---- Market sections ---- */
  .market-section {
    margin-bottom: 36px;
  }
  .market-header {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 14px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .market-title { font-size: 1.15em; font-weight: 700; }
  .market-subtitle { color: var(--muted); font-size: 0.8em; }

  /* ---- Tier breakdown table ---- */
  .tier-table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 16px;
    font-size: 0.88em;
  }
  .tier-table th {
    text-align: left;
    color: var(--muted);
    font-size: 0.78em;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 6px 12px;
    border-bottom: 1px solid var(--border);
    font-weight: 500;
  }
  .tier-table td {
    padding: 8px 12px;
    border-bottom: 1px solid rgba(46,51,71,0.5);
  }
  .tier-table tr:last-child td { border-bottom: none; }
  .tier-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    margin-right: 8px;
    vertical-align: middle;
  }
  .tier-name { font-weight: 600; }
  .tier-rate { font-weight: 700; font-size: 1.05em; }
  .tier-rate.above { color: var(--green); }
  .tier-rate.below { color: var(--red); }
  .tier-rate.neutral { color: var(--yellow); }
  .bar-bg {
    width: 100%;
    max-width: 140px;
    height: 8px;
    background: var(--surface2);
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill {
    height: 100%;
    border-radius: 4px;
    transition: width 0.3s;
  }

  /* ---- Chart cards (same as before, improved) ---- */
  .chart-section {
    display: grid;
    grid-template-columns: minmax(340px, 420px) 1fr;
    gap: 16px;
    align-items: stretch;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 20px 16px;
    overflow: hidden;
  }
  .chart-card.full-width { grid-column: 1 / -1; }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 16px;
    margin-bottom: 12px;
  }
  .legend-item  { display: flex; align-items: center; gap: 5px; font-size: 0.78em; }
  .legend-dot   { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { color: var(--muted); }
  .legend-n     { color: var(--muted); font-size: 0.85em; }

  .chart-svg { display: block; width: 100%; overflow: visible; }
  .chart-svg text { font-family: inherit; }

  /* ---- Tooltip ---- */
  #tooltip {
    position: fixed;
    pointer-events: none;
    background: #1e2235;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 0.82em;
    line-height: 1.6;
    white-space: nowrap;
    z-index: 999;
    display: none;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5);
  }
  #tooltip .tt-date { font-weight: 700; color: var(--text); margin-bottom: 4px; }
  #tooltip .tt-row  { display: flex; justify-content: space-between; gap: 20px; color: var(--muted); }
  #tooltip .tt-hit  { font-weight: 600; }

  .no-data       { text-align: center; padding: 80px 24px; color: var(--muted); }
  .no-data-icon  { font-size: 3em; margin-bottom: 16px; }
  .no-data-title { font-size: 1.2em; font-weight: 700; color: var(--text); margin-bottom: 8px; }
  .no-data-sub   { font-size: 0.9em; line-height: 1.7; }
  code { background: var(--surface2); padding: 1px 6px; border-radius: 4px; font-size: 0.95em; }

  @media (max-width: 900px) {
    .summary-row { grid-template-columns: repeat(2, 1fr); }
    .chart-section { grid-template-columns: 1fr; }
  }
  @media (max-width: 600px) {
    .summary-row { grid-template-columns: 1fr; }
  }

  /* ---- Placed bets section ---- */
  .placed-section {
    margin-top: 28px;
    padding: 20px;
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--surface2);
  }
  .placed-title {
    font-size: 1.1em;
    font-weight: 700;
    color: var(--text);
    margin-bottom: 4px;
  }
  .placed-sub {
    font-size: 0.85em;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .placed-market-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }
  .placed-market-card {
    background: var(--surface2);
    border-radius: 8px;
    padding: 10px 12px;
  }
  .placed-mkt-name {
    font-size: 0.78em;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    margin-bottom: 4px;
  }
  .placed-mkt-main {
    font-size: 1.1em;
    font-weight: 800;
    margin-bottom: 2px;
  }
  .placed-mkt-sub {
    font-size: 0.75em;
    color: var(--muted);
  }
  .placed-pl-pos { color: #4ade80; }
  .placed-pl-neg { color: #f87171; }
  .placed-pl-neu { color: var(--muted); }

  .placed-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82em;
    margin-top: 8px;
  }
  .placed-table th {
    text-align: left;
    padding: 8px 10px;
    color: var(--muted);
    font-weight: 600;
    border-bottom: 1px solid var(--surface2);
    text-transform: uppercase;
    font-size: 0.72em;
    letter-spacing: 0.04em;
  }
  .placed-table td {
    padding: 7px 10px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .placed-result-WIN { color: #4ade80; font-weight: 700; }
  .placed-result-LOSS { color: #f87171; font-weight: 700; }
  .placed-result-PUSH { color: #facc15; font-weight: 700; }
  .placed-result-pending { color: var(--muted); font-style: italic; }

  .placed-chart-wrap {
    background: var(--surface2);
    border-radius: 8px;
    padding: 14px;
    margin-bottom: 18px;
  }
  .placed-chart-title {
    font-size: 0.82em;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    margin-bottom: 8px;
  }
  .placed-chart-wrap svg { width: 100%; height: auto; display: block; }
"""

_JS = """
const PAD = { top: 18, right: 36, bottom: 46, left: 50 };
const VW  = 720;
const VH  = 280;
const BREAK_EVEN = 52.4;

function xOf(allDates, dateStr) {
  const n = allDates.length;
  const i = allDates.indexOf(dateStr);
  if (n <= 1) return PAD.left + (VW - PAD.left - PAD.right) / 2;
  return PAD.left + (i / (n - 1)) * (VW - PAD.left - PAD.right);
}
function yOf(rate) {
  return PAD.top + (1 - rate / 100) * (VH - PAD.top - PAD.bottom);
}

function rollingPoints(series, W) {
  W = W || 7;
  var pts = [];
  for (var i = W - 1; i < series.length; i++) {
    var rh = 0, rn = 0;
    for (var j = i - W + 1; j <= i; j++) { rh += series[j].day_hits; rn += series[j].day_n; }
    if (rn > 0) pts.push({ date: series[i].date, rate: 100 * rh / rn });
  }
  return pts;
}

function svgEl(tag, attrs, inner) {
  var attrStr = Object.keys(attrs).map(function(k) { return k + '="' + attrs[k] + '"'; }).join(' ');
  return inner !== undefined
    ? '<' + tag + ' ' + attrStr + '>' + inner + '</' + tag + '>'
    : '<' + tag + ' ' + attrStr + '/>';
}

function buildChart(cfg) {
  var marketData = CHARTS_DATA[cfg.key] || {};
  var tiers = cfg.tiers.filter(function(t) { return (marketData[t.key] || []).length > 0; });
  if (!tiers.length) return null;

  var dateSet = {};
  tiers.forEach(function(t) {
    (marketData[t.key] || []).forEach(function(p) { dateSet[p.date] = 1; });
  });
  var allDates = Object.keys(dateSet).sort();
  var nDates   = allDates.length;
  var innerW   = VW - PAD.left - PAD.right;
  var innerH   = VH - PAD.top  - PAD.bottom;

  var els = [];

  // Background
  els.push(svgEl('rect', { x:0, y:0, width:VW, height:VH, fill:'#1a1d27', rx:0 }));

  // Gridlines + Y labels
  [0, 25, 50, 75, 100].forEach(function(v) {
    var y = yOf(v).toFixed(1);
    els.push(svgEl('line', { x1:PAD.left, y1:y, x2:VW-PAD.right, y2:y,
      stroke: v === 50 ? '#334155' : '#1e2435', 'stroke-width': v === 50 ? 1 : 0.8 }));
    els.push(svgEl('text', { x: PAD.left - 6, y: (parseFloat(y)+4).toFixed(1),
      'text-anchor':'end', fill:'#475569', 'font-size':10 }, v + '%'));
  });

  // Reference line at 52.4%
  var refY = yOf(52.4).toFixed(1);
  els.push(svgEl('line', { x1:PAD.left, y1:refY, x2:VW-PAD.right, y2:refY,
    stroke:'rgba(250,204,21,0.45)', 'stroke-width':'1.2', 'stroke-dasharray':'5,4' }));
  els.push(svgEl('text', { x: VW-PAD.right+4, y: (parseFloat(refY)+4).toFixed(1),
    fill:'rgba(250,204,21,0.6)', 'font-size':9 }, '52.4%'));

  // X-axis ticks + labels
  var tickEvery = nDates > 14 ? 7 : nDates > 7 ? 3 : 1;
  allDates.forEach(function(d, i) {
    if (i % tickEvery !== 0 && i !== nDates - 1) return;
    var x = xOf(allDates, d).toFixed(1);
    var axisY = (PAD.top + innerH).toFixed(1);
    els.push(svgEl('line', { x1:x, y1:axisY, x2:x, y2:(parseFloat(axisY)+4).toFixed(1),
      stroke:'#334155', 'stroke-width':1 }));
    els.push(svgEl('text', { x:x, y:(parseFloat(axisY)+15).toFixed(1),
      'text-anchor':'middle', fill:'#475569', 'font-size':'9.5' }, d.slice(5)));
  });

  // Axes
  els.push(svgEl('line', { x1:PAD.left, y1:PAD.top, x2:PAD.left, y2:PAD.top+innerH,
    stroke:'#334155', 'stroke-width':1 }));
  els.push(svgEl('line', { x1:PAD.left, y1:PAD.top+innerH, x2:VW-PAD.right, y2:PAD.top+innerH,
    stroke:'#334155', 'stroke-width':1 }));

  // Lines + dots per tier
  var dotData = [];
  tiers.forEach(function(t) {
    var series = marketData[t.key] || [];
    if (!series.length) return;
    var col = t.color;

    // Cumulative solid line
    if (series.length > 1) {
      var pts = series.map(function(p) {
        return xOf(allDates, p.date).toFixed(1) + ',' + yOf(p.cum_rate).toFixed(1);
      }).join(' ');
      els.push(svgEl('polyline', { points:pts, fill:'none', stroke:col,
        'stroke-width':'2.2', 'stroke-linejoin':'round', 'stroke-linecap':'round' }));
    }

    // 7-day rolling dashed line
    var roll = rollingPoints(series, 7);
    if (roll.length > 1) {
      var rpts = roll.map(function(p) {
        return xOf(allDates, p.date).toFixed(1) + ',' + yOf(p.rate).toFixed(1);
      }).join(' ');
      els.push(svgEl('polyline', { points:rpts, fill:'none', stroke:col,
        'stroke-width':'1.4', 'stroke-dasharray':'5,3', opacity:'0.5',
        'stroke-linejoin':'round' }));
    }

    // Collect dots
    series.forEach(function(p) {
      dotData.push(Object.assign({}, p, { tier: t.label, color: col }));
    });
  });

  // Dots on top (so they render above lines)
  dotData.forEach(function(p) {
    var x = xOf(allDates, p.date).toFixed(1);
    var y = yOf(p.cum_rate).toFixed(1);
    var enc = encodeURIComponent(JSON.stringify(p));
    els.push('<circle cx="' + x + '" cy="' + y + '" r="4" fill="' + p.color +
      '" stroke="#1a1d27" stroke-width="1.5" class="chart-dot" data-pt="' + enc +
      '" style="cursor:pointer"/>');
  });

  return '<svg class="chart-svg" viewBox="0 0 ' + VW + ' ' + VH +
    '" preserveAspectRatio="xMidYMid meet">' + els.join('') + '</svg>';
}

function buildLegend(cfg) {
  var marketData = CHARTS_DATA[cfg.key] || {};
  var tiers = cfg.tiers.filter(function(t) { return (marketData[t.key] || []).length > 0; });
  if (!tiers.length) return '';
  var items = tiers.map(function(t) {
    var series = marketData[t.key] || [];
    var last = series[series.length - 1];
    var n = last ? last.cum_n : 0;
    return '<div class="legend-item">' +
      '<span class="legend-dot" style="background:' + t.color + '"></span>' +
      '<span class="legend-label">' + t.label + '</span>' +
      '<span class="legend-n">(' + n + ')</span>' +
      '</div>';
  });
  items.push('<div class="legend-item" style="margin-left:8px;opacity:0.55">' +
    '<span style="display:inline-block;width:18px;height:0;border-top:2px dashed #94a3b8;vertical-align:middle"></span>' +
    '&nbsp;<span class="legend-label" style="font-size:0.85em">7-day rolling</span>' +
    '</div>');
  return '<div class="legend">' + items.join('') + '</div>';
}

function rateClass(rate) {
  if (rate >= BREAK_EVEN + 2) return 'above';
  if (rate <= BREAK_EVEN - 2) return 'below';
  return 'neutral';
}
function barColor(rate) {
  if (rate >= BREAK_EVEN + 2) return '#4ade80';
  if (rate <= BREAK_EVEN - 2) return '#f87171';
  return '#facc15';
}

function buildTierTable(cfg) {
  var marketData = CHARTS_DATA[cfg.key] || {};
  var tiers = cfg.tiers.filter(function(t) { return (marketData[t.key] || []).length > 0; });
  if (!tiers.length) return '';

  // Sort tiers by sample size descending for easier reading
  tiers.sort(function(a, b) {
    var sa = marketData[a.key] || [];
    var sb = marketData[b.key] || [];
    var na = sa.length ? sa[sa.length - 1].cum_n : 0;
    var nb = sb.length ? sb[sb.length - 1].cum_n : 0;
    return nb - na;
  });

  var rows = tiers.map(function(t) {
    var series = marketData[t.key] || [];
    if (!series.length) return '';
    var last = series[series.length - 1];
    var rate = last.cum_rate;
    var n = last.cum_n;
    var hits = last.cum_hits;
    var rCls = rateClass(rate);
    var barW = Math.min(100, Math.max(0, rate));
    // ROI at -110: win 100/110 per win, lose 110 per loss
    //   per unit risked: (hits * (100/110) - losses) / n * 100  (as percent)
    var losses = n - hits;
    var unitsWon = hits * (100 / 110) - losses;
    var roi = n ? (unitsWon / n) * 100 : 0;
    var roiCls = roi > 0 ? 'above' : (roi < 0 ? 'below' : 'neutral');
    var roiStr = (roi > 0 ? '+' : '') + roi.toFixed(1) + '%';
    return '<tr>' +
      '<td><span class="tier-dot" style="background:' + t.color + '"></span>' +
        '<span class="tier-name">' + t.label + '</span></td>' +
      '<td><div class="bar-bg"><div class="bar-fill" style="width:' + barW + '%;background:' + barColor(rate) + '"></div></div></td>' +
      '<td class="tier-rate ' + rCls + '">' + rate.toFixed(1) + '%</td>' +
      '<td style="color:var(--muted);white-space:nowrap">' + hits + '&thinsp;/&thinsp;' + n + '</td>' +
      '<td class="tier-rate ' + roiCls + '" style="font-size:0.95em">' + roiStr + '</td>' +
      '</tr>';
  });

  return '<table class="tier-table">' +
    '<thead><tr>' +
      '<th>Tier</th>' +
      '<th colspan="2">Hit Rate</th>' +
      '<th>Record</th>' +
      '<th title="Return on investment at -110 odds">ROI</th>' +
    '</tr></thead>' +
    '<tbody>' + rows.join('') + '</tbody></table>';
}

function renderCharts() {
  var container = document.getElementById('marketsContainer');
  if (!container) return;
  MARKET_CONFIGS.forEach(function(cfg) {
    var svgHtml = buildChart(cfg);
    if (!svgHtml) return;
    var summary = (MARKET_SUMMARIES || {})[cfg.key] || {};
    var overallRate = typeof summary.rate === 'number' ? summary.rate : 0;
    var overallCls = rateClass(overallRate);
    var overallTxt = summary.n ? overallRate.toFixed(1) + '% <span style="color:var(--muted);font-weight:500;font-size:0.7em">(' + summary.hits + '/' + summary.n + ')</span>' : '—';

    // Overall ROI at -110
    var overallROI = summary.n ? ((summary.hits * (100/110) - (summary.n - summary.hits)) / summary.n) * 100 : 0;
    var roiCls = overallROI > 0 ? 'above' : (overallROI < 0 ? 'below' : 'neutral');
    var roiStr = (overallROI > 0 ? '+' : '') + overallROI.toFixed(1) + '%';
    var roiBadge = summary.n
      ? '<span style="color:var(--muted);margin:0 6px 0 14px">ROI:</span><span class="tier-rate ' + roiCls + '">' + roiStr + '</span>'
      : '';

    var section = document.createElement('div');
    section.className = 'market-section';
    section.innerHTML =
      '<div class="market-header">' +
        '<span class="market-title">' + cfg.title + '</span>' +
        '<span class="market-subtitle">' + cfg.subtitle + '</span>' +
        '<span style="margin-left:auto;font-size:0.88em;white-space:nowrap">' +
          '<span style="color:var(--muted);margin-right:6px">Overall:</span>' +
          '<span class="tier-rate ' + overallCls + '">' + overallTxt + '</span>' +
          roiBadge +
        '</span>' +
      '</div>' +
      '<div class="chart-section">' +
        '<div class="chart-card">' +
          '<div class="chart-title" style="font-size:0.82em;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:10px">Tier Breakdown</div>' +
          buildTierTable(cfg) +
        '</div>' +
        '<div class="chart-card">' +
          '<div class="chart-title" style="font-size:0.82em;color:var(--muted);text-transform:uppercase;letter-spacing:0.08em;margin-bottom:10px">Accuracy Over Time</div>' +
          buildLegend(cfg) + svgHtml +
        '</div>' +
      '</div>';
    container.appendChild(section);
  });
}

function initTooltip() {
  var tt = document.getElementById('tooltip');
  document.addEventListener('mousemove', function(e) {
    var dot = e.target.closest('.chart-dot');
    if (!dot) { tt.style.display = 'none'; return; }
    var p = JSON.parse(decodeURIComponent(dot.dataset.pt));
    var dayRate = p.day_n ? (100 * p.day_hits / p.day_n).toFixed(1) : '—';
    tt.innerHTML =
      '<div class="tt-date">' + p.date + '</div>' +
      '<div class="tt-row"><span>' + p.tier + '</span></div>' +
      '<div class="tt-row"><span>Cumulative</span>' +
        '<span class="tt-hit" style="color:' + p.color + '">' + p.cum_rate + '%</span></div>' +
      '<div class="tt-row"><span>Today</span>' +
        '<span class="tt-hit">' + dayRate + '% (' + p.day_hits + '/' + p.day_n + ')</span></div>' +
      '<div class="tt-row"><span>Total</span>' +
        '<span>' + p.cum_hits + '/' + p.cum_n + '</span></div>';
    tt.style.display = 'block';
    var margin = 14;
    var left = e.clientX + margin;
    var top  = e.clientY - tt.offsetHeight / 2;
    if (left + tt.offsetWidth > window.innerWidth - 8) left = e.clientX - tt.offsetWidth - margin;
    if (top < 8) top = 8;
    tt.style.left = left + 'px';
    tt.style.top  = top  + 'px';
  });
}

renderCharts();
initTooltip();
"""


def _fmt_money(v):
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _pl_class(v):
    if v > 0.01:
        return "placed-pl-pos"
    if v < -0.01:
        return "placed-pl-neg"
    return "placed-pl-neu"


def _build_pl_chart_svg(series):
    """Simple cumulative-$ P&L line chart. Returns inline SVG string."""
    if not series:
        return '<div class="placed-sub">No graded bets yet — chart will appear after first settlement.</div>'

    W, H = 720, 240
    pad_l, pad_r, pad_t, pad_b = 52, 20, 18, 34

    xs = list(range(len(series)))
    ys = [pt["cum_pl"] for pt in series]
    y_min = min(ys + [0.0])
    y_max = max(ys + [0.0])
    if y_max == y_min:
        y_max = y_min + 1.0

    def sx(i):
        if len(xs) == 1:
            return pad_l + (W - pad_l - pad_r) / 2
        return pad_l + (W - pad_l - pad_r) * i / (len(xs) - 1)

    def sy(y):
        return H - pad_b - (H - pad_t - pad_b) * (y - y_min) / (y_max - y_min)

    zero_y = sy(0)

    # Gridline labels (0, min, max)
    labels = sorted({round(y_min, 2), 0.0, round(y_max, 2)})
    grid = ""
    for lv in labels:
        y = sy(lv)
        grid += (
            f'<line x1="{pad_l}" x2="{W - pad_r}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="rgba(255,255,255,0.08)" stroke-width="1"/>'
            f'<text x="{pad_l - 8}" y="{y + 3:.1f}" fill="#94a3b8" '
            f'font-size="10" text-anchor="end">${lv:,.0f}</text>'
        )

    # X-axis date labels (first, last, optionally middle)
    def date_label(idx):
        return series[idx]["date"][5:]  # MM-DD
    if len(series) == 1:
        x_labels = [(0, series[0]["date"])]
    elif len(series) == 2:
        x_labels = [(0, series[0]["date"]), (len(series) - 1, series[-1]["date"])]
    else:
        mid = len(series) // 2
        x_labels = [
            (0, series[0]["date"]),
            (mid, series[mid]["date"]),
            (len(series) - 1, series[-1]["date"]),
        ]
    x_axis = ""
    for idx, d in x_labels:
        x_axis += (
            f'<text x="{sx(idx):.1f}" y="{H - pad_b + 16}" fill="#94a3b8" '
            f'font-size="10" text-anchor="middle">{d}</text>'
        )

    # Line path
    points = " ".join(f"{sx(i):.1f},{sy(y):.1f}" for i, y in enumerate(ys))
    final_pl = ys[-1] if ys else 0.0
    line_color = "#4ade80" if final_pl >= 0 else "#f87171"

    # Point dots
    dots = ""
    for i, pt in enumerate(series):
        dots += (
            f'<circle cx="{sx(i):.1f}" cy="{sy(pt["cum_pl"]):.1f}" r="3" '
            f'fill="{line_color}"><title>{pt["date"]}: cum ${pt["cum_pl"]:,.2f} '
            f'(day {pt["day_pl"]:+,.2f})</title></circle>'
        )

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">'
        f'{grid}'
        f'<line x1="{pad_l}" x2="{W - pad_r}" y1="{zero_y:.1f}" y2="{zero_y:.1f}" '
        f'stroke="rgba(255,255,255,0.25)" stroke-width="1" stroke-dasharray="3,3"/>'
        f'<polyline points="{points}" fill="none" stroke="{line_color}" stroke-width="2"/>'
        f'{dots}'
        f'{x_axis}'
        f'</svg>'
    )


def _build_placed_section(summary):
    """Build the full 'My Placed Bets' section HTML. Returns '' if no bets."""
    if not summary:
        return ""
    overall = summary["overall"]
    if overall["total"] == 0:
        return ""

    by_market = summary["by_market"]
    series = summary["daily_series"]
    recent = summary["recent"]

    # Ordered market list: show only markets that have bets
    MARKET_LABELS = {
        "F5_ML": "F5 Moneyline",
        "F5_TOTAL": "F5 Total",
        "NRFI": "NRFI",
        "YRFI": "YRFI",
    }

    # --- Overall summary cards ---
    o_pl_cls = _pl_class(overall["dollars_pl"])
    o_roi_cls = _pl_class(overall["dollars_pl"])  # same sign as pl
    overall_cards = (
        '<div class="placed-market-grid">'
        + '<div class="placed-market-card">'
        +   '<div class="placed-mkt-name">Overall</div>'
        +   f'<div class="placed-mkt-main {o_pl_cls}">{_fmt_money(overall["dollars_pl"])}</div>'
        +   f'<div class="placed-mkt-sub">{overall["units_pl"]:+.2f}u · ROI <span class="{o_roi_cls}">{overall["roi"]:+.2f}%</span></div>'
        + '</div>'
        + '<div class="placed-market-card">'
        +   '<div class="placed-mkt-name">Record</div>'
        +   f'<div class="placed-mkt-main">{overall["wins"]}–{overall["losses"]}'
        +   (f'–{overall["pushes"]}' if overall["pushes"] else '') + '</div>'
        +   f'<div class="placed-mkt-sub">{overall["win_rate"]:.1f}% win rate · {overall["pending"]} pending</div>'
        + '</div>'
        + '<div class="placed-market-card">'
        +   '<div class="placed-mkt-name">Wagered</div>'
        +   f'<div class="placed-mkt-main">${overall["dollars_wagered"]:,.2f}</div>'
        +   f'<div class="placed-mkt-sub">{overall["units_wagered"]:.1f}u across {overall["total"]} bets</div>'
        + '</div>'
        + '</div>'
    )

    # --- Per-market breakdown ---
    market_cards = []
    for mkey, mlabel in MARKET_LABELS.items():
        mv = by_market.get(mkey)
        if not mv or mv["total"] == 0:
            continue
        pl_cls = _pl_class(mv["dollars_pl"])
        record = f'{mv["wins"]}–{mv["losses"]}' + (f'–{mv["pushes"]}' if mv["pushes"] else '')
        market_cards.append(
            '<div class="placed-market-card">'
            + f'<div class="placed-mkt-name">{mlabel}</div>'
            + f'<div class="placed-mkt-main {pl_cls}">{_fmt_money(mv["dollars_pl"])}</div>'
            + f'<div class="placed-mkt-sub">{record} · {mv["win_rate"]:.1f}% · ROI {mv["roi"]:+.2f}%'
            + (f' · {mv["pending"]} pending' if mv["pending"] else '')
            + '</div></div>'
        )
    per_market_block = (
        '<div class="placed-mkt-name" style="margin-top:4px;margin-bottom:6px">By Market</div>'
        + '<div class="placed-market-grid">'
        + "".join(market_cards)
        + '</div>'
    ) if market_cards else ""

    # --- Chart ---
    chart_block = (
        '<div class="placed-chart-wrap">'
        + '<div class="placed-chart-title">Cumulative $ P&amp;L</div>'
        + _build_pl_chart_svg(series)
        + '</div>'
    )

    # --- Recent bets table ---
    rows_html = []
    for r in recent:
        result = (r.get("result") or "").strip().upper()
        if result in ("WIN", "LOSS", "PUSH"):
            cls = f"placed-result-{result}"
            rdisp = result
        else:
            cls = "placed-result-pending"
            rdisp = "pending"
        pl_raw = r.get("dollars_pl", "")
        try:
            pl_val = float(pl_raw) if pl_raw not in ("", None) else None
        except (TypeError, ValueError):
            pl_val = None
        pl_disp = _fmt_money(pl_val) if pl_val is not None else "—"
        pl_cls = _pl_class(pl_val) if pl_val is not None else "placed-pl-neu"
        units = r.get("units", "")
        odds = r.get("odds", "")
        odds_disp = f"+{odds}" if odds and odds.lstrip("+-").isdigit() and int(odds) > 0 else odds
        rows_html.append(
            "<tr>"
            f"<td>{r.get('date', '')}</td>"
            f"<td>{r.get('matchup', '')}</td>"
            f"<td>{MARKET_LABELS.get(r.get('market', ''), r.get('market', ''))}</td>"
            f"<td>{r.get('pick', '')}</td>"
            f"<td>{units}u</td>"
            f"<td>{odds_disp}</td>"
            f'<td class="{cls}">{rdisp}</td>'
            f'<td class="{pl_cls}">{pl_disp}</td>'
            "</tr>"
        )
    recent_label = f"Recent Bets (last {len(recent)} of {overall['total']})" if overall['total'] > len(recent) else "Recent Bets"
    table_block = (
        f'<div class="placed-mkt-name" style="margin-top:4px;margin-bottom:6px">{recent_label}</div>'
        '<table class="placed-table">'
        '<thead><tr>'
        '<th>Date</th><th>Matchup</th><th>Market</th><th>Pick</th>'
        '<th>Units</th><th>Odds</th><th>Result</th><th>$ P&amp;L</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows_html)}</tbody>'
        '</table>'
    ) if rows_html else ""

    return (
        '<div class="placed-section">'
        + '<div class="placed-title">My Placed Bets</div>'
        + '<div class="placed-sub">Actual wagers placed (exported from the daily dashboard). '
          'P&amp;L uses real odds; bets on unfinished games show as pending.</div>'
        + overall_cards
        + per_market_block
        + chart_block
        + table_block
        + '</div>'
    )


def _build_html(data, placed_bets_summary=None):
    placed_block = _build_placed_section(placed_bets_summary)
    summary = data.get("summary", {})
    total   = summary.get("total_resolved", 0)
    days    = summary.get("days", 0)
    first_d = summary.get("first_date", "—")
    last_d  = summary.get("last_date", "—")

    market_summaries = data.get("market_summaries", {})

    has_data = bool(total)

    # Serialise chart data as JSON for the JS renderer
    charts_json = json.dumps({
        cfg["key"]: {
            tier: data.get(cfg["key"], {}).get(tier, [])
            for tier in cfg["tier_order"]
            if tier in data.get(cfg["key"], {})
        }
        for cfg in MARKET_CONFIG
    }, indent=None)

    market_configs_json = json.dumps([
        {
            "key": cfg["key"],
            "title": cfg["title"],
            "subtitle": cfg["subtitle"],
            "tiers": [
                {
                    "key": t,
                    "label": TIER_META.get(t, {}).get("label", t),
                    "color": TIER_META.get(t, {}).get("color", "#ffffff"),
                }
                for t in cfg["tier_order"]
                if t in data.get(cfg["key"], {})
            ],
        }
        for cfg in MARKET_CONFIG
    ])

    market_summaries_json = json.dumps(market_summaries)

    date_range = f"{first_d} \u2192 {last_d}" if first_d and first_d != "—" else "—"

    def _rate_class(rate, n):
        if not n:
            return "neutral"
        if rate >= 54.4:
            return "green"
        if rate <= 50.4:
            return "red"
        return "yellow"

    def _stat_card(label, rate, n, hits, sub_override=None):
        rate_str = f"{rate:.1f}%" if n else "—"
        rcls = _rate_class(rate, n)
        sub = sub_override if sub_override is not None else (f"{hits} / {n} hit" if n else "no graded picks yet")
        return (
            '<div class="stat-card">'
            f'<div class="stat-label">{label}</div>'
            f'<div class="stat-value {rcls}">{rate_str}</div>'
            f'<div class="stat-sub">{sub}</div>'
            '</div>'
        )

    no_data_block = (
        ""
        if has_data else
        '<div class="no-data">'
        '<div class="no-data-icon">\U0001f4ca</div>'
        '<div class="no-data-title">No resolved predictions yet</div>'
        '<div class="no-data-sub">Charts will appear once outcomes have been fetched '
        'for at least one completed game date.<br>'
        'Run <code>python backtest.py</code> to pull outcomes.</div>'
        '</div>'
    )
    charts_block = '<div id="marketsContainer"></div>' if has_data else ""

    # Build summary cards — one per market, showing overall hit rate with
    # color-coded break-even comparison
    nrfi_s  = market_summaries.get("nrfi", {})
    ml_s    = market_summaries.get("f5_ml", {})
    sp_s    = market_summaries.get("f5_spread", {})
    tot_s   = market_summaries.get("f5_total", {})

    summary_cards = (
        '<div class="summary-row">'
        + _stat_card("NRFI",       nrfi_s.get("rate", 0), nrfi_s.get("n", 0), nrfi_s.get("hits", 0))
        + _stat_card("F5 Moneyline", ml_s.get("rate", 0), ml_s.get("n", 0),  ml_s.get("hits", 0))
        + _stat_card("F5 Spread",    sp_s.get("rate", 0), sp_s.get("n", 0),  sp_s.get("hits", 0))
        + _stat_card("F5 Total",    tot_s.get("rate", 0), tot_s.get("n", 0), tot_s.get("hits", 0))
        + '</div>'
        + '<div class="summary-row">'
        + '<div class="stat-card">'
        +   '<div class="stat-label">Resolved Games</div>'
        +   f'<div class="stat-value neutral">{total}</div>'
        +   f'<div class="stat-sub">across {days} day' + ("s" if days != 1 else "") + '</div>'
        + '</div>'
        + '<div class="stat-card">'
        +   '<div class="stat-label">Date Range</div>'
        +   f'<div class="stat-value neutral" style="font-size:1.05em">{date_range}</div>'
        +   '<div class="stat-sub">first → last resolved</div>'
        + '</div>'
        + '<div class="stat-card">'
        +   '<div class="stat-label">Break-Even</div>'
        +   '<div class="stat-value yellow">52.4%</div>'
        +   '<div class="stat-sub">for standard -110 lines</div>'
        + '</div>'
        + '<div class="stat-card">'
        +   '<div class="stat-label">Push Handling</div>'
        +   '<div class="stat-value neutral" style="font-size:1em">Pushes = Losses</div>'
        +   '<div class="stat-sub">conservative grading</div>'
        + '</div>'
        + '</div>'
    )

    data_script = (
        "const CHARTS_DATA = " + charts_json + ";\n"
        "const MARKET_CONFIGS = " + market_configs_json + ";\n"
        "const MARKET_SUMMARIES = " + market_summaries_json + ";\n"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>NRFI / F5 Hit Rate Tracker</title>\n"
        "<style>\n" + _CSS + "\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="header">'
        '<span class="header-title">NRFI / F5 Hit Rate Tracker</span>'
        '<span class="header-sub">Cumulative accuracy over time \u00b7 pushes = losses</span>'
        '</div>\n'
        + placed_block
        + summary_cards
        + no_data_block
        + "\n"
        + charts_block
        + '\n<div id="tooltip"></div>\n'
        "<script>\n"
        + data_script
        + _JS
        + "\n</script>\n"
        "</body>\n"
        "</html>\n"
    )
def generate_hit_rate_dashboard(predictions_csv, outcomes_csv, output_path, placed_bets_csv=None):
    """
    Compute hit rates from predictions + outcomes CSVs and write a self-contained
    HTML dashboard to output_path. Safe to call even when CSVs are missing or empty.

    If placed_bets_csv is None, defaults to `<outcomes_dir>/placed_bets.csv`.
    """
    try:
        if placed_bets_csv is None:
            placed_bets_csv = os.path.join(os.path.dirname(outcomes_csv), "placed_bets.csv")
        data = compute_hit_rates(predictions_csv, outcomes_csv)
        placed_summary = compute_placed_bets(placed_bets_csv)
        html = _build_html(data, placed_bets_summary=placed_summary)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(html)
    except Exception as e:
        # Never crash the main workflow
        print(f"  [hit_rate_tracker] Warning: could not generate dashboard — {e}")
