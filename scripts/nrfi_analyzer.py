#!/usr/bin/env python3
"""
NRFI (No Run First Inning) Analyzer
====================================
Pulls today's MLB schedule, starting pitcher stats, top-of-order batter stats,
and weather data. Scores each game for NRFI probability and outputs an
interactive HTML dashboard.

Data Sources (all free, no API keys):
  - MLB Stats API (statsapi.mlb.com) — schedule, rosters, pitcher/batter stats
  - Open-Meteo — weather at game time

Usage:
  python nrfi_analyzer.py              # analyze today's games
  python nrfi_analyzer.py 2026-04-10   # analyze a specific date
"""

import json
import sys
import os
import math
from datetime import datetime, date, timedelta
from typing import Optional
import requests

from f5_analyzer import compute_f5_scores

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
MLB_BASE = "https://statsapi.mlb.com/api/v1"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
DEFAULT_SEASON = date.today().year

# Ballpark coordinates for weather lookups
BALLPARK_COORDS = {
    "Angel Stadium": (33.8003, -117.8827),
    "Busch Stadium": (38.6226, -90.1928),
    "Chase Field": (33.4453, -112.0667),
    "Citi Field": (40.7571, -73.8458),
    "Citizens Bank Park": (39.9061, -75.1665),
    "Comerica Park": (42.3390, -83.0485),
    "Coors Field": (39.7559, -104.9942),
    "Dodger Stadium": (34.0739, -118.2400),
    "Fenway Park": (42.3467, -71.0972),
    "Globe Life Field": (32.7473, -97.0845),
    "Great American Ball Park": (39.0975, -84.5069),
    "Guaranteed Rate Field": (41.8299, -87.6338),
    "Kauffman Stadium": (39.0517, -94.4803),
    "loanDepot park": (25.7781, -80.2196),
    "Minute Maid Park": (29.7573, -95.3555),
    "Nationals Park": (38.8730, -77.0074),
    "Oakland Coliseum": (37.7516, -122.2005),
    "Oracle Park": (37.7786, -122.3893),
    "Oriole Park at Camden Yards": (39.2838, -76.6218),
    "Petco Park": (32.7076, -117.1570),
    "PNC Park": (40.4469, -80.0058),
    "Progressive Field": (41.4962, -81.6852),
    "Rogers Centre": (43.6414, -79.3894),
    "T-Mobile Park": (47.5914, -122.3325),
    "Target Field": (44.9818, -93.2775),
    "Tropicana Field": (27.7682, -82.6534),
    "Truist Park": (33.8911, -84.4680),
    "Wrigley Field": (41.9484, -87.6553),
    "Yankee Stadium": (40.8296, -73.9262),
    # 2026 updates — add new parks here as needed
    "American Family Field": (43.0280, -87.9712),
    "Salt River Fields": (33.5453, -111.8847),  # spring training fallback
}

# Roof status — indoor parks are weather-neutral
INDOOR_PARKS = {
    "Globe Life Field", "Tropicana Field", "loanDepot park",
    "Minute Maid Park", "Chase Field", "Rogers Centre",
    "American Family Field", "T-Mobile Park",
}

# Park Factors — runs-based, normalized to 100 (league average).
# >100 = hitter-friendly, <100 = pitcher-friendly.
# Sources: multi-year ESPN/FanGraphs park factor consensus.
# These shift slowly year-to-year; update annually.
PARK_FACTORS = {
    "Coors Field":                  115,   # most hitter-friendly in MLB
    "Great American Ball Park":     107,
    "Globe Life Field":             106,
    "Fenway Park":                  105,
    "Yankee Stadium":               105,
    "Citizens Bank Park":           104,
    "Wrigley Field":                103,   # wind-dependent but trends hitter-friendly
    "Guaranteed Rate Field":        102,
    "Truist Park":                  102,
    "Citi Field":                   101,
    "Oriole Park at Camden Yards":  101,
    "American Family Field":        101,
    "Target Field":                 101,
    "Rogers Centre":                100,
    "Busch Stadium":                100,
    "Comerica Park":                100,
    "Minute Maid Park":             100,
    "Kauffman Stadium":              99,
    "Dodger Stadium":                99,
    "Chase Field":                   99,
    "PNC Park":                      99,
    "Progressive Field":             98,
    "Tropicana Field":               97,
    "loanDepot park":                97,
    "T-Mobile Park":                 96,
    "Petco Park":                    95,
    "Oracle Park":                   94,
    "Oakland Coliseum":              95,
    "Nationals Park":                99,
}

# Classification thresholds
PARK_HITTER_FRIENDLY = 103   # >= this is hitter-friendly
PARK_PITCHER_FRIENDLY = 97   # <= this is pitcher-friendly

# Ballpark outfield orientation in compass degrees (direction the batter faces).
# Wind blowing FROM roughly the same direction as the batter faces = blowing OUT
# (bad for NRFI — ball carries). Wind blowing toward the batter = blowing IN
# (good for NRFI — suppresses fly balls).
# Source: Clem's Baseball / Ballparks of Baseball / Google Earth measurements.
# Degrees are approximate center-field direction from home plate.
PARK_ORIENTATION = {
    "Angel Stadium":                195,   # SSW
    "Busch Stadium":                210,   # SSW
    "Chase Field":                  185,   # S (indoor — moot, but listed for completeness)
    "Citi Field":                   140,   # SE
    "Citizens Bank Park":           220,   # SW
    "Comerica Park":                195,   # SSW
    "Coors Field":                  200,   # SSW
    "Dodger Stadium":               235,   # SW
    "Fenway Park":                  205,   # SSW
    "Globe Life Field":             195,   # SSW (indoor)
    "Great American Ball Park":     205,   # SSW
    "Guaranteed Rate Field":        210,   # SSW
    "Kauffman Stadium":             190,   # S
    "loanDepot park":               200,   # SSW (indoor)
    "Minute Maid Park":             195,   # SSW (indoor)
    "Nationals Park":               155,   # SSE
    "Oakland Coliseum":             215,   # SW
    "Oracle Park":                  205,   # SSW
    "Oriole Park at Camden Yards":  215,   # SW
    "Petco Park":                   195,   # SSW
    "PNC Park":                     200,   # SSW
    "Progressive Field":            185,   # S
    "Rogers Centre":                195,   # SSW (retractable)
    "T-Mobile Park":                195,   # SSW (retractable)
    "Target Field":                 215,   # SW
    "Tropicana Field":              180,   # S (indoor)
    "Truist Park":                  200,   # SSW
    "Wrigley Field":                215,   # SW
    "Yankee Stadium":               205,   # SSW
    "American Family Field":        200,   # SSW (retractable)
}


def get_park_factor(venue: str) -> dict:
    """Look up the park factor for a venue. Returns dict with factor and label."""
    factor = PARK_FACTORS.get(venue)
    if factor is None:
        # fuzzy match
        for k, v in PARK_FACTORS.items():
            if k.lower() in venue.lower() or venue.lower() in k.lower():
                factor = v
                break
    if factor is None:
        factor = 100  # default to neutral

    if factor >= PARK_HITTER_FRIENDLY:
        label = "Hitter-friendly"
        tier = "hitter"
    elif factor <= PARK_PITCHER_FRIENDLY:
        label = "Pitcher-friendly"
        tier = "pitcher"
    else:
        label = "Neutral"
        tier = "neutral"

    return {"factor": factor, "label": label, "tier": tier}


def score_park(park_info: dict) -> float:
    """
    Park factor adjustment: -8 to +8.
    Positive = pitcher-friendly (good for NRFI).
    Replaces the old binary Coors penalty with a continuous scale.
    """
    factor = park_info["factor"]
    # Center at 100, scale linearly
    # Each point away from 100 = ~0.6 points of adjustment
    adj = (100 - factor) * 0.6
    return round(max(-8, min(8, adj)), 1)


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
def mlb_get(endpoint: str, params: dict = None) -> dict:
    """Hit the MLB Stats API and return JSON."""
    url = f"{MLB_BASE}/{endpoint}"
    r = requests.get(url, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def safe_get(d, *keys, default=None):
    """Nested dict access without KeyError."""
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, default)
        else:
            return default
    return d


def _parse_game_date(game_date: Optional[str]) -> date:
    """Best-effort ISO date parse; falls back to today."""
    if game_date:
        try:
            return date.fromisoformat(game_date)
        except Exception:
            pass
    return date.today()


def _season_for_date(game_date: Optional[str]) -> int:
    """Map a slate date to MLB season year."""
    return _parse_game_date(game_date).year or DEFAULT_SEASON


def _stats_end_date(game_date: Optional[str]) -> str:
    """
    End date used for historical feature computation.
    We stop at day-1 so a replay only uses data known before first pitch.
    """
    return (_parse_game_date(game_date) - timedelta(days=1)).isoformat()


def _first_stat_from_groups(stat_groups: list[dict]) -> dict:
    """Return the first non-empty stat dict from MLB stat-group payloads."""
    for stat_group in stat_groups or []:
        for split in stat_group.get("splits", []):
            stat = split.get("stat", {})
            if stat:
                return stat
    return {}


def _fmt_rate(v: Optional[float]) -> str:
    """Format batting rates as MLB-style strings (e.g. .287, 1.042)."""
    if v is None:
        return ".000"
    s = f"{v:.3f}"
    return s[1:] if s.startswith("0") else s


# ---------------------------------------------------------------------------
# 1. SCHEDULE
# ---------------------------------------------------------------------------
def get_todays_games(game_date: str) -> list[dict]:
    """Return list of game dicts for the given date (YYYY-MM-DD)."""
    data = mlb_get("schedule", {
        "sportId": 1,
        "date": game_date,
        "hydrate": "team,probablePitcher,venue,linescore,lineups",
    })
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            status = safe_get(g, "status", "detailedState", default="")
            if "Postponed" in status or "Cancelled" in status:
                continue
            games.append(g)
    return games


def parse_game_info(game: dict) -> dict:
    """Extract structured info from a schedule game object."""
    away = safe_get(game, "teams", "away", default={})
    home = safe_get(game, "teams", "home", default={})
    venue_name = safe_get(game, "venue", "name", default="Unknown")
    game_time = game.get("gameDate", "")

    # Extract official lineups if available (in batting order)
    lineups = game.get("lineups", {})
    home_lineup_raw = lineups.get("homePlayers", [])
    away_lineup_raw = lineups.get("awayPlayers", [])

    # Convert to simple list of {id, name, position}
    def parse_lineup_players(players):
        return [
            {
                "id": p.get("id"),
                "name": p.get("fullName", "Unknown"),
                "position": safe_get(p, "primaryPosition", "abbreviation", default=""),
            }
            for p in players
        ]

    info = {
        "game_pk": game.get("gamePk"),
        "game_time": game_time,
        "venue": venue_name,
        "is_indoor": venue_name in INDOOR_PARKS,
        "away_team": safe_get(away, "team", "name", default="TBD"),
        "away_abbr": safe_get(away, "team", "abbreviation", default=""),
        "away_pitcher_id": safe_get(away, "probablePitcher", "id"),
        "away_pitcher_name": safe_get(away, "probablePitcher", "fullName", default="TBD"),
        "home_team": safe_get(home, "team", "name", default="TBD"),
        "home_abbr": safe_get(home, "team", "abbreviation", default=""),
        "home_pitcher_id": safe_get(home, "probablePitcher", "id"),
        "home_pitcher_name": safe_get(home, "probablePitcher", "fullName", default="TBD"),
        "home_team_id": safe_get(home, "team", "id"),
        "away_team_id": safe_get(away, "team", "id"),
        "home_lineup": parse_lineup_players(home_lineup_raw),
        "away_lineup": parse_lineup_players(away_lineup_raw),
        "lineups_available": len(home_lineup_raw) > 0 and len(away_lineup_raw) > 0,
        "home_lineup_available": len(home_lineup_raw) > 0,
        "away_lineup_available": len(away_lineup_raw) > 0,
    }
    return info


