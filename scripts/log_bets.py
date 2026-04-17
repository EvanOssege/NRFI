#!/usr/bin/env python3
"""
Append/upsert placed bets from a bets_YYYY-MM-DD.json export (produced by the
dashboard's "Export Bets" button) into output/placed_bets.csv.

Key: (date, game_pk, market, line) — re-importing the same file replaces
matching rows for that date so the log stays consistent if you edit selections
in the browser and re-export.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone


PLACED_BETS_COLUMNS = [
    "logged_at",
    "date",
    "game_pk",
    "matchup",
    "market",           # F5_ML | F5_TOTAL | NRFI | YRFI
    "pick",             # "DET", "OVER 4.5", "NRFI", "YRFI"
    "line",             # numeric for F5_TOTAL, blank otherwise
    "units",
    "odds",
    "unit_size_dollars",
    "wager_dollars",
    "model_confidence",
    "model_units",
    "model_score_or_edge",
    "result",           # WIN | LOSS | PUSH | NO_GRADE  (blank until graded)
    "units_pl",         # signed net units (blank until graded)
    "dollars_pl",       # signed net dollars (blank until graded)
]

VALID_MARKETS = {"F5_ML", "F5_TOTAL", "NRFI", "YRFI"}


def _row_key(row: dict) -> tuple:
    line_val = str(row.get("line", "")).strip()
    return (
        str(row.get("date", "")).strip(),
        str(row.get("game_pk", "")).strip(),
        str(row.get("market", "")).strip(),
        line_val,
    )


def _read_existing(csv_path: str) -> list[dict]:
    if not os.path.exists(csv_path):
        return []
    rows = []
    try:
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({c: r.get(c, "") for c in PLACED_BETS_COLUMNS})
    except Exception:
        pass
    return rows


def _fmt_line(line):
    if line is None or line == "":
        return ""
    try:
        v = float(line)
        # keep one decimal for F5 totals (4.0, 4.5, etc.)
        if v.is_integer():
            return f"{v:.1f}"
        return str(v)
    except (TypeError, ValueError):
        return str(line)


def _fmt_num(v, precision=2):
    if v is None or v == "":
        return ""
    try:
        return f"{float(v):.{precision}f}"
    except (TypeError, ValueError):
        return ""


def _bet_to_row(bet: dict, date: str, unit_size: float, logged_at: str) -> dict | None:
    market = (bet.get("market") or "").strip().upper()
    if market not in VALID_MARKETS:
        return None
    game_pk = bet.get("game_pk")
    if game_pk is None or str(game_pk).strip() == "":
        return None

    units = float(bet.get("units") or 0)
    odds = bet.get("odds")
    try:
        odds = int(float(odds))
    except (TypeError, ValueError):
        odds = -110

    wager = units * unit_size

    return {
        "logged_at": logged_at,
        "date": date,
        "game_pk": str(game_pk),
        "matchup": bet.get("matchup", "") or "",
        "market": market,
        "pick": bet.get("pick", "") or "",
        "line": _fmt_line(bet.get("line")),
        "units": _fmt_num(units, 2),
        "odds": str(odds),
        "unit_size_dollars": _fmt_num(unit_size, 2),
        "wager_dollars": _fmt_num(wager, 2),
        "model_confidence": bet.get("model_confidence", "") or "",
        "model_units": _fmt_num(bet.get("model_units"), 2),
        "model_score_or_edge": _fmt_num(bet.get("model_score_or_edge"), 2),
        "result": "",
        "units_pl": "",
        "dollars_pl": "",
    }


def log_bets(json_path: str, csv_path: str | None = None) -> dict:
    """Upsert all bets from a bets_*.json export into placed_bets.csv."""
    with open(json_path, "r") as f:
        payload = json.load(f)

    date = payload.get("date")
    if not date:
        raise ValueError(f"{json_path}: missing 'date' field")
    unit_size = float(payload.get("unit_size_dollars") or 0)
    bets = payload.get("bets") or []
    if not isinstance(bets, list):
        raise ValueError(f"{json_path}: 'bets' must be a list")

    if csv_path is None:
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        csv_path = os.path.join(project_root, "output", "placed_bets.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    logged_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Build fresh rows from the export
    fresh_rows = []
    skipped = 0
    for bet in bets:
        row = _bet_to_row(bet, date, unit_size, logged_at)
        if row is None:
            skipped += 1
            continue
        fresh_rows.append(row)

    # Upsert: keep rows whose key is NOT in fresh_rows, replace those that match
    existing = _read_existing(csv_path)
    fresh_keys = {_row_key(r) for r in fresh_rows}

    kept = [r for r in existing if _row_key(r) not in fresh_keys]
    updated = len(existing) - len(kept)
    added = len(fresh_rows) - updated

    all_rows = kept + fresh_rows
    # Sort by date asc, then matchup for readability
    all_rows.sort(key=lambda r: (r.get("date", ""), r.get("matchup", ""), r.get("market", "")))

    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PLACED_BETS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)
    os.replace(tmp_path, csv_path)

    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "total": len(fresh_rows),
        "csv_path": csv_path,
        "date": date,
    }


def main(argv=None):
    argv = argv or sys.argv[1:]
    if not argv:
        print("Usage: python scripts/log_bets.py <path-to-bets.json>")
        return 1
    json_path = argv[0]
    if not os.path.exists(json_path):
        print(f"Error: file not found: {json_path}")
        return 1
    result = log_bets(json_path)
    print(
        f"Logged bets for {result['date']}: "
        f"{result['added']} added, {result['updated']} updated, "
        f"{result['skipped']} skipped → {result['csv_path']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
