#!/usr/bin/env python3
"""
First 5 Innings (F5) Betting Analyzer
=======================================
Extends the NRFI system to score First 5 innings markets:
  - F5 Moneyline (which team leads after 5 innings)
  - F5 Spread (run line, typically ±0.5 or ±1.5)
  - F5 Total (over/under runs in first 5 innings)

The F5 module is NOT a standalone pipeline. It consumes the same per-game
data dict that analyze_date() already builds (pitcher stats, lineup stats,
park factor, weather, etc.) and layers F5-specific scoring on top.

Design rationale
----------------
F5 outcomes are driven primarily by the two starting pitchers. Bullpen
variance is (mostly) eliminated, making these lines more modellable than
full-game equivalents. The same data that powers NRFI — pitcher quality,
lineup matchups, park/weather — feeds directly into F5 scoring, just
weighted differently.

Key differences from NRFI scoring:
  - Full-season pitcher stats matter more (not just first inning).
  - Deeper lineup matters (not just top-of-order for 1 inning).
  - Park factor has ~5x more influence (5 innings of exposure).
  - Weather accumulates across 5 innings.
  - First-inning specific metrics are still useful but down-weighted.

Outputs
-------
Each game result dict gets a new "f5" key containing:
  {
      "ml": {"edge": float, "pick": str, "confidence": str, ...},
      "spread": {"edge": float, "pick": str, "line": float, ...},
      "total": {"projected": float, "lean": str, "line": float, ...},
  }
"""

# ---------------------------------------------------------------------------
# F5 MONEYLINE — which side has the pitching/lineup advantage over 5 innings
# ---------------------------------------------------------------------------

def score_pitch_count_efficiency(pitch_eff: dict) -> float:
    """
    Score pitcher pitch-count efficiency for F5 side strength.

    Returns adjustment from roughly -4 to +1.5.
    Positive = efficient starter likely to work deeper into the game.
    Negative = inefficient starter with elevated early-exit risk.
    """
    if not pitch_eff.get("has_data"):
        return 0.0

    ppi = pitch_eff.get("avg_pitches_per_inning")
    starts = pitch_eff.get("starts_sample", 0) or 0
    if ppi is None or starts <= 0:
        return 0.0

    adj = 0.0

    # High P/IP increases likelihood of early hook before 5 full innings.
    if ppi >= 19.0:
        adj -= 4.0
    elif ppi >= 18.0:
        adj -= 3.0
    elif ppi >= 17.0:
        adj -= 2.0
    elif ppi >= 16.0:
        adj -= 1.0
    elif ppi <= 13.0:
        adj += 1.5
    elif ppi <= 14.0:
        adj += 0.8

    # Extra penalty if inefficient starts are common in the sample.
    high_ineff_rate = pitch_eff.get("high_inefficiency_rate")
    if high_ineff_rate is not None:
        if high_ineff_rate >= 0.65:
            adj -= 1.0
        elif high_ineff_rate >= 0.45:
            adj -= 0.5

    confidence = min(1.0, starts / 6.0)  # full confidence by ~6 starts
    return round(adj * confidence, 1)


def compute_f5_power_rating(pitcher_score, lineup_threat, fi_adj, bvp_adj,
                            platoon_adj, streak_adj, rest_adj,
                            pitch_eff_adj=0.0):
    """
    Compute a per-side "power rating" for F5 purposes.

    This is conceptually: how good is this team's pitcher at suppressing
    runs over 5 innings, minus how dangerous the opposing lineup is.

    Returns a float — higher = stronger side (better pitcher, weaker
    opposing lineup).
    """
    # Pitcher quality is the dominant driver for F5 (70% weight).
    # Lineup threat is inverted: high threat = bad for that pitcher's side.
    # Adjustments are carried forward at reduced scale for some (FI adj
    # is less relevant over 5 full innings than for NRFI).

    rating = (
        0.70 * pitcher_score +                      # pitcher dominance
        0.30 * (100 - lineup_threat) +               # lineup weakness (inverted)
        fi_adj * 0.3 +                               # FI history: still signal, less weight
        bvp_adj * 1.0 +                              # BvP: full weight — carries across innings
        platoon_adj * 1.0 +                           # Platoon: full weight
        streak_adj * 0.8 +                            # Recent form: slight discount
        rest_adj * 1.2 +                               # Rest: amplified over 5 innings
        pitch_eff_adj * 1.4                            # P/IP efficiency: early-exit risk
    )
    return round(rating, 2)


