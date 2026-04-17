#!/usr/bin/env python3
"""
NRFI Daily Runner
==================
One-command script: fetches data, scores games, generates dashboard.

Usage:
  python run_nrfi.py                              # today's games
  python run_nrfi.py 2026-04-15                   # specific date
  python run_nrfi.py --refresh-odds               # force fresh odds pull (bypass 2hr cache)
  python run_nrfi.py 2026-04-15 --refresh-odds
  python run_nrfi.py --log-bets <bets.json>       # log placed bets from dashboard export
"""

import sys
import os
import glob
from datetime import date, timedelta

# Ensure scripts dir is on path
script_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, script_dir)

from nrfi_analyzer import analyze_date
from dashboard import generate_dashboard
from predictions_log import log_predictions
from odds import fetch_nrfi_odds, match_odds_to_games
from hit_rate_tracker import generate_hit_rate_dashboard
from unit_sizing import compute_unit_sizing
import json
from datetime import datetime


def _stats_through_for_date(target_date: str) -> str:
    """Return day-1 cutoff date used for historical feature inputs."""
    try:
        d = date.fromisoformat(target_date)
    except Exception:
        d = date.today()
    return (d - timedelta(days=1)).isoformat()


def main():
    raw_argv = sys.argv[1:]

    # --- --log-bets <path>: short-circuit, log and exit ---
    if "--log-bets" in raw_argv:
        idx = raw_argv.index("--log-bets")
        if idx + 1 >= len(raw_argv):
            print("Error: --log-bets requires a path argument")
            print("Usage: python run_nrfi.py --log-bets <path-to-bets.json>")
            sys.exit(1)
        from log_bets import log_bets as _log_bets
        bets_path = raw_argv[idx + 1]
        if not os.path.exists(bets_path):
            print(f"Error: file not found: {bets_path}")
            sys.exit(1)
        result = _log_bets(bets_path)
        print(
            f"Logged bets for {result['date']}: "
            f"{result['added']} added, {result['updated']} updated, "
            f"{result['skipped']} skipped → {result['csv_path']}"
        )
        # Refresh hit rate tracker so new bets + any existing outcomes show up
        try:
            output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
            predictions_csv = os.path.join(output_dir, "predictions.csv")
            outcomes_csv = os.path.join(output_dir, "outcomes.csv")
            tracker_path = os.path.join(output_dir, "hit_rate_tracker.html")
            generate_hit_rate_dashboard(predictions_csv, outcomes_csv, tracker_path)
            print(f"Hit rate tracker refreshed: {tracker_path}")
        except Exception as e:
            print(f"(tracker refresh skipped: {e})")
        return

    args = [a for a in raw_argv if not a.startswith("--")]
    flags = [a for a in raw_argv if a.startswith("--")]
    target_date = args[0] if args else date.today().isoformat()
    refresh_odds = "--refresh-odds" in flags

    # 1. Run analysis
    results = analyze_date(target_date)

    if not results:
        print("\nNo games to analyze. Exiting.")
        return

    # 1b. Fetch betting odds (optional — requires ODDS_API_KEY)
    print("\nFetching NRFI odds...")
    odds = fetch_nrfi_odds(target_date, force_refresh=refresh_odds)
    if odds:
        match_odds_to_games(odds, results)
        matched = sum(1 for g in results if g.get("odds", {}).get("has_odds"))
        print(f"  Matched odds for {matched}/{len(results)} games")
    else:
        for g in results:
            g["odds"] = {"has_odds": False}
        print("  No odds available (set ODDS_API_KEY to enable)")

    # 1c. Suggested unit sizing per bet (F5 ML + F5 Total)
    compute_unit_sizing(results)

    # 2. Save JSON
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)

    data = {
        "date": target_date,
        "stats_through": _stats_through_for_date(target_date),
        "generated": datetime.now().isoformat(),
        "games": results,
    }

    json_path = os.path.join(output_dir, f"nrfi_{target_date}.json")
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nJSON saved: {json_path}")

    # 2b. Upsert predictions log — re-runs update existing rows for the same date
    predictions_csv = os.path.join(output_dir, "predictions.csv")
    log_summary = log_predictions(target_date, results, predictions_csv)
    parts = []
    if log_summary["appended"]:
        parts.append(f"+{log_summary['appended']} new")
    if log_summary["updated"]:
        parts.append(f"~{log_summary['updated']} updated")
    if log_summary["skipped_no_pk"]:
        parts.append(f"{log_summary['skipped_no_pk']} skipped (no game_pk)")
    print(f"Predictions log: {', '.join(parts) or 'no changes'} -> {predictions_csv}")

    # 3. Generate dashboard
    dash_path = os.path.join(output_dir, f"nrfi_dashboard_{target_date}.html")
    generate_dashboard(data, dash_path)

    # 3b. Generate hit rate tracker
    tracker_path = os.path.join(output_dir, "hit_rate_tracker.html")
    outcomes_csv = os.path.join(output_dir, "outcomes.csv")
    generate_hit_rate_dashboard(predictions_csv, outcomes_csv, tracker_path)
    print(f"Hit rate tracker: {tracker_path}")

    # 4. Print summary
    print(f"\n{'='*60}")
    print(f"  TOP PICKS — {target_date}")
    print(f"{'='*60}")

    # --- NRFI Picks ---
    picks = [g for g in results if g["nrfi"]["tier"] in ("STRONG", "LEAN")]
    print(f"\n  NRFI")
    print(f"  {'─'*40}")
    if picks:
        for g in picks:
            n = g["nrfi"]
            emoji = "🟢" if n["tier"] == "STRONG" else "🟡"
            print(f"  {emoji} {g['matchup']:12s}  Score: {n['score']:5.1f}  ({n['tier']})")
            print(f"     {g['away_pitcher']} vs {g['home_pitcher']}")
            print(f"     {g['venue']} · Weather: ", end="")
            w = g['weather']
            if w.get('indoor'):
                print("Indoor")
            else:
                parts = []
                if w.get('temp_f') is not None: parts.append(f"{w['temp_f']:.0f}°F")
                if w.get('wind_mph') is not None: parts.append(f"{w['wind_mph']:.0f}mph wind")
                print(" · ".join(parts) if parts else "N/A")
            print()
    else:
        print("  No strong/lean NRFI picks today.")

    # --- F5 Moneyline Picks ---
    f5_ml_picks = [g for g in results
                   if g.get("f5", {}).get("ml", {}).get("confidence") in ("STRONG", "MODERATE")]
    print(f"\n  F5 MONEYLINE")
    print(f"  {'─'*40}")
    if f5_ml_picks:
        for g in f5_ml_picks:
            ml = g["f5"]["ml"]
            emoji = "🟢" if ml["confidence"] == "STRONG" else "🟡"
            print(f"  {emoji} {g['matchup']:12s}  Pick: {ml['pick']} (edge {ml['edge']:+.1f}, {ml['confidence']})")
            print(f"     {g['away_pitcher']} vs {g['home_pitcher']}")
    else:
        print("  No strong F5 ML picks today.")

    # --- F5 Total Picks ---
    f5_total_picks = [g for g in results
                      if g.get("f5", {}).get("total", {}).get("confidence") in ("STRONG", "LEAN")]
    print(f"\n  F5 TOTAL")
    print(f"  {'─'*40}")
    if f5_total_picks:
        for g in f5_total_picks:
            t = g["f5"]["total"]
            emoji = "⬆️" if t["lean"] == "OVER" else "⬇️"
            print(f"  {emoji} {g['matchup']:12s}  Proj: {t['projected_total']} ({t['lean']} {t['primary_line']}, {t['confidence']})")
            print(f"     {g['away_pitcher']} vs {g['home_pitcher']}")
    else:
        print("  No strong F5 total picks today.")

    # --- F5 Spread Picks ---
    f5_spread_picks = [g for g in results
                       if g.get("f5", {}).get("spread", {}).get("confidence") in ("STRONG", "LEAN")]
    print(f"\n  F5 SPREAD")
    print(f"  {'─'*40}")
    if f5_spread_picks:
        for g in f5_spread_picks:
            s = g["f5"]["spread"]
            print(f"  ► {g['matchup']:12s}  {s['recommended_label']} ({s['confidence']})")
            print(f"     {g['away_pitcher']} vs {g['home_pitcher']}")
    else:
        print("  No strong F5 spread picks today.")

    hub_path = os.path.join(output_dir, "index.html")
    print(f"\n{'─'*60}")
    print(f"Dashboard: {dash_path}")
    print(f"Hub:       {hub_path}")

    # 5. Clean up output files older than 5 days
    cutoff = date.today() - timedelta(days=5)
    removed = []
    for pattern in ("nrfi_*.json", "nrfi_dashboard_*.html"):
        for filepath in glob.glob(os.path.join(output_dir, pattern)):
            fname = os.path.basename(filepath)
            # Extract date string from filename (nrfi_YYYY-MM-DD.json or nrfi_dashboard_YYYY-MM-DD.html)
            try:
                date_str = fname.replace("nrfi_dashboard_", "").replace("nrfi_", "").split(".")[0]
                file_date = date.fromisoformat(date_str)
                if file_date < cutoff:
                    os.remove(filepath)
                    removed.append(fname)
            except (ValueError, IndexError):
                pass  # skip files that don't match expected naming
    if removed:
        print(f"Cleaned up {len(removed)} old output file(s): {', '.join(removed)}")


if __name__ == "__main__":
    main()