# ---------------------------------------------------------------------------
# 2. PITCHER STATS
# ---------------------------------------------------------------------------
def get_pitcher_season_stats(pitcher_id: int, game_date: Optional[str] = None,
                             season: Optional[int] = None) -> dict:
    """Fetch pitcher season-to-date stats as of the target slate date."""
    if not pitcher_id:
        return {}
    season = season or _season_for_date(game_date)
    start_date = f"{season}-01-01"
    end_date = _stats_end_date(game_date)

    try:
        # Primary: current season-to-date as of day-1.
        data = mlb_get(f"people/{pitcher_id}/stats", {
            "stats": "byDateRange",
            "group": "pitching",
            "startDate": start_date,
            "endDate": end_date,
        })
        s = _first_stat_from_groups(data.get("stats", []))
        if s:
            return s

        # Fallback: full prior season (available pregame without look-ahead).
        data = mlb_get(f"people/{pitcher_id}", {
            "hydrate": f"stats(group=[pitching],type=[season],season={season - 1})"
        })
        person = data.get("people", [{}])[0]
        s = _first_stat_from_groups(safe_get(person, "stats", default=[]))
        if s:
            return s
    except Exception as e:
        print(f"  ⚠ Could not fetch pitcher {pitcher_id}: {e}")
    return {}


def get_pitcher_hand(pitcher_id: int) -> str:
    """Get pitcher's throwing hand: 'L', 'R', or '?' if unknown."""
    if not pitcher_id:
        return "?"
    try:
        data = mlb_get(f"people/{pitcher_id}")
        person = data.get("people", [{}])[0]
        return safe_get(person, "pitchHand", "code", default="?")
    except Exception:
        return "?"


def _safe_int(v, default=None):
    """Best-effort int conversion for MLB stat payload values."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _innings_pitched_to_outs(ip_raw) -> Optional[int]:
    """
    Convert MLB innings-pitched notation to outs.

    MLB uses baseball notation (e.g. 5.1 = 5 and 1/3, not 5.1 decimal).
    """
    if ip_raw is None:
        return None

    s = str(ip_raw).strip()
    if not s:
        return None

    if "." not in s:
        whole = _safe_int(s)
        return whole * 3 if whole is not None else None

    whole_s, frac_s = s.split(".", 1)
    whole = _safe_int(whole_s)
    if whole is None:
        return None

    outs = whole * 3
    frac = frac_s.strip()
    if not frac or set(frac) == {"0"}:
        return outs

    # Primary MLB notation: .1 = 1 out, .2 = 2 outs.
    if frac[0] == "1":
        return outs + 1
    if frac[0] == "2":
        return outs + 2

    # Defensive handling for decimal-like thirds from alternate feeds.
    if frac.startswith("33") or frac[0] == "3":
        return outs + 1
    if frac.startswith("66") or frac.startswith("67") or frac[0] in ("6", "7"):
        return outs + 2
    return None


# Per-run cache keyed by (pitcher_id, season); cleared in analyze_date().
_pitcher_gamelog_cache: dict = {}


def _get_pitcher_starts_from_gamelog(pitcher_id: int, season: int) -> list[dict]:
    """Fetch and cache a pitcher's starts from MLB game logs."""
    if not pitcher_id:
        return []

    key = (int(pitcher_id), int(season))
    if key in _pitcher_gamelog_cache:
        return _pitcher_gamelog_cache[key]

    starts = []
    try:
        data = mlb_get(f"people/{pitcher_id}/stats", {
            "stats": "gameLog",
            "group": "pitching",
            "season": season,
        })
        for sg in data.get("stats", []):
            for split in sg.get("splits", []):
                stat = split.get("stat", {}) or {}
                games_started = _safe_int(stat.get("gamesStarted"), 0) or 0
                if games_started <= 0:
                    continue

                pitches = stat.get("numberOfPitches")
                if pitches is None:
                    pitches = stat.get("pitchesThrown")

                starts.append({
                    "date": split.get("date", ""),
                    "pitches": _safe_int(pitches),
                    "ip_outs": _innings_pitched_to_outs(stat.get("inningsPitched")),
                })
    except Exception as e:
        print(f"  ⚠ Could not fetch pitcher game logs for {pitcher_id}: {e}")

    starts.sort(key=lambda s: s.get("date", ""), reverse=True)
    _pitcher_gamelog_cache[key] = starts
    return starts


def get_pitcher_rest_and_workload(pitcher_id: int, game_date: str,
                                  season: Optional[int] = None) -> dict:
    """
    Determine how many days since the pitcher's last start and how many
    pitches they threw.  Uses the current-season game log.

    Returns:
        {
            "has_data": bool,
            "days_rest": int | None,
            "last_pitches": int | None,   # pitches thrown in last start
            "last_start_date": str | None, # ISO date
        }
    """
    if not pitcher_id:
        return {"has_data": False}

    try:
        as_of = date.fromisoformat(game_date)
    except Exception:
        as_of = date.today()
    season = season or _season_for_date(game_date)

    starts = _get_pitcher_starts_from_gamelog(pitcher_id, season)
    for s in starts:
        try:
            start_dt = date.fromisoformat(s.get("date", ""))
        except Exception:
            continue
        if start_dt < as_of:
            days_rest = (as_of - start_dt).days
            return {
                "has_data": True,
                "days_rest": days_rest,
                "last_pitches": s.get("pitches"),
                "last_start_date": s.get("date"),
            }

    return {"has_data": False}


PITCH_EFF_LOOKBACK_STARTS = 8


def get_pitcher_pitch_efficiency(pitcher_id: int, game_date: str,
                                 season: Optional[int] = None,
                                 lookback_starts: int = PITCH_EFF_LOOKBACK_STARTS) -> dict:
    """
    Compute recent pitch-count efficiency from game logs.

    Returns a dict with pitches-per-inning (P/IP) summary for recent starts.
    Higher P/IP indicates inefficiency and elevated risk of early exit.
    """
    if not pitcher_id:
        return {"has_data": False}

    as_of = _parse_game_date(game_date)
    season = season or _season_for_date(game_date)
    starts = _get_pitcher_starts_from_gamelog(pitcher_id, season)
    if not starts:
        return {"has_data": False}

    prior_starts = []
    for s in starts:
        try:
            start_dt = date.fromisoformat(s.get("date", ""))
        except Exception:
            continue
        if start_dt < as_of:
            prior_starts.append(s)

    if not prior_starts:
        return {"has_data": False}

    recent_starts = prior_starts[:max(1, lookback_starts)]
    ppi_values = []
    for s in recent_starts:
        pitches = s.get("pitches")
        ip_outs = s.get("ip_outs")
        if pitches is None or ip_outs is None or ip_outs <= 0:
            continue
        ip = ip_outs / 3.0
        ppi_values.append(pitches / ip)

    if not ppi_values:
        return {"has_data": False}

    ppi_sorted = sorted(ppi_values)
    n = len(ppi_sorted)
    mid = n // 2
    if n % 2 == 1:
        median_ppi = ppi_sorted[mid]
    else:
        median_ppi = (ppi_sorted[mid - 1] + ppi_sorted[mid]) / 2.0

    high_ineff = sum(1 for v in ppi_values if v >= 17.5)

    return {
        "has_data": True,
        "starts_sample": n,
        "starts_lookback": len(recent_starts),
        "avg_pitches_per_inning": round(sum(ppi_values) / n, 2),
        "median_pitches_per_inning": round(median_ppi, 2),
        "high_inefficiency_starts": high_ineff,
        "high_inefficiency_rate": round(high_ineff / n, 3),
    }


def score_rest(rest_info: dict) -> float:
    """
    Days-rest adjustment: -3 to +3.
    Positive = well-rested pitcher (good for NRFI).
    Negative = short rest or high recent workload (bad for NRFI).

    Normal rotation is 5 days rest.  Short rest (4) with heavy workload
    is penalized; extra rest (6+) with moderate workload is a slight plus.
    """
    if not rest_info.get("has_data"):
        return 0.0

    days = rest_info.get("days_rest")
    pitches = rest_info.get("last_pitches")
    if days is None:
        return 0.0

    adj = 0.0

    # Days rest component
    if days <= 3:
        adj -= 3.0     # very short rest — rare for starters, big red flag
    elif days == 4:
        adj -= 1.5     # short rest
    elif days == 5:
        adj += 0.0     # standard rotation — neutral
    elif days == 6:
        adj += 0.5     # extra day — slight positive
    else:
        adj += 0.0     # 7+ days could mean injury/return — neutral

    # Workload modifier: heavy last outing amplifies short rest penalty
    if pitches is not None and days is not None and days <= 4:
        if pitches >= 105:
            adj -= 1.5   # high pitch count on short rest
        elif pitches >= 95:
            adj -= 0.5
    # Light workload on normal rest — negligible, no bonus

    return round(max(-3.0, min(3.0, adj)), 1)


def get_first_inning_stats(pitcher_id: int, game_date: Optional[str] = None,
                           season: Optional[int] = None) -> dict:
    """
    Compute first-inning stats by cross-referencing game logs with linescores.
    Uses current season first; if <5 starts, supplements with prior season.
    """
    if not pitcher_id:
        return {"has_data": False}
    as_of = _parse_game_date(game_date)
    season = season or _season_for_date(game_date)

    def fetch_season_starts(target_season: int):
        try:
            data = mlb_get(f"people/{pitcher_id}/stats", {
                "stats": "gameLog",
                "group": "pitching",
                "season": target_season,
            })
            starts = []
            for sg in data.get("stats", []):
                for split in sg.get("splits", []):
                    gs = split.get("stat", {}).get("gamesStarted", 0)
                    if gs > 0:
                        split_date = split.get("date", "")
                        try:
                            start_dt = date.fromisoformat(split_date)
                        except Exception:
                            continue
                        if start_dt >= as_of:
                            continue
                        starts.append({
                            "gpk": split.get("game", {}).get("gamePk"),
                            "isHome": split.get("isHome"),
                            "date": split_date,
                        })
            starts.sort(key=lambda s: s.get("date", ""), reverse=True)
            return starts
        except Exception:
            return []

    # Gather starts: current season + prior season if needed.
    # Tag each start with its season so we can apply recency weighting.
    PRIOR_SEASON_WEIGHT = 0.5  # prior-season starts count half

    cur_starts = fetch_season_starts(season)
    for s in cur_starts:
        s["weight"] = 1.0
    starts = cur_starts
    if len(cur_starts) < 5:
        prior_starts = fetch_season_starts(season - 1)
        for s in prior_starts:
            s["weight"] = PRIOR_SEASON_WEIGHT
        starts += prior_starts

    if not starts:
        return {"has_data": False}

    # Fetch first-inning runs from linescores (cap at 25 most recent starts)
    starts.sort(key=lambda s: s.get("date", ""), reverse=True)
    starts = starts[:25]
    fi_entries = []  # list of (runs, hits, weight) tuples

    for s in starts:
        gpk = s.get("gpk")
        if not gpk:
            continue
        try:
            r = requests.get(f"{MLB_BASE}/game/{gpk}/linescore", timeout=8)
            r.raise_for_status()
            innings = r.json().get("innings", [])
            if not innings:
                continue
            first = innings[0]
            # Home pitcher pitches top of 1st; away pitcher pitches bottom of 1st
            if s["isHome"]:
                runs = first.get("away", {}).get("runs", 0) or 0
                hits = first.get("away", {}).get("hits", 0) or 0
            else:
                runs = first.get("home", {}).get("runs", 0) or 0
                hits = first.get("home", {}).get("hits", 0) or 0
            fi_entries.append((runs, hits, s["weight"]))
        except Exception:
            continue

    if not fi_entries:
        return {"has_data": False}

    total_weight = sum(w for _, _, w in fi_entries)
    weighted_runs = sum(r * w for r, _, w in fi_entries)
    weighted_hits = sum(h * w for _, h, w in fi_entries)
    weighted_clean = sum(w for r, _, w in fi_entries if r == 0)

    total_starts = len(fi_entries)
    total_runs = sum(r for r, _, _ in fi_entries)
    total_hits = sum(h for _, h, _ in fi_entries)
    fi_clean = sum(1 for r, _, _ in fi_entries if r == 0)

    # Weighted metrics used for scoring
    fi_era = (weighted_runs / total_weight) * 9
    clean_pct = weighted_clean / total_weight
    hits_per_fi = weighted_hits / total_weight  # baserunner pressure proxy

    return {
        "has_data": True,
        "fi_era": round(fi_era, 2),
        "fi_runs": total_runs,
        "fi_hits": total_hits,
        "fi_hits_per_fi": round(hits_per_fi, 3),
        "fi_starts": total_starts,
        "fi_clean": fi_clean,
        "fi_clean_pct": round(clean_pct, 3),
    }


