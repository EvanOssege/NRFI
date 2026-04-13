#!/usr/bin/env python3
"""
NRFI Odds Fetcher
==================
Pulls first-inning over/under (NRFI/YRFI) odds from The Odds API.

Strategy:
  1. One bulk call to get all MLB events + their IDs (cheap).
  2. Per-event calls to fetch the first-inning totals market, which is
     only available on the event-level endpoint.

The Odds API counts "requests used" based on market-regions, not raw HTTP
calls.  Event-level fetches for a single market + single region cost very
little quota.  With ~15 games/day and 500 free requests/month, daily use
fits easily.

Setup:
  1. Get a free API key at https://the-odds-api.com
  2. Either:  export ODDS_API_KEY=your_key_here
     — or add ODDS_API_KEY=your_key_here to a .env file in the project root.

If no key is set, all functions return graceful empty results.
"""

import json
import os
import requests
from datetime import datetime
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT = "baseball_mlb"
REGIONS = "us"                           # US-licensed books
BOOKMAKER = "fanduel"                    # primary book to display
ODDS_FORMAT = "american"                 # +150 / -180 style
FI_MARKET = "totals_1st_1_innings"       # first-inning over/under


def _get_api_key() -> Optional[str]:
    """
    Look for the API key in environment variables.
    Also checks for a .env file in the project root as a convenience.
    """
    key = os.environ.get("ODDS_API_KEY")
    if key:
        return key.strip()

    # Try loading from .env in project root
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ODDS_API_KEY="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
        except Exception:
            pass
    return None


def _cache_path(game_date: str) -> str:
    """Path to the daily odds cache file."""
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "output")
    os.makedirs(output_dir, exist_ok=True)
    return os.path.join(output_dir, f"odds_cache_{game_date}.json")


def _load_cache(game_date: str) -> Optional[dict]:
    """
    Load cached odds if they exist and are fresh enough.
    Returns None if no cache or cache is stale.

    TTL: 2 hours when odds were found, 15 minutes when empty (so we
    retry soon in case lines get posted closer to game time).
    """
    path = _cache_path(game_date)
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            cached = json.load(f)
        fetched_at = datetime.fromisoformat(cached.get("fetched_at", ""))
        odds = cached.get("odds", {})
        age_minutes = (datetime.now() - fetched_at).total_seconds() / 60
        max_age = 120 if odds else 15  # 2hr with data, 15min if empty
        if age_minutes > max_age:
            return None
        print(f"  Odds cache hit ({int(age_minutes)}m old, {len(odds)} games) — no API calls used")
        return odds
    except Exception:
        return None


def _save_cache(game_date: str, odds: dict):
    """Persist odds to disk so re-runs don't cost API quota."""
    path = _cache_path(game_date)
    try:
        with open(path, "w") as f:
            json.dump({"fetched_at": datetime.now().isoformat(), "odds": odds}, f, indent=2)
    except Exception:
        pass


def fetch_nrfi_odds(game_date: str, force_refresh: bool = False) -> dict:
    """
    Fetch first-inning over/under odds for all MLB games.

    Uses a 2-hour daily cache so re-runs don't burn API quota.
    Pass force_refresh=True to bypass the cache.

    Two-phase approach:
      Phase 1: bulk-fetch events (1 API call) to get event IDs + team names.
      Phase 2: per-event fetch of the first-inning totals market.

    Returns a dict keyed by normalized matchup ("AWAY @ HOME"):
        {
            "NYY @ BOS": {
                "has_odds": True,
                "book": "fanduel",
                "nrfi_price": -142,
                "yrfi_price": +120,
                "nrfi_implied_prob": 0.587,
                "point": 0.5,
            },
            ...
        }

    Returns {} if no API key, API errors, or no data available.
    """
    # Check cache first
    if not force_refresh:
        cached = _load_cache(game_date)
        if cached is not None:
            return cached

    api_key = _get_api_key()
    if not api_key:
        return {}

    # Phase 1: get all MLB events (use cheap 'h2h' market just to list events)
    try:
        r = requests.get(
            f"{ODDS_API_BASE}/sports/{SPORT}/odds",
            params={
                "apiKey": api_key,
                "regions": REGIONS,
                "markets": "h2h",
                "oddsFormat": ODDS_FORMAT,
            },
            timeout=15,
        )
        r.raise_for_status()
        events = r.json()
        remaining = r.headers.get("x-requests-remaining", "?")
        print(f"  Odds API: found {len(events)} events ({remaining} requests remaining)")
    except Exception as e:
        print(f"  ⚠ Odds event list failed: {e}")
        return {}

    if not events:
        return {}

    # Phase 2: per-event fetch of first-inning totals
    results = {}
    fetched = 0
    matched = 0

    for event in events:
        event_id = event.get("id")
        away_full = event.get("away_team", "")
        home_full = event.get("home_team", "")
        away = _normalize_team(away_full)
        home = _normalize_team(home_full)
        if not event_id or not away or not home:
            continue

        try:
            r2 = requests.get(
                f"{ODDS_API_BASE}/sports/{SPORT}/events/{event_id}/odds",
                params={
                    "apiKey": api_key,
                    "regions": REGIONS,
                    "markets": FI_MARKET,
                    "oddsFormat": ODDS_FORMAT,
                    # No bookmaker filter — grab all US books
                },
                timeout=10,
            )
            fetched += 1

            if r2.status_code != 200:
                continue

            data = r2.json()

            # Collect valid lines from all bookmakers, prefer FanDuel
            best = None
            for bm in data.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") != FI_MARKET:
                        continue

                    over_price = None
                    under_price = None
                    point = None

                    for outcome in market.get("outcomes", []):
                        name = outcome.get("name", "").lower()
                        price = outcome.get("price")
                        pt = outcome.get("point")
                        if name == "over":
                            over_price = price
                            point = pt
                        elif name == "under":
                            under_price = price
                            point = pt

                    if under_price is not None:
                        entry = {
                            "has_odds": True,
                            "book": bm["key"],
                            "nrfi_price": under_price,
                            "yrfi_price": over_price,
                            "nrfi_implied_prob": _american_to_prob(under_price),
                            "point": point,
                        }
                        # FanDuel always wins; otherwise keep first found
                        if bm["key"] == BOOKMAKER:
                            best = entry
                            break
                        elif best is None:
                            best = entry

            if best is not None:
                results[f"{away} @ {home}"] = best
                matched += 1

        except Exception:
            continue

    # Log final quota
    try:
        final_remaining = r2.headers.get("x-requests-remaining", "?")
        print(f"  Odds API: queried {fetched} events, {matched} with NRFI lines ({final_remaining} requests remaining)")
    except Exception:
        print(f"  Odds API: queried {fetched} events, {matched} with NRFI lines")

    # Cache results. When we found lines, cache for the full 2-hour window.
    # When empty, still cache but with a short TTL (saves to same file;
    # staleness check in _load_cache handles expiry).
    _save_cache(game_date, results)

    return results