def compute_f5_moneyline(game: dict) -> dict:
    """
    F5 Moneyline: predict which team leads (or ties) after 5 innings.

    Approach: compute a power rating for each side, then the differential
    determines edge and confidence.

    The "away side" power rating measures: how well the away pitcher
    suppresses the home lineup. The "home side" measures the reverse.

    Per-side adjustment decomposition (fixed):
    Each side's rating uses only the adjustments relevant to THAT matchup:
      away_rating: away pitcher quality + adjustments from home-lineup-vs-away-pitcher
      home_rating: home pitcher quality + adjustments from away-lineup-vs-home-pitcher
    Previously, game-level sums were split 50/50 to both sides, which
    caused all adjustments to cancel out in the differential.
    """
    away_pitch_eff_adj = score_pitch_count_efficiency(game.get("away_pitch_eff", {}))
    home_pitch_eff_adj = score_pitch_count_efficiency(game.get("home_pitch_eff", {}))

    # Away side: away pitcher suppressing the home lineup.
    # - fi_adj: away pitcher's first-inning track record
    # - bvp_adj: home batters vs away pitcher (positive = batters struggle = good for away side)
    # - platoon_adj: home lineup's platoon situation vs away pitcher hand
    # - streak_adj: home lineup's hot/cold streak (positive = cold = good for away side)
    # - rest_adj: away pitcher's rest/workload
    away_rating = compute_f5_power_rating(
        pitcher_score=game.get("away_pitcher_score", 50),
        lineup_threat=game.get("home_lineup_threat", 50),
        fi_adj=game.get("away_fi_adj", 0),
        bvp_adj=game.get("home_bvp_adj", 0),
        platoon_adj=game.get("home_platoon_adj", 0),
        streak_adj=game.get("home_streak_adj", 0),
        rest_adj=game.get("away_rest_adj", 0),
        pitch_eff_adj=away_pitch_eff_adj,
    )

    # Home side: home pitcher suppressing the away lineup.
    home_rating = compute_f5_power_rating(
        pitcher_score=game.get("home_pitcher_score", 50),
        lineup_threat=game.get("away_lineup_threat", 50),
        fi_adj=game.get("home_fi_adj", 0),
        bvp_adj=game.get("away_bvp_adj", 0),
        platoon_adj=game.get("away_platoon_adj", 0),
        streak_adj=game.get("away_streak_adj", 0),
        rest_adj=game.get("home_rest_adj", 0),
        pitch_eff_adj=home_pitch_eff_adj,
    )

    # Park and weather adjustments are symmetric (affect both sides equally
    # for total runs, but for ML they shift who benefits).
    # Hitter-friendly parks benefit the better offense; pitcher-friendly
    # parks benefit the better pitcher. We apply park/weather as a slight
    # boost to the side with the offensive advantage.
    nrfi = game.get("nrfi", {})
    park_adj = nrfi.get("park_adj", 0)
    weather_adj = nrfi.get("weather_adj", 0)

    # The side with the WEAKER pitcher (lower score) is hurt more by
    # hitter-friendly conditions. park_adj is positive for pitcher parks.
    # So negative park_adj (hitter-friendly) widens the pitcher gap.
    away_rating += park_adj * 0.3
    home_rating += park_adj * 0.3

    differential = away_rating - home_rating

    # Map differential to a pick and confidence.
    # Thresholds are intentionally tight — F5 markets are efficient and
    # a model edge of 5 points corresponds to a modest real advantage.
    # We want 1-3 STRONG picks per day, not 8.
    if abs(differential) < 3:
        confidence = "TOSS-UP"
    elif abs(differential) < 7:
        confidence = "LEAN"
    elif abs(differential) < 12:
        confidence = "MODERATE"
    else:
        confidence = "STRONG"

    if differential > 0.5:
        pick = game.get("away_abbr", "AWAY")
        pick_full = game.get("away_team", "Away")
    elif differential < -0.5:
        pick = game.get("home_abbr", "HOME")
        pick_full = game.get("home_team", "Home")
    else:
        pick = "PICK"   # too close to call
        pick_full = "Pick'em"

    return {
        "away_rating": away_rating,
        "home_rating": home_rating,
        "edge": round(differential, 1),
        "pick": pick,
        "pick_full": pick_full,
        "confidence": confidence,
        "away_pitch_eff_adj": away_pitch_eff_adj,
        "home_pitch_eff_adj": home_pitch_eff_adj,
    }