def score_first_inning(fi_stats: dict) -> float:
    """
    First-inning adjustment: -10 to +10.
    Positive = pitcher keeps first inning clean (good for NRFI).

    Primary metric: clean-inning percentage (binary: did a run score?).
    This directly mirrors what NRFI bets measure.
    Secondary metric: first-inning ERA (captures run-environment risk
    even when the clean % looks OK — e.g. lots of baserunners).
    """
    if not fi_stats.get("has_data"):
        return 0.0

    adj = 0.0
    fi_era = fi_stats.get("fi_era")
    clean_pct = fi_stats.get("fi_clean_pct")

    # Confidence: more starts = more reliable
    starts = fi_stats.get("fi_starts", 0)
    confidence = min(1.0, starts / 15.0)

    # --- Primary: clean-inning percentage (±8) ---
    if clean_pct is not None:
        if clean_pct >= 0.85:
            adj += 8
        elif clean_pct >= 0.75:
            adj += 5
        elif clean_pct >= 0.65:
            adj += 2
        elif clean_pct >= 0.55:
            adj -= 2
        elif clean_pct >= 0.45:
            adj -= 5
        else:
            adj -= 8

    # --- Secondary: first-inning ERA modifier (±3) ---
    # Captures run-environment risk that clean_pct alone misses.
    # A pitcher with 75% clean rate but a 6.00 fi_era is letting
    # multiple runs through when they DO allow scoring.
    if fi_era is not None:
        if fi_era <= 1.50:
            adj += 3
        elif fi_era <= 3.00:
            adj += 1
        elif fi_era <= 5.00:
            adj += 0
        elif fi_era <= 7.00:
            adj -= 2
        else:
            adj -= 3

    # --- Tertiary: baserunner pressure via hits per first inning (±2) ---
    # A pitcher with high clean% but lots of hits is "clean but messy" —
    # surviving on sequencing luck (stranding runners).  That clean% will
    # regress.  Conversely, low hits/FI signals true dominance.
    # League-average is roughly 1.0 hits per first inning.
    hits_per_fi = fi_stats.get("fi_hits_per_fi")
    if hits_per_fi is not None:
        if hits_per_fi <= 0.50:
            adj += 2      # dominant — few baserunners at all
        elif hits_per_fi <= 0.80:
            adj += 1      # below-average traffic — good sign
        elif hits_per_fi <= 1.20:
            adj += 0      # league-average range — neutral
        elif hits_per_fi <= 1.60:
            adj -= 1      # elevated traffic — regression risk
        else:
            adj -= 2      # heavy traffic — clean% likely to crater

    return round(adj * confidence, 1)


# ---------------------------------------------------------------------------
# TEAM FIRST-INNING SCORING TENDENCIES
# ---------------------------------------------------------------------------
# League average: ~27% of team-games involve scoring in the 1st inning.
# (Across MLB ~0.55 first-inning runs per game / 2 sides ~ 0.275 score-rate.)
LEAGUE_FI_SCORE_RATE = 0.27
TEAM_TENDENCY_LOOKBACK = 30  # rolling window in completed games

# Module-level cache so multiple teams sharing the same game don't refetch.
# Cleared at the top of analyze_date().
_linescore_cache: dict = {}


