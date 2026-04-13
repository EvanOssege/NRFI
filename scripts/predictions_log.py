#!/usr/bin/env python3
"""
Predictions Log
================
Upsert-based CSV log of every NRFI prediction the model produces. Each row
captures the score, tier, and the value of every component adjustment so we
can later correlate individual signals against actual outcomes.

Schema is intentionally flat (one row per game) and stable so backtest.py can
join against outcomes.csv on game_pk.

Usage:
    from predictions_log import log_predictions
    log_predictions(date_iso, games, csv_path)
"""

import csv
import os
from datetime import datetime

PREDICTIONS_COLUMNS = [
    "prediction_date",      # the slate date the model ran for (YYYY-MM-DD)
    "logged_at",            # ISO timestamp the row was written
    "game_pk",              # MLB stats API gamePk — primary join key
    "matchup",              # e.g. "NYY @ TB"
    "away_team",
    "home_team",
    "away_pitcher",
    "home_pitcher",
    "away_pitcher_hand",
    "home_pitcher_hand",
    "venue",
    "is_indoor",
    # ---- final score & tier ----
    "nrfi_score",
    "tier",
    # ---- components: base ----
    "pitcher_component",
    "lineup_component",
    # ---- components: adjustments ----
    "fi_adj",
    "bvp_adj",
    "platoon_adj",
    "streak_adj",
    "park_adj",
    "weather_adj",
    "team_tendency_adj",
    # ---- raw subscores (useful for component-level analysis) ----
    "away_pitcher_score",
    "home_pitcher_score",
    "home_lineup_threat",
    "away_lineup_threat",
    # ---- rest & workload ----
    "rest_adj",
    "away_days_rest",
    "home_days_rest",
    "away_last_pitches",
    "home_last_pitches",
    # ---- wind direction ----
    "wind_dir",
    "wind_effect_label",
    "wind_component_out",
    "wind_component_in",
    # ---- lineup confidence at prediction time ----
    "home_real_lineup",
    "away_real_lineup",
    # ---- F5 predictions ----
    "f5_ml_pick",
    "f5_ml_edge",
    "f5_ml_confidence",
    "f5_spread_label",
    "f5_spread_confidence",
    "f5_total_projected",
    "f5_total_lean",
    "f5_total_confidence",
    "f5_total_line",
]


def _row_from_game(prediction_date: str, g: dict) -> dict:
    """Flatten a game result dict into a single CSV row."""
    nrfi = g.get("nrfi", {})
    return {
        "prediction_date": prediction_date,
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "game_pk": g.get("game_pk", ""),
        "matchup": g.get("matchup", ""),
        "away_team": g.get("away_team", ""),
        "home_team": g.get("home_team", ""),
        "away_pitcher": g.get("away_pitcher", ""),
        "home_pitcher": g.get("home_pitcher", ""),
        "away_pitcher_hand": g.get("away_pitcher_hand", ""),
        "home_pitcher_hand": g.get("home_pitcher_hand", ""),
        "venue": g.get("venue", ""),
        "is_indoor": int(bool(g.get("is_indoor", False))),
        "nrfi_score": nrfi.get("score", ""),
        "tier": nrfi.get("tier", ""),
        "pitcher_component": nrfi.get("pitcher_component", ""),
        "lineup_component": nrfi.get("lineup_component", ""),
        "fi_adj": nrfi.get("fi_adj", 0),
        "bvp_adj": nrfi.get("bvp_adj", 0),
        "platoon_adj": nrfi.get("platoon_adj", 0),
        "streak_adj": nrfi.get("streak_adj", 0),
        "park_adj": nrfi.get("park_adj", 0),
        "weather_adj": nrfi.get("weather_adj", 0),
        "team_tendency_adj": nrfi.get("team_tendency_adj", 0),
        "rest_adj": nrfi.get("rest_adj", 0),
        "away_days_rest": (g.get("away_rest") or {}).get("days_rest", ""),
        "home_days_rest": (g.get("home_rest") or {}).get("days_rest", ""),
        "away_last_pitches": (g.get("away_rest") or {}).get("last_pitches", ""),
        "home_last_pitches": (g.get("home_rest") or {}).get("last_pitches", ""),
        "wind_dir": (g.get("weather") or {}).get("wind_dir", ""),
        "wind_effect_label": ((g.get("weather") or {}).get("wind_effect") or {}).get("label", ""),
        "wind_component_out": ((g.get("weather") or {}).get("wind_effect") or {}).get("component_out", ""),
        "wind_component_in": ((g.get("weather") or {}).get("wind_effect") or {}).get("component_in", ""),
        "away_pitcher_score": g.get("away_pitcher_score", ""),
        "home_pitcher_score": g.get("home_pitcher_score", ""),
        "home_lineup_threat": g.get("home_lineup_threat", ""),
        "away_lineup_threat": g.get("away_lineup_threat", ""),
        "home_real_lineup": int(bool(g.get("home_real_lineup", False))),
        "away_real_lineup": int(bool(g.get("away_real_lineup", False))),
        # F5 predictions
        "f5_ml_pick": (g.get("f5") or {}).get("ml", {}).get("pick", ""),
        "f5_ml_edge": (g.get("f5") or {}).get("ml", {}).get("edge", ""),
        "f5_ml_confidence": (g.get("f5") or {}).get("ml", {}).get("confidence", ""),
        "f5_spread_label": (g.get("f5") or {}).get("spread", {}).get("recommended_label", ""),
        "f5_spread_confidence": (g.get("f5") or {}).get("spread", {}).get("confidence", ""),
        "f5_total_projected": (g.get("f5") or {}).get("total", {}).get("projected_total", ""),
        "f5_total_lean": (g.get("f5") or {}).get("total", {}).get("lean", ""),
        "f5_total_confidence": (g.get("f5") or {}).get("total", {}).get("confidence", ""),
        "f5_total_line": (g.get("f5") or {}).get("total", {}).get("primary_line", ""),
    }