# ---------------------------------------------------------------------------
# F5 SPREAD — run line advantage adjusted for 5-inning context
# ---------------------------------------------------------------------------

def compute_f5_spread(game: dict, f5_ml: dict) -> dict:
    """
    F5 Spread: can the favored side cover -0.5 (or -1.5)?

    The standard F5 spread is -0.5 / +0.5 (which is really just ML with
    different juice). The -1.5 line is where real value lives — it requires
    predicting a multi-run edge.

    We use the ML differential to project spread coverage probability.
    """
    edge = f5_ml["edge"]
    pick = f5_ml["pick"]

    # Determine if the favorite can cover common spread lines
    abs_edge = abs(edge)

    # -0.5 spread (equivalent to ML): favored side needs to lead by 1+
    cover_half = abs_edge >= 3.0
    # -1.5 spread: favored side needs to lead by 2+
    cover_1_5 = abs_edge >= 10.0  # need a very strong edge for -1.5

    # Confidence in spread coverage
    if abs_edge >= 14:
        spread_confidence = "STRONG"
    elif abs_edge >= 10:
        spread_confidence = "LEAN"
    elif abs_edge >= 5:
        spread_confidence = "SLIGHT"
    else:
        spread_confidence = "TOSS-UP"

    # Which line to recommend
    if cover_1_5:
        rec_line = -1.5
        rec_label = f"{pick} -1.5"
    elif cover_half:
        rec_line = -0.5
        rec_label = f"{pick} -0.5"
    else:
        rec_line = 0
        rec_label = "No spread play"

    return {
        "edge": round(edge, 1),
        "abs_edge": round(abs_edge, 1),
        "pick": pick,
        "covers_half": cover_half,
        "covers_1_5": cover_1_5,
        "recommended_line": rec_line,
        "recommended_label": rec_label,
        "confidence": spread_confidence,
    }


# ---------------------------------------------------------------------------
# F5 TOTAL — projected run environment over 5 innings
# ---------------------------------------------------------------------------

# Baseline: MLB average is roughly 4.5 runs per game full, ~2.5 over F5.
# The most common F5 O/U line is 4.5 or 5.
F5_BASELINE_TOTAL = 4.5
F5_SIDE_BASE_RUNS = 2.25
F5_PITCHER_SCALE = 0.60
F5_LINEUP_SCALE = 0.35
F5_FI_ADJ_SCALE = 0.02
F5_BVP_ADJ_SCALE = 0.04
F5_PLATOON_ADJ_SCALE = 0.04
F5_STREAK_ADJ_SCALE = 0.03
F5_REST_ADJ_SCALE = 0.05

def _estimate_f5_runs(pitcher_score, lineup_threat, fi_adj, bvp_adj,
                      platoon_adj, streak_adj, rest_adj):
    """
    Estimate how many runs ONE side allows over 5 innings based on
    pitcher quality, opposing lineup, and adjustments.

    Returns estimated runs (float).

    Calibration: a perfectly average game (pitcher_score=50, lineup=50,
    no adjustments) should project ~2.25 runs per side (4.5 total for F5).
    """
    # Start from a baseline of 2.25 runs per side (half of F5 total)
    base = F5_SIDE_BASE_RUNS

    # Pitcher quality: each point above 50 reduces runs
    # Scale: a 70-score pitcher might allow ~1.5 runs; a 30-score ~3.0
    pitcher_delta = (50 - pitcher_score) / 50.0  # positive = bad pitcher
    runs_from_pitcher = base * (1 + pitcher_delta * F5_PITCHER_SCALE)

    # Lineup threat: each point above 50 adds runs
    lineup_delta = (lineup_threat - 50) / 50.0  # positive = dangerous lineup
    runs_from_lineup = runs_from_pitcher * (1 + lineup_delta * F5_LINEUP_SCALE)

    # Adjustments — convert from NRFI-scale (positive=good for pitcher)
    # to runs (positive = more runs scored). Flip the sign.
    adj_runs = 0
    adj_runs -= fi_adj * F5_FI_ADJ_SCALE       # FI adj: minor influence over 5 inn
    adj_runs -= bvp_adj * F5_BVP_ADJ_SCALE     # BvP: meaningful
    adj_runs -= platoon_adj * F5_PLATOON_ADJ_SCALE  # platoon: meaningful
    adj_runs -= streak_adj * F5_STREAK_ADJ_SCALE    # streaks: moderate
    adj_runs -= rest_adj * F5_REST_ADJ_SCALE   # rest: amplified over 5 innings

    total = runs_from_lineup + adj_runs
    return max(0.5, round(total, 2))  # floor at 0.5 — can't be negative