def fetch_linescore_cached(gpk: int) -> Optional[dict]:
    """Fetch a game linescore once per run, then reuse."""
    if not gpk:
        return None
    if gpk in _linescore_cache:
        return _linescore_cache[gpk]
    try:
        r = requests.get(f"{MLB_BASE}/game/{gpk}/linescore", timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception:
        data = None
    _linescore_cache[gpk] = data
    return data


def _fetch_team_completed_games(team_id: int, start_dt: date, end_dt: date) -> list[dict]:
    """Pull a team's completed regular-season games in a date window."""
    out = []
    try:
        data = mlb_get("schedule", {
            "sportId": 1,
            "teamId": team_id,
            "startDate": start_dt.isoformat(),
            "endDate": end_dt.isoformat(),
            "gameType": "R",
        })
        for d in data.get("dates", []):
            for g in d.get("games", []):
                state = safe_get(g, "status", "abstractGameState")
                if state != "Final":
                    continue
                gpk = g.get("gamePk")
                home_id = safe_get(g, "teams", "home", "team", "id")
                if not gpk or home_id is None:
                    continue
                out.append({
                    "gpk": gpk,
                    "isHome": (home_id == team_id),
                    "date": g.get("officialDate") or (g.get("gameDate") or "")[:10],
                })
    except Exception:
        pass
    return out


def get_team_first_inning_tendency(team_id: int, as_of_date: str,
                                   lookback_games: int = TEAM_TENDENCY_LOOKBACK) -> dict:
    """
    Compute a team's 1st-inning scoring rate over its trailing N completed games,
    split by whether the team was batting at home or on the road.

    Walks back through the schedule (current season first, then prior season if
    needed) until we have lookback_games completed regular-season games. Each
    game's first-inning runs are pulled from the linescore endpoint.

    Returns a dict with home/away/overall scoring rates plus sample sizes.
    """
    if not team_id:
        return {"has_data": False}

    try:
        as_of = date.fromisoformat(as_of_date)
    except Exception:
        as_of = date.today()

    # Step 1: pull current-season games up to the day before the slate.
    end_dt = as_of - timedelta(days=1)
    start_dt = end_dt - timedelta(days=120)
    games = _fetch_team_completed_games(team_id, start_dt, end_dt)

    # Step 2: if we don't have enough yet, extend back into the prior season.
    if len(games) < lookback_games:
        prior_end = start_dt - timedelta(days=1)
        prior_start = prior_end - timedelta(days=240)
        games += _fetch_team_completed_games(team_id, prior_start, prior_end)

    if not games:
        return {"has_data": False}

    # Sort newest-first, take the most recent N
    games.sort(key=lambda g: g["date"], reverse=True)
    games = games[:lookback_games]

    home_n = away_n = 0
    home_scored = away_scored = 0

    for g in games:
        ls = fetch_linescore_cached(g["gpk"])
        if not ls:
            continue
        innings = ls.get("innings", [])
        if not innings:
            continue
        first = innings[0]
        if g["isHome"]:
            runs = (first.get("home", {}) or {}).get("runs", 0) or 0
            home_n += 1
            if runs > 0:
                home_scored += 1
        else:
            runs = (first.get("away", {}) or {}).get("runs", 0) or 0
            away_n += 1
            if runs > 0:
                away_scored += 1

    total_n = home_n + away_n
    if total_n == 0:
        return {"has_data": False}

    home_rate = (home_scored / home_n) if home_n else None
    away_rate = (away_scored / away_n) if away_n else None
    overall_rate = (home_scored + away_scored) / total_n

    return {
        "has_data": True,
        "games": total_n,
        "home_games": home_n,
        "away_games": away_n,
        "home_scored": home_scored,
        "away_scored": away_scored,
        "home_score_rate": round(home_rate, 3) if home_rate is not None else None,
        "away_score_rate": round(away_rate, 3) if away_rate is not None else None,
        "overall_score_rate": round(overall_rate, 3),
    }


def _shrunk_team_rate(tendency: dict, side: str) -> Optional[float]:
    """
    Get a team's first-inning scoring rate as the relevant batter (home or away),
    with two-stage Bayesian shrinkage:
      1) shrink the home/away split toward the team's overall rate (k=10),
      2) then shrink toward league average (k=5).

    With ~15 games per side in a 30-game window, the splits are noisy and need
    regularization or we'll overweight a single hot/cold week.
    """
    if not tendency.get("has_data"):
        return None

    side_n = tendency.get(f"{side}_games", 0) or 0
    side_rate = tendency.get(f"{side}_score_rate")
    overall_n = tendency.get("games", 0) or 0
    overall_rate = tendency.get("overall_score_rate")

    if overall_n == 0:
        return None
    if side_rate is None or side_n == 0:
        # No same-side sample — fall back to overall, lightly shrunk to league.
        k = 8.0
        return (overall_rate * overall_n + LEAGUE_FI_SCORE_RATE * k) / (overall_n + k)

    # Stage 1: side -> team overall
    k1 = 10.0
    side_to_team = (side_rate * side_n + overall_rate * k1) / (side_n + k1)

    # Stage 2: team -> league
    k2 = 5.0
    team_to_league = (side_to_team * (side_n + k1) + LEAGUE_FI_SCORE_RATE * k2) / (side_n + k1 + k2)
    return team_to_league


def score_team_tendency(home_tendency: dict, away_tendency: dict) -> tuple[float, dict]:
    """
    Light NRFI adjustment based on each team's recent 1st-inning scoring tendency.

    Range: -3 to +3. Positive = both teams typically quiet in the 1st (good for NRFI).
    The home team's HOME rate and the away team's AWAY rate are used because that's
    the role they'll be in tonight.

    Also returns a metadata dict for the dashboard.
    """
    h_rate = _shrunk_team_rate(home_tendency, "home")
    a_rate = _shrunk_team_rate(away_tendency, "away")

    if h_rate is None and a_rate is None:
        return 0.0, {"has_data": False}

    rates = [r for r in (h_rate, a_rate) if r is not None]
    avg_rate = sum(rates) / len(rates)

    # Each 0.01 (1pp) below league average ≈ +0.30 points.
    # Typical team range is ~0.18–0.36, so this naturally produces ±2.7 ish.
    deviation = LEAGUE_FI_SCORE_RATE - avg_rate
    adj = deviation * 30
    adj = round(max(-3.0, min(3.0, adj)), 1)

    return adj, {
        "has_data": True,
        "league_baseline": LEAGUE_FI_SCORE_RATE,
        "home_team_home_rate_shrunk": round(h_rate, 3) if h_rate is not None else None,
        "away_team_away_rate_shrunk": round(a_rate, 3) if a_rate is not None else None,
        "blended_rate": round(avg_rate, 3),
        "adj": adj,
    }


def extract_pitcher_metrics(stats: dict) -> dict:
    """Pull the NRFI-relevant numbers from raw stats."""
    def to_float(v, default=None):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    era = to_float(stats.get("era"))
    whip = to_float(stats.get("whip"))
    k9 = to_float(stats.get("strikeoutsPer9Inn"))
    bb9 = to_float(stats.get("walksPer9Inn"))
    hr9 = to_float(stats.get("homeRunsPer9"))
    ip = to_float(stats.get("inningsPitched"))
    hits = to_float(stats.get("hits"))
    walks = to_float(stats.get("baseOnBalls"))
    strikeouts = to_float(stats.get("strikeOuts"))
    games_started = to_float(stats.get("gamesStarted"))
    games_played = to_float(stats.get("gamesPlayed"))
    runs = to_float(stats.get("runs"))
    earned_runs = to_float(stats.get("earnedRuns"))

    return {
        "era": era,
        "whip": whip,
        "k9": k9,
        "bb9": bb9,
        "hr9": hr9,
        "ip": ip,
        "hits": hits,
        "walks": walks,
        "strikeouts": strikeouts,
        "games_started": games_started,
        "games_played": games_played,
        "runs": runs,
        "earned_runs": earned_runs,
    }


def detect_bullpen_game(metrics: dict) -> dict:
    """
    Detect whether a pitcher is likely a reliever making a spot start
    (i.e., a bullpen game) based on season stats.

    Uses games_started vs games_played ratio from season-to-date stats.
    A traditional starter will have GS ≈ GP.  A reliever pressed into a
    start will have many more GP than GS.

    Returns:
        {
            "is_bullpen": bool,
            "reason": str | None,        # human-readable explanation
            "games_started": int,
            "games_played": int,
            "relief_appearances": int,
        }
    """
    gs = int(metrics.get("games_started") or 0)
    gp = int(metrics.get("games_played") or 0)
    relief = max(0, gp - gs)

    result = {
        "is_bullpen": False,
        "reason": None,
        "games_started": gs,
        "games_played": gp,
        "relief_appearances": relief,
    }

    # No stats at all — TBD pitcher or just called up.
    # Not flagged as bullpen; sample-size regression already handles this.
    if gp == 0:
        return result

    # Pure reliever: zero starts this season, multiple relief outings.
    if gs == 0 and relief >= 3:
        result["is_bullpen"] = True
        result["reason"] = f"reliever (0 GS, {gp} G this season)"
        return result

    # Mostly reliever: 1-2 starts but ≥5 relief appearances.
    # These are often "openers" or emergency spot starts.
    if gs <= 2 and relief >= 5:
        result["is_bullpen"] = True
        result["reason"] = f"primary reliever ({gs} GS, {relief} relief apps)"
        return result

    # Low-start pitcher with high relief ratio.
    # E.g., 3 starts and 10 relief appearances — still a reliever profile.
    if gs <= 3 and gp >= 8 and (relief / gp) >= 0.65:
        result["is_bullpen"] = True
        result["reason"] = f"reliever profile ({gs} GS, {relief} relief in {gp} G)"
        return result

    return result


# ---------------------------------------------------------------------------
# 3. BATTER STATS (top of order)
# ---------------------------------------------------------------------------
def get_batter_season_stats(player_id: int, game_date: Optional[str] = None,
                            season: Optional[int] = None) -> dict:
    """Fetch a batter's season-to-date stats as of the target slate date."""
    if not player_id:
        return {}
    season = season or _season_for_date(game_date)
    start_date = f"{season}-01-01"
    end_date = _stats_end_date(game_date)
    try:
        # Primary: current season-to-date as of day-1.
        data = mlb_get(f"people/{player_id}/stats", {
            "stats": "byDateRange",
            "group": "hitting",
            "startDate": start_date,
            "endDate": end_date,
        })
        s = _first_stat_from_groups(data.get("stats", []))
        if s:
            return s

        # Fallback: prior full season.
        data = mlb_get(f"people/{player_id}", {
            "hydrate": f"stats(group=[hitting],type=[season],season={season - 1})",
        })
        person = data.get("people", [{}])[0]
        s = _first_stat_from_groups(safe_get(person, "stats", default=[]))
        if s:
            return s
    except Exception:
        pass
    return {}


def _get_last_lineup_vs_hand(team_id: int, opposing_hand: str,
                              as_of_date: str = None) -> list[dict] | None:
    """
    Find the most recent completed game where this team faced a starter
    with the given handedness, and return the batting order (top 4).

    Searches up to 14 days back.  Returns None if no suitable game found.
    """
    if not team_id or opposing_hand not in ("L", "R"):
        return None

    from datetime import datetime, timedelta
    end = datetime.strptime(as_of_date, "%Y-%m-%d") if as_of_date else datetime.today()
    start = end - timedelta(days=14)

    try:
        data = mlb_get("schedule", {
            "sportId": 1,
            "teamId": team_id,
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": (end - timedelta(days=1)).strftime("%Y-%m-%d"),  # exclude today
            "hydrate": "probablePitcher",
        })
    except Exception:
        return None

    # Collect completed games, most recent first
    candidates = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            state = safe_get(g, "status", "detailedState", default="")
            if "Final" not in state:
                continue
            candidates.append(g)
    candidates.sort(key=lambda g: g.get("gameDate", ""), reverse=True)

    for g in candidates:
        away = safe_get(g, "teams", "away", default={})
        home = safe_get(g, "teams", "home", default={})

        # Determine which side this team was on and who pitched against them
        away_id = safe_get(away, "team", "id")
        home_id = safe_get(home, "team", "id")
        if team_id == home_id:
            opp_pitcher_id = safe_get(away, "probablePitcher", "id")
        elif team_id == away_id:
            opp_pitcher_id = safe_get(home, "probablePitcher", "id")
        else:
            continue

        if not opp_pitcher_id:
            continue

        # Check handedness
        hand = get_pitcher_hand(opp_pitcher_id)
        if hand != opposing_hand:
            continue

        # Found a match — pull the batting order from the boxscore
        gpk = g.get("gamePk")
        if not gpk:
            continue
        try:
            box = requests.get(
                f"{MLB_BASE}/game/{gpk}/boxscore", timeout=10
            ).json()
        except Exception:
            continue

        side = "home" if team_id == home_id else "away"
        team_box = safe_get(box, "teams", side, default={})
        batting_order = team_box.get("battingOrder", [])
        players = team_box.get("players", {})

        if len(batting_order) < 4:
            continue

        top4 = []
        for pid in batting_order[:4]:
            pdata = players.get(f"ID{pid}", {})
            person = pdata.get("person", {})
            top4.append({
                "id": pid,
                "name": person.get("fullName", "Unknown"),
                "position": safe_get(pdata, "position", "abbreviation", default=""),
            })
        return top4

    return None


def get_top_of_order(lineup: list[dict], team_id: int, lineup_available: bool,
                     opposing_hand: str = "?",
                     game_date: str = None,
                     season: Optional[int] = None) -> tuple[list[dict], bool]:
    """
    Get the top 4 batters with their season stats.

    Strategy:
      1. If official lineup is available → use it (first 4 in batting order)
      2. Fallback → lineup from last game vs same-handed starter
      3. Last resort → roster sorted by PA

    Returns (batters_list, used_real_lineup: bool)
    """

    def _enrich(player_list: list[dict]) -> list[dict]:
        """Fetch season stats for a list of player dicts with 'id'/'name'."""
        batters = []
        for p in player_list:
            stat = get_batter_season_stats(p["id"], game_date=game_date, season=season)
            pa = 0
            try:
                pa = int(stat.get("plateAppearances", 0))
            except (TypeError, ValueError):
                pass
            batters.append({
                "id": p["id"],
                "name": p["name"],
                "position": p.get("position", ""),
                "pa": pa,
                "avg": stat.get("avg", ".000"),
                "obp": stat.get("obp", ".000"),
                "slg": stat.get("slg", ".000"),
                "ops": stat.get("ops", ".000"),
                "hr": stat.get("homeRuns", 0),
                "so": stat.get("strikeOuts", 0),
                "bb": stat.get("baseOnBalls", 0),
            })
        return batters

    # --- Strategy 1: Official lineup ---
    if lineup_available and len(lineup) >= 4:
        return _enrich(lineup[:4]), True

    # --- Strategy 2: Last game vs same-handed starter ---
    if team_id and opposing_hand in ("L", "R"):
        prev = _get_last_lineup_vs_hand(team_id, opposing_hand, game_date)
        if prev and len(prev) >= 4:
            print(f"    ↳ Using lineup from last game vs {opposing_hand}HP")
            return _enrich(prev), False

    # --- Strategy 3: Fallback to roster sorted by PA ---
    if not team_id:
        return [], False
    season = season or _season_for_date(game_date)
    end_date = _stats_end_date(game_date)
    try:
        data = mlb_get(f"teams/{team_id}/roster", {
            "rosterType": "active",
            "hydrate": (
                "person(stats("
                f"group=[hitting],type=[byDateRange],startDate={season}-01-01,endDate={end_date}"
                "))"
            ),
        })
        batters = []
        for entry in data.get("roster", []):
            pos = safe_get(entry, "position", "abbreviation", default="P")
            if pos == "P":
                continue
            person = entry.get("person", {})
            stat = _first_stat_from_groups(safe_get(person, "stats", default=[]))
            pa = 0
            try:
                pa = int(stat.get("plateAppearances", 0))
            except (TypeError, ValueError):
                pass
            batters.append({
                "id": person.get("id"),
                "name": person.get("fullName", "Unknown"),
                "pa": pa,
                "avg": stat.get("avg", ".000"),
                "obp": stat.get("obp", ".000"),
                "slg": stat.get("slg", ".000"),
                "ops": stat.get("ops", ".000"),
                "hr": stat.get("homeRuns", 0),
                "so": stat.get("strikeOuts", 0),
                "bb": stat.get("baseOnBalls", 0),
            })
        batters.sort(key=lambda b: b["pa"], reverse=True)
        return batters[:4], False
    except Exception as e:
        print(f"  ⚠ Could not fetch roster for team {team_id}: {e}")
        return [], False


def summarize_top_order(batters: list[dict]) -> dict:
    """Compute aggregate stats for the top-of-order hitters."""
    if not batters:
        return {"avg_obp": None, "avg_slg": None, "avg_ops": None, "batters": []}

    def to_f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    obps = [to_f(b["obp"]) for b in batters]
    slgs = [to_f(b["slg"]) for b in batters]
    opss = [to_f(b["ops"]) for b in batters]

    return {
        "avg_obp": sum(obps) / len(obps) if obps else None,
        "avg_slg": sum(slgs) / len(slgs) if slgs else None,
        "avg_ops": sum(opss) / len(opss) if opss else None,
        "batters": batters,
    }


# ---------------------------------------------------------------------------
# 3b. BATTER L/R PLATOON SPLITS
# ---------------------------------------------------------------------------
def get_batter_platoon_splits(batters: list[dict], pitcher_hand: str,
                              game_date: Optional[str] = None,
                              season: Optional[int] = None) -> list[dict]:
    """
    Enrich each batter with their stats specifically against LHP or RHP.
    pitcher_hand: 'L' or 'R'
    """
    if pitcher_hand not in ("L", "R"):
        return batters  # can't look up splits without knowing hand

    sit_code = "vl" if pitcher_hand == "L" else "vr"
    season = season or _season_for_date(game_date)
    start_date = f"{season}-01-01"
    end_date = _stats_end_date(game_date)

    enriched = []
    for batter in batters:
        batter_id = batter.get("id")
        platoon = {"has_data": False, "vs_hand": pitcher_hand}

        if batter_id:
            try:
                data = mlb_get(f"people/{batter_id}/stats", {
                    "stats": "statSplits",
                    "group": "hitting",
                    "season": season,
                    "sitCodes": sit_code,
                    "startDate": start_date,
                    "endDate": end_date,
                })
                for sg in data.get("stats", []):
                    for split in sg.get("splits", []):
                        st = split.get("stat", {})
                        ab = int(st.get("atBats", 0))
                        if ab > 0:
                            platoon = {
                                "has_data": True,
                                "vs_hand": pitcher_hand,
                                "ab": ab,
                                "avg": st.get("avg", ".000"),
                                "obp": st.get("obp", ".000"),
                                "ops": st.get("ops", ".000"),
                                "hr": int(st.get("homeRuns", 0)),
                                "so": int(st.get("strikeOuts", 0)),
                            }
                        break
                    break
            except Exception:
                pass

        # Also get batter's own bat side for reference
        if batter_id and "bat_side" not in batter:
            try:
                pdata = mlb_get(f"people/{batter_id}")
                person = pdata.get("people", [{}])[0]
                platoon["bat_side"] = safe_get(person, "batSide", "code", default="?")
            except Exception:
                platoon["bat_side"] = "?"

        b_copy = dict(batter)
        b_copy["platoon"] = platoon
        enriched.append(b_copy)

    return enriched


def summarize_platoon(batters: list[dict]) -> dict:
    """Aggregate platoon split data for the top of the order."""
    def to_f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    with_data = [b for b in batters if b.get("platoon", {}).get("has_data")]
    if not with_data:
        return {"has_data": False}

    total_ab = sum(b["platoon"]["ab"] for b in with_data)
    ops_vals = [to_f(b["platoon"]["ops"]) for b in with_data if to_f(b["platoon"]["ops"]) is not None]

    # Count platoon advantage/disadvantage
    # Same-hand matchup (e.g. LHB vs LHP) is disadvantage for batter
    # Opposite-hand (RHB vs LHP) is advantage for batter
    advantage_count = 0
    disadvantage_count = 0
    for b in with_data:
        bat_side = b.get("platoon", {}).get("bat_side", "?")
        vs_hand = b.get("platoon", {}).get("vs_hand", "?")
        if bat_side == "S":  # switch hitter — always has advantage
            advantage_count += 1
        elif bat_side == vs_hand:
            disadvantage_count += 1  # same hand = batter disadvantage
        elif bat_side != "?" and vs_hand != "?":
            advantage_count += 1  # opposite hand = batter advantage

    weighted_ops = None
    if total_ab > 0 and ops_vals:
        weighted_ops = sum(
            to_f(b["platoon"]["ops"]) * b["platoon"]["ab"]
            for b in with_data
            if to_f(b["platoon"]["ops"]) is not None
        ) / total_ab

    return {
        "has_data": True,
        "vs_hand": with_data[0]["platoon"]["vs_hand"] if with_data else "?",
        "total_ab": total_ab,
        "weighted_ops": round(weighted_ops, 3) if weighted_ops is not None else None,
        "advantage_count": advantage_count,
        "disadvantage_count": disadvantage_count,
        "batters_with_data": len(with_data),
    }


def score_platoon(platoon_summary: dict) -> float:
    """
    Platoon adjustment: -6 to +6.
    Positive = lineup has platoon disadvantage (good for NRFI).
    Negative = lineup has platoon advantage (bad for NRFI).
    """
    if not platoon_summary.get("has_data"):
        return 0.0

    adj = 0.0
    w_ops = platoon_summary.get("weighted_ops")
    adv = platoon_summary.get("advantage_count", 0)
    disadv = platoon_summary.get("disadvantage_count", 0)

    # OPS-based adjustment against this hand
    if w_ops is not None:
        if w_ops >= .850:
            adj -= 4     # lineup rakes vs this handedness
        elif w_ops >= .750:
            adj -= 2
        elif w_ops <= .550:
            adj += 4     # lineup struggles vs this handedness
        elif w_ops <= .650:
            adj += 2

    # Platoon composition bonus
    if disadv >= 3:
        adj += 2   # most of the top order has same-hand disadvantage
    elif adv >= 3:
        adj -= 2   # most of the top order has opposite-hand advantage

    return round(max(-6, min(6, adj)), 1)


# ---------------------------------------------------------------------------
# 3c. BATTER HOT/COLD STREAKS (last 7 games)
# ---------------------------------------------------------------------------
STREAK_GAMES = 7  # look-back window

def get_batter_recent_form(batters: list[dict], game_date: Optional[str] = None,
                           season: Optional[int] = None) -> list[dict]:
    """
    Enrich each batter with their last-7-game stats.
    Compares recent OPS to season OPS to detect hot/cold streaks.
    """
    as_of = _parse_game_date(game_date)
    season = season or _season_for_date(game_date)
    enriched = []
    for batter in batters:
        batter_id = batter.get("id")
        recent = {"has_data": False}

        if batter_id:
            try:
                data = mlb_get(f"people/{batter_id}/stats", {
                    "stats": "gameLog",
                    "group": "hitting",
                    "season": season,
                })
                logs = []
                for sg in data.get("stats", []):
                    for split in sg.get("splits", []):
                        d = split.get("date", "")
                        try:
                            d_dt = date.fromisoformat(d)
                        except Exception:
                            continue
                        if d_dt >= as_of:
                            continue
                        logs.append({"date": d, "stat": split.get("stat", {})})

                logs.sort(key=lambda g: g["date"], reverse=True)
                recent_logs = logs[:STREAK_GAMES]
                if recent_logs:
                    ab = sum(int((g["stat"] or {}).get("atBats", 0) or 0) for g in recent_logs)
                    hits = sum(int((g["stat"] or {}).get("hits", 0) or 0) for g in recent_logs)
                    hr = sum(int((g["stat"] or {}).get("homeRuns", 0) or 0) for g in recent_logs)
                    so = sum(int((g["stat"] or {}).get("strikeOuts", 0) or 0) for g in recent_logs)
                    bb = sum(int((g["stat"] or {}).get("baseOnBalls", 0) or 0) for g in recent_logs)
                    hbp = sum(int((g["stat"] or {}).get("hitByPitch", 0) or 0) for g in recent_logs)
                    sf = sum(int((g["stat"] or {}).get("sacFlies", 0) or 0) for g in recent_logs)
                    doubles = sum(int((g["stat"] or {}).get("doubles", 0) or 0) for g in recent_logs)
                    triples = sum(int((g["stat"] or {}).get("triples", 0) or 0) for g in recent_logs)

                    total_bases = sum(int((g["stat"] or {}).get("totalBases", 0) or 0) for g in recent_logs)
                    if total_bases == 0 and hits > 0:
                        singles = max(0, hits - doubles - triples - hr)
                        total_bases = singles + 2 * doubles + 3 * triples + 4 * hr

                    avg = (hits / ab) if ab > 0 else None
                    obp_den = ab + bb + hbp + sf
                    obp = ((hits + bb + hbp) / obp_den) if obp_den > 0 else None
                    slg = (total_bases / ab) if ab > 0 else None
                    ops = (obp + slg) if (obp is not None and slg is not None) else None

                    recent = {
                        "has_data": True,
                        "games": len(recent_logs),
                        "ab": ab,
                        "hits": hits,
                        "hr": hr,
                        "so": so,
                        "bb": bb,
                        "avg": _fmt_rate(avg),
                        "obp": _fmt_rate(obp),
                        "ops": _fmt_rate(ops),
                    }
            except Exception:
                pass

        # Compute streak status by comparing recent OPS to season OPS
        def to_f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        season_ops = to_f(batter.get("ops"))
        recent_ops = to_f(recent.get("ops")) if recent["has_data"] else None

        streak_status = "unknown"
        ops_delta = None
        if season_ops is not None and recent_ops is not None and season_ops > 0:
            ops_delta = recent_ops - season_ops
            # Thresholds for streak classification
            if ops_delta >= .150:
                streak_status = "hot"       # way above season pace
            elif ops_delta >= .050:
                streak_status = "warm"      # trending up
            elif ops_delta <= -.150:
                streak_status = "cold"      # way below season pace
            elif ops_delta <= -.050:
                streak_status = "cool"      # trending down
            else:
                streak_status = "neutral"   # in line with season

        recent["streak_status"] = streak_status
        recent["ops_delta"] = round(ops_delta, 3) if ops_delta is not None else None

        b_copy = dict(batter)
        b_copy["recent"] = recent
        enriched.append(b_copy)

    return enriched


def summarize_streaks(batters: list[dict]) -> dict:
    """Aggregate streak info across the top of the order."""
    def to_f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    with_data = [b for b in batters if b.get("recent", {}).get("has_data")]
    if not with_data:
        return {"has_data": False, "avg_recent_ops": None, "avg_ops_delta": None,
                "hot_count": 0, "cold_count": 0}

    recent_ops_vals = [to_f(b["recent"]["ops"]) for b in with_data if to_f(b["recent"]["ops"]) is not None]
    deltas = [b["recent"]["ops_delta"] for b in with_data if b["recent"].get("ops_delta") is not None]
    hot = sum(1 for b in with_data if b["recent"]["streak_status"] in ("hot", "warm"))
    cold = sum(1 for b in with_data if b["recent"]["streak_status"] in ("cold", "cool"))

    return {
        "has_data": True,
        "avg_recent_ops": round(sum(recent_ops_vals) / len(recent_ops_vals), 3) if recent_ops_vals else None,
        "avg_ops_delta": round(sum(deltas) / len(deltas), 3) if deltas else None,
        "hot_count": hot,
        "cold_count": cold,
        "total_with_data": len(with_data),
    }


def score_streaks(streak_summary: dict) -> float:
    """
    Streak adjustment: -6 to +6.
    Positive = lineup is cold (good for NRFI).
    Negative = lineup is hot (bad for NRFI).
    """
    if not streak_summary.get("has_data"):
        return 0.0

    adj = 0.0
    avg_delta = streak_summary.get("avg_ops_delta")

    if avg_delta is not None:
        if avg_delta <= -.200:
            adj += 5     # lineup is ice cold
        elif avg_delta <= -.100:
            adj += 3
        elif avg_delta <= -.050:
            adj += 1
        elif avg_delta >= .200:
            adj -= 5     # lineup is on fire
        elif avg_delta >= .100:
            adj -= 3
        elif avg_delta >= .050:
            adj -= 1

    # Extra push if multiple hitters are streaking the same direction
    hot = streak_summary.get("hot_count", 0)
    cold = streak_summary.get("cold_count", 0)
    if cold >= 3:
        adj += 1
    if hot >= 3:
        adj -= 1

    return round(max(-6, min(6, adj)), 1)


# ---------------------------------------------------------------------------
# 3c. BATTER vs PITCHER (BvP) MATCHUP HISTORY
# ---------------------------------------------------------------------------
def get_bvp_matchups(batters: list[dict], pitcher_id: int,
                     game_date: Optional[str] = None) -> list[dict]:
    """
    For each batter, fetch their career stats against a specific pitcher.
    Returns enriched batter dicts with 'bvp' key added.
    """
    if not pitcher_id or not batters:
        return batters
    as_of = _parse_game_date(game_date)
    historical_replay = as_of < date.today()
    end_date = _stats_end_date(game_date)

    enriched = []
    for batter in batters:
        batter_id = batter.get("id")
        bvp = {"ab": 0, "hits": 0, "hr": 0, "so": 0, "bb": 0, "avg": None, "ops": None, "has_data": False}

        # MLB vsPlayerTotal currently ignores start/end date filters. To avoid
        # look-ahead leakage on historical replays, disable BvP there.
        if historical_replay:
            b_copy = dict(batter)
            b_copy["bvp"] = bvp
            enriched.append(b_copy)
            continue

        if batter_id:
            try:
                data = mlb_get(f"people/{batter_id}/stats", {
                    "stats": "vsPlayerTotal",
                    "opposingPlayerId": pitcher_id,
                    "group": "hitting",
                    "startDate": "1900-01-01",
                    "endDate": end_date,
                })
                for stat_group in data.get("stats", []):
                    for split in stat_group.get("splits", []):
                        s = split.get("stat", {})
                        ab = int(s.get("atBats", 0))
                        if ab > 0:
                            bvp = {
                                "ab": ab,
                                "hits": int(s.get("hits", 0)),
                                "hr": int(s.get("homeRuns", 0)),
                                "so": int(s.get("strikeOuts", 0)),
                                "bb": int(s.get("baseOnBalls", 0)),
                                "avg": s.get("avg"),
                                "ops": s.get("ops"),
                                "has_data": True,
                            }
                        break  # only need the total
                    break
            except Exception as e:
                pass  # silently skip — BvP is supplemental

        b_copy = dict(batter)
        b_copy["bvp"] = bvp
        enriched.append(b_copy)

    return enriched


def summarize_bvp(batters: list[dict]) -> dict:
    """
    Aggregate BvP data across the top of the order.
    Returns summary with average BvP OPS, sample size, and danger flags.
    """
    def to_f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    bvp_batters = [b for b in batters if b.get("bvp", {}).get("has_data")]
    total_ab = sum(b["bvp"]["ab"] for b in bvp_batters)
    total_hits = sum(b["bvp"]["hits"] for b in bvp_batters)
    total_hr = sum(b["bvp"]["hr"] for b in bvp_batters)
    total_so = sum(b["bvp"]["so"] for b in bvp_batters)

    ops_values = [to_f(b["bvp"]["ops"]) for b in bvp_batters if to_f(b["bvp"]["ops"]) is not None]
    avg_values = [to_f(b["bvp"]["avg"]) for b in bvp_batters if to_f(b["bvp"]["avg"]) is not None]

    # Weight by ABs for a more accurate aggregate
    weighted_ops = None
    if bvp_batters and total_ab > 0:
        weighted_ops = sum(
            to_f(b["bvp"]["ops"]) * b["bvp"]["ab"]
            for b in bvp_batters
            if to_f(b["bvp"]["ops"]) is not None
        ) / total_ab

    weighted_avg = None
    if total_ab > 0:
        weighted_avg = total_hits / total_ab

    return {
        "total_ab": total_ab,
        "total_hits": total_hits,
        "total_hr": total_hr,
        "total_so": total_so,
        "weighted_avg": round(weighted_avg, 3) if weighted_avg is not None else None,
        "weighted_ops": round(weighted_ops, 3) if weighted_ops is not None else None,
        "batters_with_history": len(bvp_batters),
        "has_meaningful_data": total_ab >= 8,  # need at least ~2 AB per batter
    }


def score_bvp(bvp_summary: dict) -> float:
    """
    Score the BvP matchup: returns adjustment from -10 to +10.
    Positive = batters struggle against this pitcher (good for NRFI).
    Negative = batters own this pitcher (bad for NRFI).
    Only applies when we have meaningful sample size.
    """
    if not bvp_summary.get("has_meaningful_data"):
        return 0.0  # no adjustment if insufficient data

    adj = 0.0
    total_ab = bvp_summary["total_ab"]
    w_ops = bvp_summary.get("weighted_ops")
    w_avg = bvp_summary.get("weighted_avg")

    # Confidence multiplier: more ABs = more weight (caps at 1.0 for 30+ AB)
    confidence = min(1.0, total_ab / 30.0)

    if w_ops is not None:
        if w_ops >= .900:
            adj -= 8   # batters crush this pitcher
        elif w_ops >= .800:
            adj -= 5
        elif w_ops >= .700:
            adj -= 1
        elif w_ops <= .500:
            adj += 8   # pitcher dominates these batters
        elif w_ops <= .600:
            adj += 5
        elif w_ops <= .650:
            adj += 2

    # HR flag: if multiple HRs in limited ABs, that's a red flag
    if total_ab > 0 and bvp_summary["total_hr"] >= 2:
        hr_rate = bvp_summary["total_hr"] / total_ab
        if hr_rate > 0.08:  # more than 1 HR per ~12 AB
            adj -= 3

    # Strikeout dominance: pitcher fans these guys
    if total_ab > 0:
        so_rate = bvp_summary["total_so"] / total_ab
        if so_rate > 0.35:
            adj += 3
        elif so_rate > 0.28:
            adj += 1

    return round(adj * confidence, 1)


# ---------------------------------------------------------------------------
# 4. WEATHER
# ---------------------------------------------------------------------------
def _compute_wind_effect(wind_dir_deg: float, wind_mph: float, venue: str) -> dict:
    """
    Compute how wind direction interacts with ballpark orientation.

    Returns a dict with:
        component_out: positive mph component blowing toward outfield (bad for NRFI)
        component_in:  positive mph component blowing toward home plate (good for NRFI)
        label: human-readable description ("blowing out", "blowing in", "crosswind")
    """
    park_dir = PARK_ORIENTATION.get(venue)
    if park_dir is None:
        # fuzzy match
        for k, v in PARK_ORIENTATION.items():
            if k.lower() in venue.lower() or venue.lower() in k.lower():
                park_dir = v
                break
    if park_dir is None or wind_mph is None or wind_dir_deg is None:
        return {"component_out": 0, "component_in": 0, "label": "unknown"}

    # Wind direction from Open-Meteo = direction wind is coming FROM (meteorological).
    # Wind blowing OUT = wind coming from behind home plate toward outfield.
    # "Behind home plate" direction = park_dir + 180 (opposite of outfield).
    # So wind blowing out means wind_dir_deg ≈ park_dir + 180 (mod 360).
    #
    # Angle between wind source and "behind home plate":
    behind_plate = (park_dir + 180) % 360
    diff = wind_dir_deg - behind_plate
    # Normalize to -180..180
    diff = (diff + 180) % 360 - 180

    # cos(diff) > 0 means wind is blowing OUT (from behind plate toward outfield)
    # cos(diff) < 0 means wind is blowing IN (from outfield toward plate)
    cos_component = math.cos(math.radians(diff))
    effective_mph = wind_mph * cos_component

    if cos_component > 0.25:
        label = "blowing out"
        return {"component_out": round(effective_mph, 1), "component_in": 0, "label": label}
    elif cos_component < -0.25:
        label = "blowing in"
        return {"component_out": 0, "component_in": round(abs(effective_mph), 1), "label": label}
    else:
        label = "crosswind"
        return {"component_out": 0, "component_in": 0, "label": label}


def get_weather(venue: str, game_time_iso: str) -> dict:
    """Fetch weather at game time from Open-Meteo, including wind direction."""
    if venue in INDOOR_PARKS:
        return {"indoor": True, "temp_f": 72, "wind_mph": 0, "precip_chance": 0,
                "wind_dir": None, "wind_effect": {"component_out": 0, "component_in": 0, "label": "indoor"}}

    coords = BALLPARK_COORDS.get(venue)
    if not coords:
        # fuzzy match
        for k, v in BALLPARK_COORDS.items():
            if k.lower() in venue.lower() or venue.lower() in k.lower():
                coords = v
                break
    if not coords:
        return {"indoor": False, "temp_f": None, "wind_mph": None, "precip_chance": None,
                "wind_dir": None, "wind_effect": {"component_out": 0, "component_in": 0, "label": "unknown"}}

    try:
        # Parse the game date for the API
        game_dt = datetime.fromisoformat(game_time_iso.replace("Z", "+00:00"))
        game_date_str = game_dt.strftime("%Y-%m-%d")
        game_hour = game_dt.hour

        r = requests.get(OPEN_METEO_BASE, params={
            "latitude": coords[0],
            "longitude": coords[1],
            "hourly": "temperature_2m,windspeed_10m,precipitation_probability,winddirection_10m",
            "temperature_unit": "fahrenheit",
            "windspeed_unit": "mph",
            "start_date": game_date_str,
            "end_date": game_date_str,
            "timezone": "America/New_York",
        }, timeout=10)
        r.raise_for_status()
        hourly = r.json().get("hourly", {})

        # Find the hour closest to game time
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        winds = hourly.get("windspeed_10m", [])
        precip = hourly.get("precipitation_probability", [])
        wind_dirs = hourly.get("winddirection_10m", [])

        # default to mid-evening index
        idx = min(game_hour, len(temps) - 1) if temps else 0

        wind_mph = winds[idx] if idx < len(winds) else None
        wind_dir = wind_dirs[idx] if idx < len(wind_dirs) else None
        wind_effect = _compute_wind_effect(wind_dir, wind_mph, venue)

        return {
            "indoor": False,
            "temp_f": temps[idx] if idx < len(temps) else None,
            "wind_mph": wind_mph,
            "precip_chance": precip[idx] if idx < len(precip) else None,
            "wind_dir": wind_dir,
            "wind_effect": wind_effect,
        }
    except Exception as e:
        print(f"  ⚠ Weather fetch failed for {venue}: {e}")
        return {"indoor": False, "temp_f": None, "wind_mph": None, "precip_chance": None}


# ---------------------------------------------------------------------------
# 5. NRFI SCORING ENGINE
# ---------------------------------------------------------------------------
def score_pitcher(metrics: dict) -> float:
    """
    Score a pitcher 0-100 for first-inning shutout likelihood.
    Higher = more likely to keep runs off the board.

    Applies sample-size regression: with fewer games started, the raw
    score is blended toward the league-average baseline (50).  Full
    confidence is reached at 10 games started.
    """
    score = 50.0  # baseline

    era = metrics.get("era")
    whip = metrics.get("whip")
    k9 = metrics.get("k9")
    bb9 = metrics.get("bb9")
    hr9 = metrics.get("hr9")

    if era is not None:
        # ERA < 3.00 is elite, > 5.00 is bad
        if era <= 2.50:
            score += 18
        elif era <= 3.00:
            score += 14
        elif era <= 3.50:
            score += 8
        elif era <= 4.00:
            score += 2
        elif era <= 4.50:
            score -= 4
        elif era <= 5.00:
            score -= 10
        else:
            score -= 18

    if whip is not None:
        # WHIP < 1.00 is elite, > 1.40 is bad
        if whip <= 0.95:
            score += 15
        elif whip <= 1.10:
            score += 10
        elif whip <= 1.20:
            score += 4
        elif whip <= 1.30:
            score -= 2
        elif whip <= 1.40:
            score -= 8
        else:
            score -= 15

    if k9 is not None:
        if k9 >= 10.0:
            score += 8
        elif k9 >= 8.5:
            score += 5
        elif k9 >= 7.0:
            score += 1
        else:
            score -= 4

    if bb9 is not None:
        if bb9 <= 2.0:
            score += 7
        elif bb9 <= 3.0:
            score += 3
        elif bb9 <= 3.5:
            score -= 2
        else:
            score -= 8

    if hr9 is not None:
        if hr9 <= 0.8:
            score += 5
        elif hr9 <= 1.0:
            score += 2
        elif hr9 <= 1.3:
            score -= 3
        else:
            score -= 7

    # --- Sample-size regression toward baseline (50) ---
    # With very few starts, rate stats are noisy.  Blend the raw score
    # toward 50 proportionally: full weight at 10+ GS.
    gs = metrics.get("games_started", 0)
    confidence = min(1.0, gs / 10.0) if gs else 0.5  # fallback if missing
    score = 50.0 + (score - 50.0) * confidence

    return max(0, min(100, score))


def score_lineup_threat(top_order: dict) -> float:
    """
    Score how dangerous the top of the order is — 0-100.
    Higher = MORE threatening (worse for NRFI).
    """
    score = 50.0

    avg_ops = top_order.get("avg_ops")
    avg_obp = top_order.get("avg_obp")

    if avg_ops is not None:
        if avg_ops >= .850:
            score += 20
        elif avg_ops >= .780:
            score += 12
        elif avg_ops >= .720:
            score += 4
        elif avg_ops >= .660:
            score -= 5
        elif avg_ops >= .600:
            score -= 12
        else:
            score -= 20

    if avg_obp is not None:
        if avg_obp >= .370:
            score += 12
        elif avg_obp >= .340:
            score += 7
        elif avg_obp >= .310:
            score += 2
        elif avg_obp >= .280:
            score -= 5
        else:
            score -= 12

    return max(0, min(100, score))


def score_weather(weather: dict) -> float:
    """
    Weather adjustment factor: -10 to +10.
    Positive = favorable for NRFI (cold, calm/blowing-in wind).
    Negative = unfavorable (hot, blowing-out wind).

    Wind direction is now park-aware: blowing out toward the outfield is a
    meaningful negative because fly balls carry; blowing in is a positive
    because it suppresses them.
    """
    if weather.get("indoor"):
        return 3  # neutral-to-slight positive (no wind/heat boost for hitters)

    adj = 0.0
    temp = weather.get("temp_f")
    wind = weather.get("wind_mph")
    wind_effect = weather.get("wind_effect", {})

    if temp is not None:
        if temp < 50:
            adj += 5  # cold suppresses offense
        elif temp < 65:
            adj += 2
        elif temp > 85:
            adj -= 4  # warm = ball carries
        elif temp > 75:
            adj -= 1

    # Wind scoring — direction-aware
    out_mph = wind_effect.get("component_out", 0)
    in_mph = wind_effect.get("component_in", 0)
    wind_label = wind_effect.get("label", "unknown")

    if wind_label == "blowing out":
        # Blowing out is bad for NRFI — ball carries to the outfield.
        # Scale: 5mph out ≈ negligible, 10mph out = -2, 15+mph out = -5
        if out_mph >= 15:
            adj -= 5
        elif out_mph >= 10:
            adj -= 3
        elif out_mph >= 6:
            adj -= 1
    elif wind_label == "blowing in":
        # Blowing in is good for NRFI — suppresses fly balls.
        if in_mph >= 15:
            adj += 5
        elif in_mph >= 10:
            adj += 3
        elif in_mph >= 6:
            adj += 1
    elif wind_label == "crosswind":
        # Crosswind is mildly chaotic at high speed, otherwise neutral
        if wind is not None and wind > 18:
            adj -= 1
    else:
        # Fallback: no direction data — use the old speed-only logic
        if wind is not None:
            if wind > 15:
                adj -= 2
            elif wind < 5:
                adj += 1

    return round(max(-10.0, min(10.0, adj)), 1)


def compute_nrfi_score(
    away_pitcher_score: float,
    home_pitcher_score: float,
    away_lineup_threat: float,
    home_lineup_threat: float,
    weather_adj: float,
    park_adj: float,
    bvp_home_vs_away_p: float = 0.0,
    bvp_away_vs_home_p: float = 0.0,
    streak_home_adj: float = 0.0,
    streak_away_adj: float = 0.0,
    platoon_home_adj: float = 0.0,    # home batters vs away pitcher hand
    platoon_away_adj: float = 0.0,    # away batters vs home pitcher hand
    fi_away_adj: float = 0.0,         # away pitcher first-inning adj
    fi_home_adj: float = 0.0,         # home pitcher first-inning adj
    team_tendency_adj: float = 0.0,   # blended team 1st-inning scoring tendency
    rest_away_adj: float = 0.0,       # away pitcher days-rest adjustment
    rest_home_adj: float = 0.0,       # home pitcher days-rest adjustment
    away_bullpen_game: bool = False,   # away side is a bullpen game
    home_bullpen_game: bool = False,   # home side is a bullpen game
) -> dict:
    """
    Combine all factors into a single NRFI confidence score (0-100).

    The key insight: for NRFI, BOTH half-innings must be scoreless.
    So we need both pitchers to be strong AND both lineups to be weak.
    """
    # Pitcher quality (higher is better for NRFI)
    pitcher_avg = (away_pitcher_score + home_pitcher_score) / 2
    pitcher_floor = min(away_pitcher_score, home_pitcher_score)
    pitcher_component = 0.4 * pitcher_avg + 0.6 * pitcher_floor

    # Lineup threat (invert: lower threat = better for NRFI)
    lineup_avg_threat = (away_lineup_threat + home_lineup_threat) / 2
    lineup_max_threat = max(away_lineup_threat, home_lineup_threat)
    lineup_component = 100 - (0.4 * lineup_avg_threat + 0.6 * lineup_max_threat)

    # Adjustments
    bvp_adj = bvp_home_vs_away_p + bvp_away_vs_home_p
    streak_adj = streak_home_adj + streak_away_adj
    platoon_adj = platoon_home_adj + platoon_away_adj
    fi_adj = fi_away_adj + fi_home_adj
    rest_adj = rest_away_adj + rest_home_adj

    # --- Bullpen game penalty ---
    # A reliever pressed into a starting role introduces uncertainty.
    # For NRFI this is moderate: relievers can handle 1 inning fine, but
    # their first-inning stats as a "starter" are unreliable, and managers
    # sometimes use openers for just 1-2 batters before a quick hook.
    # Penalty: -6 per bullpen side (max -12 if both).
    bullpen_adj = 0.0
    if away_bullpen_game:
        bullpen_adj -= 6.0
    if home_bullpen_game:
        bullpen_adj -= 6.0

    # Combine: 45% pitching, 25% lineup, adjustments for everything else
    raw = (0.45 * pitcher_component +
           0.25 * lineup_component +
           fi_adj + bvp_adj + platoon_adj + streak_adj +
           weather_adj + park_adj + team_tendency_adj + rest_adj +
           bullpen_adj)

    # Clamp
    final = max(0, min(100, raw))

    # Confidence tier
    if final >= 72:
        tier = "STRONG"
    elif final >= 62:
        tier = "LEAN"
    elif final >= 50:
        tier = "TOSS-UP"
    else:
        tier = "FADE"

    return {
        "score": round(final, 1),
        "tier": tier,
        "pitcher_component": round(pitcher_component, 1),
        "lineup_component": round(lineup_component, 1),
        "fi_adj": round(fi_adj, 1),
        "bvp_adj": round(bvp_adj, 1),
        "platoon_adj": round(platoon_adj, 1),
        "streak_adj": round(streak_adj, 1),
        "park_adj": round(park_adj, 1),
        "weather_adj": round(weather_adj, 1),
        "team_tendency_adj": round(team_tendency_adj, 1),
        "rest_adj": round(rest_adj, 1),
        "bullpen_adj": round(bullpen_adj, 1),
    }


# ---------------------------------------------------------------------------
# 6. MAIN ANALYSIS PIPELINE
# ---------------------------------------------------------------------------
def analyze_date(game_date: str) -> list[dict]:
    """Run the full NRFI analysis for a given date."""
    print(f"\n{'='*60}")
    print(f"  NRFI ANALYZER — {game_date}")
    print(f"{'='*60}\n")

    # Reset cross-function caches at the start of every run.
    _linescore_cache.clear()
    _pitcher_gamelog_cache.clear()
    season = _season_for_date(game_date)
    stats_through = _stats_end_date(game_date)

    print(f"Using stats through {stats_through} (season {season})")

    games = get_todays_games(game_date)
    if not games:
        print("No games found for this date.")
        return []

    print(f"Found {len(games)} games. Analyzing...\n")
    results = []

    for i, game in enumerate(games):
        info = parse_game_info(game)
        matchup = f"{info['away_abbr']} @ {info['home_abbr']}"
        print(f"[{i+1}/{len(games)}] {matchup} — {info['venue']}")

        # Pitcher stats + handedness + first-inning ERA
        print(f"  Fetching pitcher stats...")
        away_p_raw = get_pitcher_season_stats(info["away_pitcher_id"], game_date, season)
        home_p_raw = get_pitcher_season_stats(info["home_pitcher_id"], game_date, season)
        away_p = extract_pitcher_metrics(away_p_raw)
        home_p = extract_pitcher_metrics(home_p_raw)

        away_hand = get_pitcher_hand(info["away_pitcher_id"])
        home_hand = get_pitcher_hand(info["home_pitcher_id"])
        print(f"  Pitcher hands: {info['away_pitcher_name']} ({away_hand}HP) vs {info['home_pitcher_name']} ({home_hand}HP)")

        # Bullpen game detection
        away_bp = detect_bullpen_game(away_p)
        home_bp = detect_bullpen_game(home_p)
        if away_bp["is_bullpen"]:
            print(f"  ⚠ BULLPEN GAME: {info['away_pitcher_name']} — {away_bp['reason']}")
        if home_bp["is_bullpen"]:
            print(f"  ⚠ BULLPEN GAME: {info['home_pitcher_name']} — {home_bp['reason']}")

        # Pitcher rest + workload
        print(f"  Fetching pitcher rest & workload...")
        away_rest = get_pitcher_rest_and_workload(info["away_pitcher_id"], game_date, season)
        home_rest = get_pitcher_rest_and_workload(info["home_pitcher_id"], game_date, season)
        away_rest_adj = score_rest(away_rest)
        home_rest_adj = score_rest(home_rest)
        if away_rest.get("has_data"):
            print(f"  Rest: {info['away_pitcher_name']} — {away_rest['days_rest']}d rest, "
                  f"{away_rest['last_pitches'] or '?'}P last outing (adj {'+' if away_rest_adj >= 0 else ''}{away_rest_adj})")
        if home_rest.get("has_data"):
            print(f"  Rest: {info['home_pitcher_name']} — {home_rest['days_rest']}d rest, "
                  f"{home_rest['last_pitches'] or '?'}P last outing (adj {'+' if home_rest_adj >= 0 else ''}{home_rest_adj})")

        print("  Fetching pitch-count efficiency...")
        away_pitch_eff = get_pitcher_pitch_efficiency(info["away_pitcher_id"], game_date, season)
        home_pitch_eff = get_pitcher_pitch_efficiency(info["home_pitcher_id"], game_date, season)
        if away_pitch_eff.get("has_data"):
            print(f"  P/IP: {info['away_pitcher_name']} — {away_pitch_eff['avg_pitches_per_inning']:.2f} "
                  f"avg over {away_pitch_eff['starts_sample']} starts")
        if home_pitch_eff.get("has_data"):
            print(f"  P/IP: {info['home_pitcher_name']} — {home_pitch_eff['avg_pitches_per_inning']:.2f} "
                  f"avg over {home_pitch_eff['starts_sample']} starts")

        print(f"  Fetching first-inning history...")
        away_fi = get_first_inning_stats(info["away_pitcher_id"], game_date, season)
        home_fi = get_first_inning_stats(info["home_pitcher_id"], game_date, season)
        away_fi_adj = score_first_inning(away_fi)
        home_fi_adj = score_first_inning(home_fi)
        if away_fi.get("has_data"):
            print(f"  1st inn: {info['away_pitcher_name']} ERA={away_fi['fi_era']} clean={away_fi['fi_clean']}/{away_fi['fi_starts']} ({away_fi['fi_clean_pct']:.0%})")
        if home_fi.get("has_data"):
            print(f"  1st inn: {info['home_pitcher_name']} ERA={home_fi['fi_era']} clean={home_fi['fi_clean']}/{home_fi['fi_starts']} ({home_fi['fi_clean_pct']:.0%})")

        # Team first-inning scoring tendencies (trailing 30 games, home/away split)
        print(f"  Fetching team 1st-inning tendencies (last {TEAM_TENDENCY_LOOKBACK} games)...")
        home_tendency = get_team_first_inning_tendency(info["home_team_id"], game_date)
        away_tendency = get_team_first_inning_tendency(info["away_team_id"], game_date)
        team_tendency_adj, team_tendency_meta = score_team_tendency(home_tendency, away_tendency)
        if home_tendency.get("has_data"):
            print(f"  {info['home_abbr']} 1st-inn @home: {home_tendency.get('home_scored',0)}/{home_tendency.get('home_games',0)} "
                  f"({(home_tendency.get('home_score_rate') or 0):.0%}) | overall {home_tendency['games']}g {(home_tendency['overall_score_rate']):.0%}")
        if away_tendency.get("has_data"):
            print(f"  {info['away_abbr']} 1st-inn @away: {away_tendency.get('away_scored',0)}/{away_tendency.get('away_games',0)} "
                  f"({(away_tendency.get('away_score_rate') or 0):.0%}) | overall {away_tendency['games']}g {(away_tendency['overall_score_rate']):.0%}")
        if team_tendency_meta.get("has_data"):
            print(f"  Team tendency adj: {'+' if team_tendency_adj >= 0 else ''}{team_tendency_adj} "
                  f"(blended {team_tendency_meta['blended_rate']:.0%} vs league {LEAGUE_FI_SCORE_RATE:.0%})")

        # Batter stats — use real lineup when available, fallback to
        # last game vs same-handed starter, then roster
        home_batters, home_real_lineup = get_top_of_order(
            info["home_lineup"], info["home_team_id"],
            info["home_lineup_available"], away_hand, game_date, season)
        away_batters, away_real_lineup = get_top_of_order(
            info["away_lineup"], info["away_team_id"],
            info["away_lineup_available"], home_hand, game_date, season)

        lineup_note = ""
        if home_real_lineup and away_real_lineup:
            lineup_note = "✓ Official lineups"
        elif home_real_lineup or away_real_lineup:
            lineup_note = "⚠ Partial lineups (one side estimated from recent game vs same hand)"
        else:
            lineup_note = "⚠ Lineups estimated from recent game vs same hand or roster"
        print(f"  Lineups: {lineup_note}")

        # L/R platoon splits
        print(f"  Fetching L/R platoon splits...")
        home_batters_plat = get_batter_platoon_splits(home_batters, away_hand, game_date, season)
        away_batters_plat = get_batter_platoon_splits(away_batters, home_hand, game_date, season)

        # Recent form (hot/cold streaks)
        print(f"  Fetching recent form (last {STREAK_GAMES} games)...")
        home_batters_streaks = get_batter_recent_form(home_batters_plat, game_date, season)
        away_batters_streaks = get_batter_recent_form(away_batters_plat, game_date, season)

        # BvP matchup history (enriches on top of streak + platoon data)
        print(f"  Fetching batter vs pitcher history...")
        home_batters_bvp = get_bvp_matchups(home_batters_streaks, info["away_pitcher_id"], game_date)
        away_batters_bvp = get_bvp_matchups(away_batters_streaks, info["home_pitcher_id"], game_date)

        home_top = summarize_top_order(home_batters_bvp)
        away_top = summarize_top_order(away_batters_bvp)

        # BvP summaries
        home_bvp_summary = summarize_bvp(home_batters_bvp)  # home batters vs away pitcher
        away_bvp_summary = summarize_bvp(away_batters_bvp)  # away batters vs home pitcher
        home_bvp_adj = score_bvp(home_bvp_summary)
        away_bvp_adj = score_bvp(away_bvp_summary)

        if home_bvp_summary["has_meaningful_data"] or away_bvp_summary["has_meaningful_data"]:
            print(f"  BvP: home lineup {home_bvp_summary['total_ab']}AB/{home_bvp_summary.get('weighted_ops','?')}OPS vs away SP | "
                  f"away lineup {away_bvp_summary['total_ab']}AB/{away_bvp_summary.get('weighted_ops','?')}OPS vs home SP")

        # Platoon summaries
        home_platoon_summary = summarize_platoon(home_batters_bvp)
        away_platoon_summary = summarize_platoon(away_batters_bvp)
        home_platoon_adj = score_platoon(home_platoon_summary)
        away_platoon_adj = score_platoon(away_platoon_summary)
        if home_platoon_summary.get("has_data") or away_platoon_summary.get("has_data"):
            hp = home_platoon_summary
            ap = away_platoon_summary
            print(f"  Platoon: home vs {away_hand}HP — {hp.get('advantage_count',0)} adv/{hp.get('disadvantage_count',0)} disadv "
                  f"OPS={hp.get('weighted_ops','?')} | "
                  f"away vs {home_hand}HP — {ap.get('advantage_count',0)} adv/{ap.get('disadvantage_count',0)} disadv "
                  f"OPS={ap.get('weighted_ops','?')}")

        # Streak summaries
        home_streak_summary = summarize_streaks(home_batters_bvp)
        away_streak_summary = summarize_streaks(away_batters_bvp)
        home_streak_adj = score_streaks(home_streak_summary)
        away_streak_adj = score_streaks(away_streak_summary)
        if home_streak_summary.get("has_data") or away_streak_summary.get("has_data"):
            h_delta = home_streak_summary.get("avg_ops_delta")
            a_delta = away_streak_summary.get("avg_ops_delta")
            print(f"  Streaks: home lineup OPS Δ{'+' if h_delta and h_delta >= 0 else ''}{h_delta or '?'} "
                  f"({home_streak_summary.get('hot_count',0)}🔥 {home_streak_summary.get('cold_count',0)}🧊) | "
                  f"away lineup OPS Δ{'+' if a_delta and a_delta >= 0 else ''}{a_delta or '?'} "
                  f"({away_streak_summary.get('hot_count',0)}🔥 {away_streak_summary.get('cold_count',0)}🧊)")

        # Weather
        print(f"  Fetching weather...")
        weather = get_weather(info["venue"], info["game_time"])

        # Log wind effect
        w_eff = weather.get("wind_effect", {})
        if not weather.get("indoor") and w_eff.get("label") != "unknown":
            print(f"  Wind: {weather.get('wind_mph', '?')}mph {w_eff.get('label', '?')} "
                  f"(out={w_eff.get('component_out', 0)}mph, in={w_eff.get('component_in', 0)}mph)")

        # Park factor
        park_info = get_park_factor(info["venue"])
        park_adj = score_park(park_info)
        print(f"  Park: {info['venue']} — factor {park_info['factor']} ({park_info['label']}, adj {'+' if park_adj >= 0 else ''}{park_adj})")

        # Score
        away_p_score = score_pitcher(away_p)
        home_p_score = score_pitcher(home_p)
        home_lineup_threat = score_lineup_threat(home_top)  # vs away pitcher
        away_lineup_threat = score_lineup_threat(away_top)  # vs home pitcher
        weather_adj = score_weather(weather)

        nrfi = compute_nrfi_score(
            away_p_score, home_p_score,
            home_lineup_threat, away_lineup_threat,
            weather_adj, park_adj,
            bvp_home_vs_away_p=home_bvp_adj,
            bvp_away_vs_home_p=away_bvp_adj,
            streak_home_adj=home_streak_adj,
            streak_away_adj=away_streak_adj,
            platoon_home_adj=home_platoon_adj,
            platoon_away_adj=away_platoon_adj,
            fi_away_adj=away_fi_adj,
            fi_home_adj=home_fi_adj,
            team_tendency_adj=team_tendency_adj,
            rest_away_adj=away_rest_adj,
            rest_home_adj=home_rest_adj,
            away_bullpen_game=away_bp["is_bullpen"],
            home_bullpen_game=home_bp["is_bullpen"],
        )

        result = {
            "game_pk": info["game_pk"],
            "matchup": matchup,
            "game_time": info["game_time"],
            "venue": info["venue"],
            "is_indoor": info["is_indoor"],
            "away_team": info["away_team"],
            "home_team": info["home_team"],
            "away_abbr": info["away_abbr"],
            "home_abbr": info["home_abbr"],
            "away_pitcher": info["away_pitcher_name"],
            "home_pitcher": info["home_pitcher_name"],
            "away_pitcher_hand": away_hand,
            "home_pitcher_hand": home_hand,
            "away_pitcher_stats": away_p,
            "home_pitcher_stats": home_p,
            "away_fi": away_fi,
            "home_fi": home_fi,
            "home_team_tendency": home_tendency,
            "away_team_tendency": away_tendency,
            "team_tendency_meta": team_tendency_meta,
            "away_rest": away_rest,
            "home_rest": home_rest,
            "away_pitch_eff": away_pitch_eff,
            "home_pitch_eff": home_pitch_eff,
            "home_top_order": home_top,   # batters facing away pitcher
            "away_top_order": away_top,   # batters facing home pitcher
            "home_real_lineup": home_real_lineup,
            "away_real_lineup": away_real_lineup,
            "home_bvp": home_bvp_summary,
            "away_bvp": away_bvp_summary,
            "home_platoon": home_platoon_summary,
            "away_platoon": away_platoon_summary,
            "home_streaks": home_streak_summary,
            "away_streaks": away_streak_summary,
            "park": park_info,
            "weather": weather,
            "away_pitcher_score": round(away_p_score, 1),
            "home_pitcher_score": round(home_p_score, 1),
            "home_lineup_threat": round(home_lineup_threat, 1),
            "away_lineup_threat": round(away_lineup_threat, 1),
            "nrfi": nrfi,
            # Per-side adjustments for F5 decomposition
            # Convention: positive = run-suppressing (good for the pitcher's side)
            "away_fi_adj": round(away_fi_adj, 1),
            "home_fi_adj": round(home_fi_adj, 1),
            "home_bvp_adj": round(home_bvp_adj, 1),   # home batters vs away pitcher
            "away_bvp_adj": round(away_bvp_adj, 1),   # away batters vs home pitcher
            "home_platoon_adj": round(home_platoon_adj, 1),
            "away_platoon_adj": round(away_platoon_adj, 1),
            "home_streak_adj": round(home_streak_adj, 1),
            "away_streak_adj": round(away_streak_adj, 1),
            "away_rest_adj": round(away_rest_adj, 1),
            "home_rest_adj": round(home_rest_adj, 1),
            # Bullpen game flags
            "away_bullpen": away_bp,
            "home_bullpen": home_bp,
            "has_bullpen_game": away_bp["is_bullpen"] or home_bp["is_bullpen"],
        }

        # --- F5 (First 5 Innings) scoring ---
        f5 = compute_f5_scores(result)
        result["f5"] = f5

        results.append(result)
        tier_emoji = {"STRONG": "🟢", "LEAN": "🟡", "TOSS-UP": "🟠", "FADE": "🔴"}.get(nrfi["tier"], "⚪")
        print(f"  → NRFI: {nrfi['score']} ({nrfi['tier']}) {tier_emoji}")

        # Print F5 summary
        f5_ml = f5["ml"]
        f5_total = f5["total"]
        f5_spread = f5["spread"]
        ml_arrow = "→" if f5_ml["confidence"] in ("TOSS-UP",) else "►"
        print(f"  → F5 ML: {f5_ml['pick']} ({f5_ml['confidence']}, edge {f5_ml['edge']:+.1f})")
        print(f"  → F5 Total: {f5_total['projected_total']} proj ({f5_total['lean']} {f5_total['primary_line']}, {f5_total['confidence']})")
        if f5_spread["recommended_line"] != 0:
            print(f"  → F5 Spread: {f5_spread['recommended_label']} ({f5_spread['confidence']})")
        print()

    # Sort by NRFI score descending
    results.sort(key=lambda r: r["nrfi"]["score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# 7. ENTRY POINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    target_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    results = analyze_date(target_date)

    # Save raw JSON
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(os.path.dirname(script_dir), "output")
    os.makedirs(output_dir, exist_ok=True)

    json_path = os.path.join(output_dir, f"nrfi_{target_date}.json")
    with open(json_path, "w") as f:
        json.dump({
            "date": target_date,
            "stats_through": _stats_end_date(target_date),
            "generated": datetime.now().isoformat(),
            "games": results,
        }, f, indent=2)
    print(f"\nJSON saved: {json_path}")