def _read_existing_rows(csv_path: str) -> list[dict]:
    """
    Read all rows from the CSV, normalising each row to PREDICTIONS_COLUMNS.
    Extra columns (from a stale schema) are silently dropped; missing columns
    default to an empty string.  Returns [] if the file does not exist or
    cannot be parsed.
    """
    if not os.path.exists(csv_path):
        return []
    rows = []
    try:
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalise: keep only known columns, fill blanks for missing ones
                clean = {col: row.get(col, "") for col in PREDICTIONS_COLUMNS}
                rows.append(clean)
    except Exception:
        pass
    return rows


def log_predictions(prediction_date: str, games: list, csv_path: str) -> dict:
    """
    Upsert one row per game into predictions.csv.

    - If a row with the same (prediction_date, game_pk) already exists it is
      REPLACED with the freshest data from this run.  This means re-running
      run_nrfi.py for the same date always reflects the latest scores and
      lineup info rather than preserving a stale first snapshot.
    - Rows for other dates are preserved unchanged.
    - The file is rewritten atomically (write to a temp path, then rename) so
      a crash mid-write never corrupts the log.

    Returns a small summary dict with counts.
    """
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    # ------------------------------------------------------------------ #
    # 1. Read all existing rows (other dates preserved verbatim)           #
    # ------------------------------------------------------------------ #
    existing_rows = _read_existing_rows(csv_path)

    # Partition: rows for other dates stay as-is; today's rows are replaced
    other_date_rows = [
        r for r in existing_rows
        if r.get("prediction_date") != prediction_date
    ]
    today_existing = {
        str(r.get("game_pk", "")): r
        for r in existing_rows
        if r.get("prediction_date") == prediction_date
    }

    # ------------------------------------------------------------------ #
    # 2. Build fresh rows for every game in this run                       #
    # ------------------------------------------------------------------ #
    new_rows = []
    updated = 0
    added = 0
    skipped_no_pk = 0

    for g in games:
        gpk = g.get("game_pk")
        # game_pk must be a real, non-None, non-empty value
        if gpk is None or str(gpk).strip() == "":
            skipped_no_pk += 1
            continue
        gpk_str = str(gpk)
        row = _row_from_game(prediction_date, g)
        if gpk_str in today_existing:
            updated += 1
        else:
            added += 1
        new_rows.append(row)

    # ------------------------------------------------------------------ #
    # 3. Write: other-date rows first, then today's fresh rows             #
    # ------------------------------------------------------------------ #
    all_rows = other_date_rows + new_rows

    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=PREDICTIONS_COLUMNS,
            extrasaction="ignore",
        )
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    os.replace(tmp_path, csv_path)  # atomic on POSIX

    return {
        "appended": added,
        "updated": updated,
        "skipped_no_pk": skipped_no_pk,
        "total_games": len(games),
        "csv_path": csv_path,
    }