def compute_f5_total(game: dict) -> dict:
    """
    F5 Total: project total runs scored by both teams in the first 5 innings.

    Common F5 total lines are 4.5 or 5. The model projects actual run
    expectations and compares against these common lines.
    """
    nrfi = game.get("nrfi", {})

    # Runs allowed by away pitcher (to home lineup) over 5 innings
    # Uses away pitcher's adjustments + home lineup's matchup adjustments
    home_runs = _estimate_f5_runs(
        pitcher_score=game.get("away_pitcher_score", 50),
        lineup_threat=game.get("home_lineup_threat", 50),
        fi_adj=game.get("away_fi_adj", 0),
        bvp_adj=game.get("home_bvp_adj", 0),
        platoon_adj=game.get("home_platoon_adj", 0),
        streak_adj=game.get("home_streak_adj", 0),
        rest_adj=game.get("away_rest_adj", 0),
    )

    # Runs allowed by home pitcher (to away lineup) over 5 innings
    # Uses home pitcher's adjustments + away lineup's matchup adjustments
    away_runs = _estimate_f5_runs(
        pitcher_score=game.get("home_pitcher_score", 50),
        lineup_threat=game.get("away_lineup_threat", 50),
        fi_adj=game.get("home_fi_adj", 0),
        bvp_adj=game.get("away_bvp_adj", 0),
        platoon_adj=game.get("away_platoon_adj", 0),
        streak_adj=game.get("away_streak_adj", 0),
        rest_adj=game.get("home_rest_adj", 0),
    )

    # Park and weather: both shift total run environment
    park_adj = nrfi.get("park_adj", 0)
    weather_adj = nrfi.get("weather_adj", 0)

    # park_adj is positive for pitcher parks (NRFI-friendly) → fewer runs
    # Multiply: each point of park_adj ≈ 0.08 runs over 5 innings
    env_adj = -(park_adj + weather_adj) * 0.08

    projected_total = home_runs + away_runs + env_adj
    projected_total = max(1.0, round(projected_total, 1))

    # Team tendency adjustment — teams that score a lot in early innings
    team_tendency_adj = nrfi.get("team_tendency_adj", 0)
    projected_total -= team_tendency_adj * 0.06  # negative tendency = more scoring

    projected_total = max(1.0, round(projected_total, 1))

    # Compare against common lines
    common_lines = [4.0, 4.5, 5.0, 5.5]
    line_calls = {}
    for line in common_lines:
        diff = projected_total - line
        if diff > 0.5:
            lean = "OVER"
            conf = "STRONG" if diff > 1.2 else "LEAN"
        elif diff > 0.15:
            lean = "OVER"
            conf = "SLIGHT"
        elif diff < -0.5:
            lean = "UNDER"
            conf = "STRONG" if diff < -1.2 else "LEAN"
        elif diff < -0.15:
            lean = "UNDER"
            conf = "SLIGHT"
        else:
            lean = "PUSH"
            conf = "TOSS-UP"
        line_calls[str(line)] = {"lean": lean, "confidence": conf, "diff": round(diff, 1)}

    # Primary recommendation: use 4.5 as the default F5 line
    primary_line = 4.5
    primary_call = line_calls.get("4.5", {})

    return {
        "projected_total": projected_total,
        "home_runs_proj": round(home_runs, 1),
        "away_runs_proj": round(away_runs, 1),
        "env_adj": round(env_adj, 2),
        "primary_line": primary_line,
        "lean": primary_call.get("lean", "PUSH"),
        "confidence": primary_call.get("confidence", "TOSS-UP"),
        "line_calls": line_calls,
    }


