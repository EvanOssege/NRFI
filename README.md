# NRFI + F5 Analyzer

MLB betting analysis system for **NRFI** (No Run First Inning) and **F5** (First 5 Innings) markets. Scores every game daily on a 0–100 scale and outputs picks with confidence tiers.

## Setup

**Requirements:** Python 3.8+

```bash
pip install -r requirements.txt
```

**Optional — FanDuel NRFI odds** (500 req/month free tier):

```bash
# Add to .env file or export in shell
ODDS_API_KEY=your_key_here
```

## Running

```bash
# Today's games
python run_nrfi.py

# Specific date
python run_nrfi.py 2026-04-15

# Force fresh odds pull (bypasses 2hr cache)
python run_nrfi.py --refresh-odds

# Or use npm scripts
npm run analyze
npm run analyze:odds
```

Outputs to `output/`:
- `nrfi_YYYY-MM-DD.json` — raw scores and game data
- `nrfi_dashboard_YYYY-MM-DD.html` — interactive dashboard
- `predictions.csv` — append-only predictions log

## Backtesting

```bash
# Update outcomes + full calibration report
python scripts/backtest.py

# Just fetch new outcomes
python scripts/backtest.py --update-only

# Report only (no fetching)
python scripts/backtest.py --report-only

# Or use npm scripts
npm run backtest
npm run backtest:update
npm run backtest:report
```

## Confidence Tiers

| Tier | Score | Meaning |
|------|-------|---------|
| STRONG | ≥ 72 | High confidence NRFI |
| LEAN | 62–71.9 | Slight edge |
| TOSS-UP | 50–61.9 | No clear edge |
| FADE | < 50 | Run likely in the 1st |

## Data Sources

- **MLB Stats API** — schedule, rosters, pitcher/batter stats (free, no key)
- **Open-Meteo** — weather forecasts for outdoor parks (free, no key)
- **The Odds API** — FanDuel NRFI odds, optional (free tier: 500 req/month)

