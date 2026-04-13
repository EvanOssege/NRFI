# NRFI + F5 Analyzer

MLB betting analysis system covering NRFI (No Run First Inning) and F5 (First 5 Innings) markets. Scores every game daily for first-inning shutout likelihood (0-100 scale) and F5 Moneyline, Spread, and Total predictions.

## Quick Start

```bash
python run_nrfi.py              # today's games
python run_nrfi.py 2026-04-15   # specific date
```

Outputs JSON + interactive HTML dashboard to `output/`.

## Project Structure

- `run_nrfi.py` — entry point: runs analysis, saves JSON, logs predictions, generates dashboard
- `scripts/nrfi_analyzer.py` — all data fetching + scoring logic (~1800 lines), imports f5_analyzer
- `scripts/f5_analyzer.py` — F5 (First 5 Innings) scoring: ML, Spread, Total predictions
- `scripts/dashboard.py` — HTML dashboard generator (NRFI + F5)
- `scripts/odds.py` — FanDuel NRFI odds fetcher via The Odds API (optional, needs API key)
- `scripts/predictions_log.py` — append-only CSV log of daily predictions (NRFI + F5 columns)
- `scripts/backtest.py` — backtesting framework
- `output/` — daily `nrfi_YYYY-MM-DD.json`, `nrfi_dashboard_YYYY-MM-DD.html`, `predictions.csv`, `outcomes.csv`

## Data Sources

- **MLB Stats API** (`statsapi.mlb.com/api/v1`) — schedule, rosters, pitcher/batter stats, game logs, linescores, boxscores. Free, no key.
- **Open-Meteo** — weather forecasts for outdoor parks. Free, no key.
- **The Odds API** (`api.the-odds-api.com/v4`) — FanDuel NRFI odds (first-inning over/under 0.5). Free tier: 500 req/month. Optional — set `ODDS_API_KEY` env var or add to `.env` file. Dashboard displays odds for value comparison but they are **not** factored into the scoring algorithm.
- Helper: `mlb_get(endpoint, params)` wraps all MLB API calls.

## Scoring Algorithm

### Final Score (0-100)

```
raw = 0.45 * pitcher_component + 0.25 * lineup_component + adjustments
```

**Confidence tiers:** STRONG (>=72), LEAN (62-71.9), TOSS-UP (50-61.9), FADE (<50)

### Pitcher Component

- Each pitcher scored 0-100 via `score_pitcher()` using ERA, WHIP, K/9, BB/9, HR/9 bucket adjustments from a baseline of 50.
- **Sample-size regression:** score is blended toward 50 based on games started (`confidence = min(1.0, GS / 10)`). With 2 starts, only 20% of the raw adjustment is applied. This prevents small-sample rate stats from dominating early in the season.
- Component = `0.4 * avg(both pitchers) + 0.6 * min(both pitchers)` — weakest-link weighted because one bad pitcher can blow NRFI.

### First-Inning Adjustment (-12 to +12)

`score_first_inning()` — applied per pitcher, scaled by confidence (`min(1.0, fi_starts / 15)`).

- **Primary metric: clean-inning percentage (+-8)** — binary "did a run score?" directly mirrors what NRFI measures. More stable than ERA for this purpose.
- **Secondary metric: first-inning ERA (+-3)** — captures run-environment risk that clean% alone misses.
- **Tertiary metric: baserunner pressure via hits per FI (+-2)** — a pitcher with high clean% but lots of hits is "clean but messy" and surviving on sequencing luck. Low hits/FI signals true dominance. League average is ~1.0 hits per first inning. Uses hits from linescores (already fetched) as a zero-extra-API-cost proxy for total baserunner traffic.
- **Recency weighting:** current-season starts weighted 1.0, prior-season starts weighted 0.5. This ensures a pitcher's current form has more influence without discarding historical data entirely. Prior-season data is only pulled if the pitcher has <5 current-season starts.

### Lineup Threat Component

- Top 4 batters scored via `score_lineup_threat()` (OBP, SLG, OPS, HR rate, K rate).
- Component = `100 - (0.4 * avg_threat + 0.6 * max_threat)` — worst lineup drives the score.
- **Lineup estimation fallback:** when official lineups aren't posted: (1) try the batting order from the team's most recent game vs a same-handed starter (searched up to 14 days back via `_get_last_lineup_vs_hand()`), (2) fall back to roster sorted by PA. Same-hand lookup is preferred because managers construct lineups around pitcher handedness and the order is sticky day-to-day.

### Adjustments (added to raw score)