# ---------------------------------------------------------------------------
# MAIN F5 SCORING — called per game after NRFI analysis is complete
# ---------------------------------------------------------------------------

def compute_f5_scores(game: dict) -> dict:
    """
    Compute all F5 predictions for a single game.

    Expects a game dict that already has 'nrfi', 'away_pitcher_score',
    'home_pitcher_score', 'home_lineup_threat', 'away_lineup_threat',
    and all the component data from the NRFI pipeline.

    Returns:
        {
            "ml": { ... },
            "spread": { ... },
            "total": { ... },
        }
    """
    ml = compute_f5_moneyline(game)
    spread = compute_f5_spread(game, ml)
    total = compute_f5_total(game)

    return {
        "ml": ml,
        "spread": spread,
        "total": total,
    }


# ---------------------------------------------------------------------------
# IMPROVEMENT NOTES
# ---------------------------------------------------------------------------
"""
FUTURE IMPROVEMENTS FOR F5 MODELS
===================================

1. PITCHER GAME-LOG DEPTH (HIGH PRIORITY)
   - Currently we use season-level ERA/WHIP/K9/BB9. For F5, we should pull
     per-start game logs and compute "first 5 innings" specific stats:
     runs allowed through 5 IP, pitch count at 5 IP, batting average against
     through the order the first time. The MLB Stats API game log endpoint
     already returns per-game IP — filter to starts where the pitcher went
     5+ IP and compute their F5-specific ERA.

2. PITCH COUNT EFFICIENCY (HIGH PRIORITY)
   - Pitchers who throw lots of pitches per inning are more likely to get
     pulled before completing 5 IP. This is critical for F5 bets because if
     the SP exits in the 4th or 5th, you're exposed to bullpen variance.
     Track pitches-per-inning from game logs and penalize inefficient starters.

3. BULLPEN EXPOSURE RISK (MEDIUM PRIORITY)
   - Even in F5, the starter sometimes doesn't finish 5 innings. Model the
     probability of early exit (based on recent pitch counts, IP per start)
     and factor in the team's bullpen quality for the "bridge" innings.

4. TIMES THROUGH THE ORDER (MEDIUM PRIORITY)
   - Batters perform better the 2nd and 3rd time through the order. By the
     4th-5th innings, the top of the order is seeing the pitcher a second
     time. This "times through order" penalty should be modeled — it affects
     F5 totals more than NRFI.

5. HOME FIELD ADVANTAGE FOR F5 ML (MEDIUM PRIORITY)
   - Home teams bat last. In F5, this matters: the home team always gets
     their 5th inning at-bat. A small home-field edge (~1-2 points on the
     power rating) should be added.

6. UMPIRE TENDENCIES (LOW-MEDIUM PRIORITY)
   - Home plate umpire strike zone data (via MLB Stats API or external
     sources) directly affects run scoring. A tight-zone ump suppresses
     offense; a wide-zone ump inflates strikeouts. This is especially
     impactful for F5 totals.

7. HISTORICAL F5 LINE CALIBRATION (HIGH PRIORITY — BACKTESTING)
   - The current run projection is calibrated from general principles. To
     make it sharp, we need to backtest: for completed games, compute the
     actual F5 total (sum of runs innings 1-5) from linescores, then
     compare against projections. Adjust the _estimate_f5_runs() scaling
     factors until the model's projected totals match historical medians
     by pitcher-quality tier.

8. F5 ODDS INTEGRATION
   - Just like NRFI odds, pull F5 ML / spread / total odds from The Odds
     API. Compare model edge vs market implied probability to find value.
     This is the most direct path to profitability.

9. LINEUP ORDER DEPTH (LOW PRIORITY)
   - NRFI only cares about the top 3-4 batters. F5 cares about 1-9 because
     most lineups turn over at least once in 5 innings. Expanding batter
     analysis to the full lineup (weighted by expected plate appearances in
     5 innings) would improve accuracy.

10. WEATHER ACCUMULATION (LOW PRIORITY)
    - Hot weather and blowing-out wind compound across innings. The NRFI
      weather adjustment is calibrated for 1 inning. For F5, the effect
      should be scaled up (roughly 2-3x) since fly balls have 5 innings
      of exposure to carry.
"""