def match_odds_to_games(odds: dict, games: list) -> list:
    """
    Attach odds to game results by matching on team abbreviations.

    Mutates each game dict in-place, adding an 'odds' key.
    Returns the games list for convenience.
    """
    if not odds:
        for g in games:
            g["odds"] = {"has_odds": False}
        return games

    for g in games:
        away_abbr = g.get("away_abbr", "")
        home_abbr = g.get("home_abbr", "")

        matched = None
        for odds_key, odds_data in odds.items():
            odds_away, _, odds_home = odds_key.partition(" @ ")
            if (_team_matches(away_abbr, odds_away) and
                    _team_matches(home_abbr, odds_home)):
                matched = odds_data
                break

        g["odds"] = matched or {"has_odds": False}

    return games


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEAM_ABBR_MAP = {
    "arizona diamondbacks": "ARI",
    "atlanta braves": "ATL",
    "baltimore orioles": "BAL",
    "boston red sox": "BOS",
    "chicago cubs": "CHC",
    "chicago white sox": "CWS",
    "cincinnati reds": "CIN",
    "cleveland guardians": "CLE",
    "colorado rockies": "COL",
    "detroit tigers": "DET",
    "houston astros": "HOU",
    "kansas city royals": "KC",
    "los angeles angels": "LAA",
    "los angeles dodgers": "LAD",
    "miami marlins": "MIA",
    "milwaukee brewers": "MIL",
    "minnesota twins": "MIN",
    "new york mets": "NYM",
    "new york yankees": "NYY",
    "oakland athletics": "OAK",
    "philadelphia phillies": "PHI",
    "pittsburgh pirates": "PIT",
    "san diego padres": "SD",
    "san francisco giants": "SF",
    "seattle mariners": "SEA",
    "st. louis cardinals": "STL",
    "tampa bay rays": "TB",
    "texas rangers": "TEX",
    "toronto blue jays": "TOR",
    "washington nationals": "WSH",
}


def _normalize_team(name: str) -> str:
    """Convert a full team name to its abbreviation."""
    return _TEAM_ABBR_MAP.get(name.lower().strip(), name)


def _team_matches(abbr: str, odds_name: str) -> bool:
    """Check if an abbreviation matches an odds team name."""
    if not abbr or not odds_name:
        return False
    if abbr.upper() == odds_name.upper():
        return True
    normalized = _TEAM_ABBR_MAP.get(odds_name.lower().strip(), "")
    return abbr.upper() == normalized.upper()


def _american_to_prob(american_odds: int) -> Optional[float]:
    """
    Convert American odds to implied probability.
    -150 → 0.600  (favorites)
    +130 → 0.435  (underdogs)
    """
    if american_odds is None:
        return None
    try:
        odds = int(american_odds)
        if odds < 0:
            return abs(odds) / (abs(odds) + 100)
        else:
            return 100 / (odds + 100)
    except (TypeError, ValueError):
        return None


def format_american_odds(price: int) -> str:
    """Format American odds with +/- prefix."""
    if price is None:
        return "—"
    return f"+{price}" if price > 0 else str(price)