- **Batter vs Pitcher (BvP):** -10 to +10. Weighted OPS from historical matchups, confidence-scaled by at-bats.
- **Platoon splits:** -6 to +6. How the lineup performs vs the starter's handedness.
- **Streak/form:** -6 to +6. Recent 7-game OPS delta vs season OPS.
- **Team tendency:** rolling 30-game window of first-inning scoring rate, shrunk toward 27% league baseline, home/away split-aware.
- **Park factor:** -8 to +8. `(100 - park_factor) * 0.6`.
- **Weather:** -10 to +10. Temperature + park-aware wind direction. Uses `PARK_ORIENTATION` (compass degrees to center field) and Open-Meteo `winddirection_10m` to compute whether wind is blowing out (bad — ball carries), blowing in (good — suppresses fly balls), or crosswind (neutral). Replaces the old speed-only wind logic.
- **Pitcher rest:** -3 to +3 per pitcher (total -6 to +6). `score_rest()` evaluates days since last start and pitch count from that outing. Short rest (≤4 days) with high workload (100+ pitches) is penalized; standard 5-day rest is neutral; extra rest (6 days) is a slight positive.

## Key Functions (nrfi_analyzer.py)

| Function | Line | Purpose |
|---|---|---|
| `get_todays_games()` | ~179 | Fetch schedule for a date |
| `parse_game_info()` | ~196 | Extract structured game info |
| `get_pitcher_season_stats()` | ~243 | Season pitching stats |
| `get_pitcher_hand()` | ~277 | Throwing hand lookup |
| `get_pitcher_rest_and_workload()` | ~289 | Days rest + pitch count from last start |
| `score_rest()` | ~355 | Rest/workload adjustment (-3 to +3) |
| `get_first_inning_stats()` | ~395 | First-inning ERA + clean% with recency weighting |
| `score_first_inning()` | ~377 | First-inning adjustment scoring |
| `get_team_first_inning_tendency()` | ~491 | Team 30-game FI scoring rate |
| `get_batter_season_stats()` | ~690 | Season batting stats |
| `_get_last_lineup_vs_hand()` | ~710 | Lineup from last game vs same-hand SP |
| `get_top_of_order()` | ~800 | Top 4 batters with 3-tier fallback |
| `get_batter_platoon_splits()` | ~815 | L/R split stats |
| `score_bvp()` | ~1174 | Batter vs pitcher matchup scoring |
| `score_pitcher()` | ~1283 | Pitcher 0-100 score with sample-size regression |
| `score_lineup_threat()` | ~1395 | Lineup threat 0-100 |
| `compute_nrfi_score()` | ~1570 | Final score assembly |
| `analyze_date()` | ~1650 | Main entry: orchestrates full analysis for a date |

## F5 (First 5 Innings) Scoring — `f5_analyzer.py`

The F5 module consumes the same per-game data that the NRFI pipeline produces and adds three predictions:

### F5 Moneyline

Per-side power rating: `0.70 * pitcher_score + 0.30 * (100 - lineup_threat) + adjustments`. The differential between away and home ratings determines pick, edge, and confidence tier (STRONG / MODERATE / LEAN / TOSS-UP).

Key difference from NRFI: pitcher quality is weighted at 70% (vs 45% in NRFI) because over 5 innings the starting pitcher's dominance is the single strongest predictor.

### F5 Spread

Derived from the ML edge. Edge ≥1 supports -0.5 (essentially ML). Edge ≥5 supports -1.5 coverage (multi-run lead). Confidence: STRONG / LEAN / SLIGHT / TOSS-UP.

### F5 Total

Projects runs per side via `_estimate_f5_runs()` using pitcher quality vs opposing lineup, then sums with park/weather/tendency adjustments. Baseline: 2.25 runs per side (4.5 total). Compared against common lines (4.0, 4.5, 5.0, 5.5). Each line gets an OVER/UNDER/PUSH call with confidence.

### Key Functions (f5_analyzer.py)

| Function | Purpose |
|---|---|
| `compute_f5_power_rating()` | Per-side strength rating for ML/spread |
| `compute_f5_moneyline()` | F5 ML pick and edge |
| `compute_f5_spread()` | F5 spread coverage from ML edge |
| `_estimate_f5_runs()` | Project runs for one side over 5 innings |
| `compute_f5_total()` | F5 total projection vs common lines |
| `compute_f5_scores()` | Main entry: returns `{ml, spread, total}` |

## Design Decisions

- **Conservative by design:** the system is meant to surface 1-3 high-confidence picks per day, not bet every game. STRONG tier (>=72) is deliberately hard to reach.
- **Weakest-link weighting:** both pitcher and lineup components weight the worse side at 60% because NRFI only needs one team to score.
- **Sample-size awareness:** pitcher scores regress toward 50 with few starts; first-inning adjustments scale by confidence. This prevents early-season noise from generating false confidence.
- **Free data only:** no paid APIs. MLB Stats API and Open-Meteo cover everything needed.

## When Making Changes

- The scoring engine is in `nrfi_analyzer.py`. All weights, thresholds, and bucket boundaries are inline (no config file).
- The `output/predictions.csv` log is append-only and idempotent on game_pk — safe to re-run dates.
- Line numbers in the table above are approximate; use function names to navigate.
