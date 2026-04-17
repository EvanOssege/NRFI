#!/usr/bin/env python3
"""
F5 + NRFI Dashboard Generator
==============================
Takes the JSON output from nrfi_analyzer.py and produces an interactive
single-file HTML dashboard.

REDESIGN (2026-04): F5 is now the headline. NRFI is secondary.
YRFI Watch surfaces games with elevated first-inning run probability.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Mirror of analyzer constant — used for tendency display only.
LEAGUE_FI_SCORE_RATE = 0.27

# YRFI thresholds — calibrated against logged outcomes (Apr 2026):
#   score <25  → 78% YRFI hit rate (n=27) — STRONG
#   score 25-29 → 100% YRFI hit rate (n=5, small) — LEAN
#   score 30+  → ~33-54% (no edge over league baseline) — no flag
# League YRFI baseline ≈ 46%. Re-calibrate as the log grows.
YRFI_STRONG_MAX = 25   # score < 25 → high-confidence YRFI
YRFI_LEAN_MAX = 30     # score < 30 → lean YRFI


def fmt(val, precision=2, fallback="—"):
    """Format a numeric value, or return fallback."""
    if val is None:
        return fallback
    try:
        return f"{float(val):.{precision}f}"
    except (TypeError, ValueError):
        return fallback


def f5_conviction(g):
    """
    Score how confident the F5 model is on this game (0-6).
    Each of ML, Spread, Total contributes: STRONG=2, LEAN/MODERATE=1, else 0.
    Total OVER/UNDER calls only count if not PUSH.
    Used for sorting and top-line headline.
    """
    f5 = g.get("f5") or {}
    score = 0

    def w(conf):
        if conf == "STRONG":
            return 2
        if conf in ("MODERATE", "LEAN"):
            return 1
        return 0

    ml = f5.get("ml", {})
    if ml.get("pick") and ml.get("pick") not in ("—", None, ""):
        score += w(ml.get("confidence", ""))

    sp = f5.get("spread", {})
    if sp.get("recommended_label") and sp.get("recommended_label") != "—":
        score += w(sp.get("confidence", ""))

    tot = f5.get("total", {})
    if tot.get("lean") in ("OVER", "UNDER"):
        score += w(tot.get("confidence", ""))

    return score


def f5_conviction_tier(score):
    """Map conviction score → tier label."""
    if score >= 5:
        return "F5 LOCK"
    if score >= 3:
        return "F5 STRONG"
    if score >= 2:
        return "F5 LEAN"
    if score >= 1:
        return "F5 SLIGHT"
    return "F5 PASS"


def yrfi_status(nrfi_score):
    """Return (label, color, tier) for YRFI lean. None if no signal."""
    if nrfi_score is None:
        return None
    if nrfi_score < YRFI_STRONG_MAX:
        return ("YRFI STRONG", "#ef4444", "STRONG")
    if nrfi_score < YRFI_LEAN_MAX:
        return ("YRFI LEAN", "#fb923c", "LEAN")
    return None


def generate_dashboard(data: dict, output_path: str):
    games = list(data.get("games", []))
    analysis_date = data.get("date", "Unknown")
    generated = data.get("generated", "")
    stats_through = data.get("stats_through")
    if not stats_through:
        try:
            stats_through = (datetime.fromisoformat(analysis_date) - timedelta(days=1)).date().isoformat()
        except Exception:
            stats_through = "Unknown"

    # --- Sort: F5 conviction first, NRFI strength second, game time third ---
    nrfi_tier_rank = {"STRONG": 0, "LEAN": 1, "TOSS-UP": 2, "FADE": 3}
    def sort_key(g):
        return (
            -f5_conviction(g),
            nrfi_tier_rank.get(g["nrfi"]["tier"], 4),
            g.get("game_time", "")
        )
    games.sort(key=sort_key)

    # --- Counts for summary bar ---
    f5_lock_count = sum(1 for g in games if f5_conviction(g) >= 5)
    f5_strong_count = sum(1 for g in games if 3 <= f5_conviction(g) < 5)
    f5_lean_count = sum(1 for g in games if f5_conviction(g) == 2)

    yrfi_strong_count = sum(1 for g in games if (g["nrfi"]["score"] < YRFI_STRONG_MAX))
    yrfi_lean_count = sum(1 for g in games if (YRFI_STRONG_MAX <= g["nrfi"]["score"] < YRFI_LEAN_MAX))

    nrfi_strong = [g for g in games if g["nrfi"]["tier"] == "STRONG"]
    nrfi_lean = [g for g in games if g["nrfi"]["tier"] == "LEAN"]

    # ---------------- shared color helpers ----------------
    def conf_color(conf):
        return {
            "STRONG": "#22c55e", "MODERATE": "#4ade80", "LEAN": "#eab308",
            "SLIGHT": "#fbbf24", "TOSS-UP": "#94a3b8", "PASS": "#64748b",
        }.get(conf, "#6b7280")

    def lean_color(lean):
        return {"OVER": "#ef4444", "UNDER": "#22c55e", "PUSH": "#94a3b8"}.get(lean, "#6b7280")

    def units_color(u):
        if u is None or u <= 0: return "#64748b"
        if u <= 1.0:            return "#94a3b8"
        if u <= 2.5:            return "#eab308"
        if u < 5.0:             return "#4ade80"
        return "#22c55e"

    def units_badge(u):
        if u is None:
            return ""
        c = units_color(u)
        return (f'<span class="f5-units-pill" style="background:{c};color:#000">'
                f'{u:.1f}u</span>')

    def nrfi_tier_color(tier):
        return {"STRONG": "#22c55e", "LEAN": "#eab308",
                "TOSS-UP": "#f97316", "FADE": "#ef4444"}.get(tier, "#6b7280")

    def f5_card_border_color(conviction):
        if conviction >= 5: return "#22c55e"
        if conviction >= 3: return "#4ade80"
        if conviction >= 2: return "#eab308"
        if conviction >= 1: return "#94a3b8"
        return "#475569"

    # ---------------- formatting helpers ----------------
    def weather_str(w):
        if not w:
            return "—"
        if w.get("indoor"):
            return "🏟️ Indoor (72°F)"
        parts = []
        if w.get("temp_f") is not None:
            parts.append(f"{w['temp_f']:.0f}°F")
        if w.get("wind_mph") is not None:
            parts.append(f"{w['wind_mph']:.0f} mph wind")
        if w.get("precip_chance") is not None:
            parts.append(f"{w['precip_chance']}% rain")
        return " · ".join(parts) if parts else "—"

    def park_badge(park):
        if not park:
            return ""
        factor = park.get("factor", 100)
        tier = park.get("tier", "neutral")
        label = park.get("label", "Neutral")
        if tier == "hitter":
            return f'<span class="park-badge park-hitter" title="Park Factor: {factor}">⚾ {factor} {label}</span>'
        elif tier == "pitcher":
            return f'<span class="park-badge park-pitcher" title="Park Factor: {factor}">⚾ {factor} {label}</span>'
        else:
            return f'<span class="park-badge park-neutral" title="Park Factor: {factor}">⚾ {factor}</span>'

    def fi_badge(fi_stats):
        if not fi_stats or not fi_stats.get("has_data"):
            return '<span class="fi-badge fi-neutral">1st: No data</span>'
        fi_era = fi_stats.get("fi_era")
        clean_pct = fi_stats.get("fi_clean_pct", 0)
        starts = fi_stats.get("fi_starts", 0)
        clean = fi_stats.get("fi_clean", 0)
        if fi_era is not None and fi_era <= 2.50:
            cls = "fi-good"
        elif fi_era is not None and fi_era <= 4.00:
            cls = "fi-neutral"
        else:
            cls = "fi-bad"
        era_str = f"{fi_era:.2f}" if fi_era is not None else "—"
        pct_str = f"{clean_pct*100:.0f}%"
        return f'<span class="fi-badge {cls}" title="{clean}/{starts} clean 1st innings">1st ERA {era_str} · {pct_str} clean ({starts}GS)</span>'

    def hand_badge(hand):
        if hand == "L":
            return '<span class="hand-badge hand-left">LHP</span>'
        elif hand == "R":
            return '<span class="hand-badge hand-right">RHP</span>'
        return ''

    def pitcher_card(name, stats, score, hand="?", fi_stats=None):
        era = fmt(stats.get("era"))
        whip = fmt(stats.get("whip"))
        k9 = fmt(stats.get("k9"), 1)
        bb9 = fmt(stats.get("bb9"), 1)
        hr9 = fmt(stats.get("hr9"), 1)
        ip = fmt(stats.get("ip"), 1)
        score_color = '#22c55e' if score >= 65 else '#eab308' if score >= 50 else '#ef4444'
        return f"""
        <div class="pitcher-card">
          <div class="pitcher-name">{name} {hand_badge(hand)}</div>
          <div class="pitcher-score" style="color: {score_color}">{score}</div>
          <div class="stat-grid">
            <div class="stat"><span class="stat-label">ERA</span><span class="stat-value">{era}</span></div>
            <div class="stat"><span class="stat-label">WHIP</span><span class="stat-value">{whip}</span></div>
            <div class="stat"><span class="stat-label">K/9</span><span class="stat-value">{k9}</span></div>
            <div class="stat"><span class="stat-label">BB/9</span><span class="stat-value">{bb9}</span></div>
            <div class="stat"><span class="stat-label">HR/9</span><span class="stat-value">{hr9}</span></div>
            <div class="stat"><span class="stat-label">IP</span><span class="stat-value">{ip}</span></div>
          </div>
          <div class="fi-row">{fi_badge(fi_stats)}</div>
        </div>"""

    def bvp_cell(bvp):
        if not bvp or not bvp.get("has_data") or bvp.get("ab", 0) == 0:
            return '<span style="color:#6b7280">—</span>'
        ab = bvp["ab"]; h = bvp["hits"]; hr = bvp["hr"]
        avg = bvp.get("avg", "—"); ops = bvp.get("ops", "—")
        try:
            ops_f = float(ops)
            color = "#ef4444" if ops_f >= .800 else "#eab308" if ops_f >= .650 else "#22c55e"
        except (TypeError, ValueError):
            color = "#6b7280"
        return f'<span style="color:{color}" title="{h}-for-{ab}, {hr} HR">{avg}/{ops} ({ab}AB)</span>'

    def bvp_summary_badge(bvp_sum):
        if not bvp_sum or not bvp_sum.get("has_meaningful_data"):
            ab = bvp_sum.get("total_ab", 0) if bvp_sum else 0
            if ab > 0:
                return f'<span class="bvp-badge bvp-neutral">BvP: {ab}AB (limited)</span>'
            return '<span class="bvp-badge bvp-neutral">BvP: No history</span>'
        w_ops = bvp_sum.get("weighted_ops")
        ab = bvp_sum["total_ab"]; hr = bvp_sum["total_hr"]
        if w_ops is not None:
            if w_ops >= .800:
                cls, label = "bvp-danger", "Batters own SP"
            elif w_ops >= .700:
                cls, label = "bvp-warn", "Slight batter edge"
            elif w_ops <= .550:
                cls, label = "bvp-safe", "SP dominates"
            elif w_ops <= .650:
                cls, label = "bvp-good", "SP has edge"
            else:
                cls, label = "bvp-neutral", "Even matchup"
            return f'<span class="bvp-badge {cls}">BvP: .{str(w_ops)[2:5]} OPS · {ab}AB · {hr}HR — {label}</span>'
        return f'<span class="bvp-badge bvp-neutral">BvP: {ab}AB</span>'

    def streak_cell(recent):
        if not recent or not recent.get("has_data"):
            return '<span style="color:#6b7280">—</span>'
        status = recent.get("streak_status", "unknown")
        ops = recent.get("ops", "—"); delta = recent.get("ops_delta")
        avg = recent.get("avg", "—"); games = recent.get("games", 7)
        icons = {"hot": "🔥", "warm": "📈", "neutral": "➖", "cool": "📉", "cold": "🧊"}
        colors = {"hot": "#ef4444", "warm": "#fb923c", "neutral": "#9ca3af", "cool": "#60a5fa", "cold": "#38bdf8"}
        icon = icons.get(status, ""); color = colors.get(status, "#6b7280")
        delta_str = f" ({'+' if delta >= 0 else ''}{delta:.3f})" if delta is not None else ""
        return f'<span style="color:{color}" title="Last {games}G: {avg} AVG / {ops} OPS{delta_str}">{icon} {avg}/{ops}</span>'

    def platoon_summary_badge(platoon_sum):
        if not platoon_sum or not platoon_sum.get("has_data"):
            return '<span class="bvp-badge bvp-neutral">L/R: No data</span>'
        vs_hand = platoon_sum.get("vs_hand", "?")
        w_ops = platoon_sum.get("weighted_ops")
        adv = platoon_sum.get("advantage_count", 0); disadv = platoon_sum.get("disadvantage_count", 0)
        if w_ops is None:
            return f'<span class="bvp-badge bvp-neutral">vs {vs_hand}HP: No splits</span>'
        if w_ops >= .800: cls, label = "bvp-danger", "Lineup rakes"
        elif w_ops >= .700: cls, label = "bvp-warn", "Lineup hits well"
        elif w_ops <= .580: cls, label = "bvp-safe", "Lineup struggles"
        elif w_ops <= .650: cls, label = "bvp-good", "SP has edge"
        else: cls, label = "bvp-neutral", "Neutral"
        ops_str = f".{str(w_ops)[2:5]}"
        comp_str = f"{disadv}dis/{adv}adv" if (adv + disadv) > 0 else ""
        return f'<span class="bvp-badge {cls}">vs {vs_hand}HP: {ops_str} OPS · {comp_str} — {label}</span>'

    def platoon_cell(platoon_data):
        if not platoon_data or not platoon_data.get("has_data"):
            return '<span style="color:#6b7280">—</span>'
        ops = platoon_data.get("ops", "—")
        bat_side = platoon_data.get("bat_side", "?")
        vs_hand = platoon_data.get("vs_hand", "?")
        ab = platoon_data.get("ab", 0)
        try:
            ops_f = float(ops)
            color = "#ef4444" if ops_f >= .800 else "#eab308" if ops_f >= .650 else "#22c55e"
        except (TypeError, ValueError):
            color = "#6b7280"
        side_label = "S" if bat_side == "S" else bat_side
        return f'<span style="color:{color}" title="{side_label}HB vs {vs_hand}HP ({ab}AB)">{side_label} {ops}</span>'

    def streak_summary_badge(streak_sum):
        if not streak_sum or not streak_sum.get("has_data"):
            return '<span class="bvp-badge bvp-neutral">Form: No data</span>'
        avg_delta = streak_sum.get("avg_ops_delta")
        hot = streak_sum.get("hot_count", 0); cold = streak_sum.get("cold_count", 0)
        recent_ops = streak_sum.get("avg_recent_ops")
        if avg_delta is None:
            return '<span class="bvp-badge bvp-neutral">Form: N/A</span>'
        if avg_delta <= -.150: cls, label = "bvp-safe", "Ice cold"
        elif avg_delta <= -.050: cls, label = "bvp-good", "Cooling off"
        elif avg_delta >= .150: cls, label = "bvp-danger", "On fire"
        elif avg_delta >= .050: cls, label = "bvp-warn", "Heating up"
        else: cls, label = "bvp-neutral", "Steady"
        ops_str = f".{str(recent_ops)[2:5]}" if recent_ops else "?"
        delta_str = f"{'+' if avg_delta >= 0 else ''}{avg_delta:.3f}"
        return f'<span class="bvp-badge {cls}">L7: {ops_str} OPS ({delta_str}) · {hot}🔥 {cold}🧊 — {label}</span>'

    def batter_rows(top_order):
        batters = top_order.get("batters", [])
        if not batters:
            return '<tr><td colspan="9" style="text-align:center;color:#6b7280;">No data</td></tr>'
        rows = ""
        for b in batters:
            rows += f"""<tr>
              <td>{b['name']}</td>
              <td>{b['avg']}</td><td>{b['obp']}</td><td>{b['ops']}</td>
              <td>{platoon_cell(b.get('platoon', {}))}</td>
              <td>{streak_cell(b.get('recent', {}))}</td>
              <td>{b['hr']}</td>
              <td>{bvp_cell(b.get('bvp', {}))}</td>
            </tr>"""
        return rows

    def team_tendency_panel(g):
        ht = g.get("home_team_tendency") or {}
        at = g.get("away_team_tendency") or {}
        meta = g.get("team_tendency_meta") or {}
        if not ht.get("has_data") and not at.get("has_data"):
            return ""
        def cell(label, t, side):
            if not t.get("has_data"):
                return f'<div class="tend-cell"><div class="tend-label">{label}</div><div class="tend-val">—</div></div>'
            scored = t.get(f"{side}_scored", 0); n = t.get(f"{side}_games", 0)
            rate = t.get(f"{side}_score_rate"); overall = t.get("overall_score_rate", 0)
            ttl = t.get("games", 0)
            rate_str = f"{rate:.0%}" if rate is not None and n > 0 else "—"
            if rate is None or n == 0: color = "#888"
            elif rate <= 0.20: color = "#1fb86e"
            elif rate <= 0.27: color = "#9bd15a"
            elif rate <= 0.33: color = "#ffb84d"
            else: color = "#ff5e5e"
            return (f'<div class="tend-cell"><div class="tend-label">{label}</div>'
                    f'<div class="tend-val" style="color:{color}">{rate_str} <span class="tend-sub">({scored}/{n})</span></div>'
                    f'<div class="tend-sub">overall {overall:.0%} · {ttl}g</div></div>')
        adj = meta.get("adj", 0)
        adj_color = "#1fb86e" if adj > 0 else ("#ff5e5e" if adj < 0 else "#aaa")
        adj_str = f"{'+' if adj >= 0 else ''}{adj}"
        blended = meta.get("blended_rate")
        blended_str = f"{blended:.0%}" if blended is not None else "—"
        return f"""
        <div class="tend-panel">
          <div class="tend-header"><span class="tend-title">Team 1st-Inning Tendencies (last 30g)</span>
            <span class="tend-adj" style="color:{adj_color}">Adj: {adj_str}</span></div>
          <div class="tend-grid">
            {cell(f"{g['home_abbr']} batting at HOME", ht, "home")}
            {cell(f"{g['away_abbr']} batting on ROAD", at, "away")}
          </div>
          <div class="tend-foot">Blended {blended_str} vs league {LEAGUE_FI_SCORE_RATE:.0%} · shrunk toward team-overall + league prior</div>
        </div>"""

    def lineup_source_badge(g):
        home_real = g.get("home_real_lineup", False)
        away_real = g.get("away_real_lineup", False)
        if home_real and away_real:
            return '<span class="lineup-src lineup-confirmed">✓ Official lineups</span>'
        elif home_real or away_real:
            return '<span class="lineup-src lineup-partial">⚠ Partial — one side estimated</span>'
        else:
            return '<span class="lineup-src lineup-estimated">⚠ Estimated from roster</span>'

    def _pretty_book(key):
        return {"fanduel": "FanDuel", "draftkings": "DraftKings", "betmgm": "BetMGM",
                "caesars": "Caesars", "pointsbet": "PointsBet", "betrivers": "BetRivers",
                "bovada": "Bovada", "betonlineag": "BetOnline", "mybookieag": "MyBookie",
                "wynnbet": "WynnBET", "espnbet": "ESPN BET", "fanatics": "Fanatics",
                "hardrockbet": "Hard Rock", "bet365": "bet365"}.get(key, key.replace("_", " ").title())

    def odds_badge(g):
        odds = g.get("odds", {})
        if not odds.get("has_odds"): return ""
        price = odds.get("nrfi_price")
        impl_prob = odds.get("nrfi_implied_prob")
        if price is None: return ""
        price_str = f"+{price}" if price > 0 else str(price)
        prob_str = f"{impl_prob:.0%}" if impl_prob else ""
        model_prob = g["nrfi"]["score"] / 100.0
        value_diff = model_prob - (impl_prob or 0)
        if value_diff >= 0.08: value_label, value_color = "VALUE", "#22c55e"
        elif value_diff >= 0.03: value_label, value_color = "FAIR", "#eab308"
        elif value_diff >= -0.03: value_label, value_color = "CLOSE", "#94a3b8"
        else: value_label, value_color = "NO VALUE", "#ef4444"
        book_name = _pretty_book(odds.get("book", ""))
        return (f'<span class="odds-badge"><span class="odds-book">NRFI {book_name}</span> '
                f'<span class="odds-price">{price_str}</span> '
                f'<span class="odds-prob">({prob_str})</span> '
                f'<span class="odds-value" style="color:{value_color}">{value_label}</span></span>')

    # ---------------- F5 Headline (NEW — large, top-of-card) ----------------
    def f5_headline(g):
        f5 = g.get("f5") or {}
        ml = f5.get("ml", {}); spread = f5.get("spread", {}); total = f5.get("total", {})

        def fmt_signed(v):
            try: return f"{float(v):+.1f}"
            except (TypeError, ValueError): return "n/a"

        # ML
        ml_pick = ml.get("pick", "—")
        ml_conf = ml.get("confidence", "TOSS-UP")
        ml_edge = ml.get("edge", 0)
        ml_edge_str = f"{'+' if ml_edge > 0 else ''}{ml_edge}"
        ml_color = conf_color(ml_conf)
        ml_units_badge = units_badge(ml.get("units"))
        away_eff = ml.get("away_pitch_eff_adj"); home_eff = ml.get("home_pitch_eff_adj")
        eff_sub = ""
        if away_eff is not None or home_eff is not None:
            eff_sub = f"<div class=\"f5-cell-extra\">P/IP adj: A {fmt_signed(away_eff)} · H {fmt_signed(home_eff)}</div>"
        away_rating = ml.get("away_rating"); home_rating = ml.get("home_rating")
        rating_sub = ""
        if away_rating is not None and home_rating is not None:
            rating_sub = f"<div class=\"f5-cell-extra\">Rating: A {away_rating:.1f} vs H {home_rating:.1f}</div>"

        # Spread
        sp_label = spread.get("recommended_label", "—")
        sp_conf = spread.get("confidence", "TOSS-UP")
        sp_color = conf_color(sp_conf)

        # Total
        t_proj = total.get("projected_total", "—")
        t_lean = total.get("lean", "PUSH")
        t_conf = total.get("confidence", "TOSS-UP")
        t_line = total.get("primary_line", 4.5)
        t_lean_color = lean_color(t_lean)
        t_units_badge = units_badge(total.get("units"))
        away_rp = total.get("away_runs_proj"); home_rp = total.get("home_runs_proj")
        runs_sub = ""
        if away_rp is not None and home_rp is not None:
            runs_sub = f"<div class=\"f5-cell-extra\">Proj: A {away_rp:.1f} + H {home_rp:.1f}</div>"

        # Line pills (which lines this projection clears)
        line_calls = total.get("line_calls", {})
        line_pills = ""
        for line_val in ["4.0", "4.5", "5.0", "5.5"]:
            call = line_calls.get(line_val, {})
            call_lean = call.get("lean", "PUSH")
            call_conf = call.get("confidence", "TOSS-UP")
            lc = lean_color(call_lean)
            pill_opacity = "1.0" if call_conf in ("STRONG", "LEAN") else "0.55"
            tier_mark = "★" if call_conf == "STRONG" else ("•" if call_conf == "LEAN" else "")
            line_pills += (f'<span class="f5-line-pill" style="opacity:{pill_opacity}" title="{call_conf}">'
                           f'<span class="f5-line-num">{line_val}</span>'
                           f'<span style="color:{lc};font-weight:700">{call_lean} {tier_mark}</span></span>')

        return f"""
        <div class="f5-headline">
          <div class="f5-headline-grid">
            <div class="f5-cell f5-cell-ml">
              <div class="f5-cell-label">F5 MONEYLINE</div>
              <div class="f5-cell-val" style="color:{ml_color}">{ml_pick}</div>
              <div class="f5-cell-sub">Edge {ml_edge_str} · <strong>{ml_conf}</strong> {ml_units_badge}</div>
              {rating_sub}
              {eff_sub}
            </div>
            <div class="f5-cell f5-cell-spread">
              <div class="f5-cell-label">F5 SPREAD</div>
              <div class="f5-cell-val" style="color:{sp_color}">{sp_label}</div>
              <div class="f5-cell-sub"><strong>{sp_conf}</strong></div>
            </div>
            <div class="f5-cell f5-cell-total">
              <div class="f5-cell-label">F5 TOTAL · proj {t_proj}</div>
              <div class="f5-cell-val" style="color:{t_lean_color}">{t_lean} {t_line}</div>
              <div class="f5-cell-sub"><strong>{t_conf}</strong> {t_units_badge}</div>
              {runs_sub}
            </div>
          </div>
          <div class="f5-lines-row">
            <span class="f5-lines-label">Line calls:</span>
            {line_pills}
          </div>
        </div>"""

    # ---------------- Bet Selection Panel (new) ----------------
    def _bet_row_html(game_pk, matchup, market, pick, label,
                      default_units, default_odds,
                      model_confidence="", model_units=None, model_score=None,
                      line_options=None, default_line=None, lean=None):
        key = f"{game_pk}:{market}"
        if market == "F5_TOTAL" and default_line is not None:
            key = f"{game_pk}:{market}:{lean}"

        matchup_safe = (matchup or "").replace('"', '&quot;')
        pick_safe = str(pick).replace('"', '&quot;')

        line_selector = ""
        if line_options:
            opts = ""
            for opt in line_options:
                sel = " selected" if abs(opt - (default_line or 0)) < 1e-9 else ""
                opts += f'<option value="{opt}"{sel}>{opt}</option>'
            line_selector = (
                f'<span class="bet-input-group">'
                f'<span class="bet-input-lbl">Line</span>'
                f'<select class="bet-line" data-role="line">{opts}</select>'
                f'</span>'
            )

        mu_str = f"{float(model_units):.1f}" if model_units is not None else ""
        ms_str = f"{float(model_score):.2f}" if model_score is not None else ""
        units_val = f"{float(default_units):.1f}"
        odds_val = str(int(default_odds)) if float(default_odds).is_integer() else str(default_odds)

        return f"""
      <label class="bet-row" data-bet-key="{key}"
             data-game-pk="{game_pk}"
             data-matchup="{matchup_safe}"
             data-market="{market}"
             data-pick="{pick_safe}"
             data-lean="{lean or ''}"
             data-model-confidence="{model_confidence}"
             data-model-units="{mu_str}"
             data-model-score="{ms_str}">
        <input type="checkbox" class="bet-check" data-role="check">
        <span class="bet-label">Bet {label}</span>
        {line_selector}
        <span class="bet-input-group">
          <span class="bet-input-lbl">Units</span>
          <input type="number" class="bet-units" data-role="units" step="0.5" min="0" value="{units_val}">
        </span>
        <span class="bet-input-group">
          <span class="bet-input-lbl">Odds</span>
          <input type="number" class="bet-odds" data-role="odds" step="5" value="{odds_val}">
        </span>
        <span class="bet-dollars" data-role="dollars">$0</span>
      </label>"""

    def bet_panel(g):
        game_pk = g.get("game_pk")
        matchup = g.get("matchup", "")
        f5 = g.get("f5") or {}
        ml = f5.get("ml", {}) or {}
        total = f5.get("total", {}) or {}
        nrfi = g.get("nrfi") or {}
        odds_data = g.get("odds") or {}

        rows = []

        # F5 Moneyline
        ml_pick = ml.get("pick")
        if ml_pick and ml_pick not in ("—", None, ""):
            default_u = ml.get("units")
            if default_u is None or default_u <= 0:
                default_u = 1.0
            rows.append(_bet_row_html(
                game_pk, matchup, "F5_ML", ml_pick,
                label=f"F5 ML: {ml_pick}",
                default_units=default_u,
                default_odds=-110,
                model_confidence=ml.get("confidence", ""),
                model_units=ml.get("units"),
                model_score=ml.get("edge"),
            ))

        # F5 Total
        t_lean = total.get("lean")
        if t_lean in ("OVER", "UNDER"):
            t_line = total.get("primary_line", 4.5)
            default_u = total.get("units")
            if default_u is None or default_u <= 0:
                default_u = 1.0
            rows.append(_bet_row_html(
                game_pk, matchup, "F5_TOTAL", f"{t_lean} {t_line}",
                label=f"F5 {t_lean}",
                default_units=default_u,
                default_odds=-110,
                line_options=[4.0, 4.5, 5.0, 5.5],
                default_line=float(t_line) if t_line is not None else 4.5,
                lean=t_lean,
                model_confidence=total.get("confidence", ""),
                model_units=total.get("units"),
                model_score=total.get("projected_total"),
            ))

        # NRFI or YRFI (mutually exclusive — show whichever signal fires)
        score = nrfi.get("score")
        tier = nrfi.get("tier")
        if tier in ("STRONG", "LEAN"):
            default_u = 2.0 if tier == "STRONG" else 1.0
            nrfi_price = odds_data.get("nrfi_price")
            default_odds = int(nrfi_price) if isinstance(nrfi_price, (int, float)) else -110
            rows.append(_bet_row_html(
                game_pk, matchup, "NRFI", "NRFI",
                label="NRFI",
                default_units=default_u,
                default_odds=default_odds,
                model_confidence=tier,
                model_units=default_u,
                model_score=score,
            ))
        else:
            yrfi = yrfi_status(score) if score is not None else None
            if yrfi:
                _, _, yrfi_tier = yrfi
                default_u = 2.0 if yrfi_tier == "STRONG" else 1.0
                rows.append(_bet_row_html(
                    game_pk, matchup, "YRFI", "YRFI",
                    label="YRFI",
                    default_units=default_u,
                    default_odds=-110,
                    model_confidence=yrfi_tier,
                    model_units=default_u,
                    model_score=score,
                ))

        if not rows:
            return ""

        return f"""
        <div class="bet-panel">
          <div class="bet-panel-label">Place Bets</div>
          {''.join(rows)}
        </div>"""

    # ---------------- NRFI compact panel (now secondary) ----------------
    def nrfi_compact_panel(g):
        nrfi = g["nrfi"]
        score = nrfi["score"]; tier = nrfi["tier"]
        tier_c = nrfi_tier_color(tier)
        yrfi = yrfi_status(score)
        yrfi_html = ""
        if yrfi:
            yrfi_label, yrfi_color, _ = yrfi
            yrfi_html = (f'<span class="yrfi-inline" style="background:{yrfi_color};color:#000">'
                         f'⚡ {yrfi_label}</span>')
        return f"""
        <details class="nrfi-secondary">
          <summary>
            <span class="nrfi-summary-label">NRFI Analysis</span>
            <span class="nrfi-score-pill" style="background:{tier_c};color:#000">{score} · {tier}</span>
            {yrfi_html}
          </summary>
          <div class="nrfi-body">
            <div class="score-breakdown">
              <span>Pitching: {nrfi['pitcher_component']}</span>
              <span>Lineup: {nrfi['lineup_component']}</span>
              <span>1st Inn: {'+' if nrfi.get('fi_adj', 0) >= 0 else ''}{nrfi.get('fi_adj', 0)}</span>
              <span>BvP: {'+' if nrfi.get('bvp_adj', 0) >= 0 else ''}{nrfi.get('bvp_adj', 0)}</span>
              <span>L/R: {'+' if nrfi.get('platoon_adj', 0) >= 0 else ''}{nrfi.get('platoon_adj', 0)}</span>
              <span>Form: {'+' if nrfi.get('streak_adj', 0) >= 0 else ''}{nrfi.get('streak_adj', 0)}</span>
              <span>Park: {'+' if nrfi.get('park_adj', 0) >= 0 else ''}{nrfi.get('park_adj', 0)}</span>
              <span>Weather: {'+' if nrfi['weather_adj'] >= 0 else ''}{nrfi['weather_adj']}</span>
              <span>Team Tend: {'+' if nrfi.get('team_tendency_adj', 0) >= 0 else ''}{nrfi.get('team_tendency_adj', 0)}</span>
            </div>
            {team_tendency_panel(g)}
          </div>
        </details>
        """

    # ---------------- Game Card (NEW LAYOUT) ----------------
    def game_card(g):
        nrfi = g["nrfi"]
        score = nrfi["score"]
        tier = nrfi["tier"]
        conviction = f5_conviction(g)
        f5_label = f5_conviction_tier(conviction)
        f5_color = f5_card_border_color(conviction)
        yrfi = yrfi_status(score)
        nrfi_tier_c = nrfi_tier_color(tier)

        # data-attributes for filtering
        f5_filter = "lock" if conviction >= 5 else ("strong" if conviction >= 3 else ("lean" if conviction >= 2 else ("slight" if conviction >= 1 else "pass")))
        yrfi_filter = "strong" if (yrfi and yrfi[2] == "STRONG") else ("lean" if (yrfi and yrfi[2] == "LEAN") else "none")
        nrfi_filter = tier.lower().replace("-", "")

        # game time
        try:
            gt_utc = datetime.fromisoformat(g["game_time"].replace("Z", "+00:00"))
            est = timezone(timedelta(hours=-4))
            gt_et = gt_utc.astimezone(est)
            time_str = gt_et.strftime("%-I:%M %p ET")
        except Exception:
            time_str = "TBD"

        # Header badges
        f5_badge = f'<span class="headline-badge f5-badge" style="background:{f5_color};color:#000">{f5_label}</span>'
        yrfi_badge = ""
        if yrfi:
            yrfi_label, yrfi_color, _ = yrfi
            yrfi_badge = f'<span class="headline-badge yrfi-badge" style="background:{yrfi_color};color:#000">⚡ {yrfi_label}</span>'
        nrfi_chip = f'<span class="headline-badge nrfi-mini" title="NRFI score" style="border:1px solid {nrfi_tier_c};color:{nrfi_tier_c}">NRFI {score}</span>'

        # Bullpen game badge
        has_bullpen = g.get("has_bullpen_game", False)
        bullpen_badge_html = ''
        if has_bullpen:
            bullpen_badge_html = '<span class="headline-badge bullpen-badge" style="background:#ff6b35;color:#fff" title="One or both pitchers are relievers — elevated variance">⚠ BULLPEN</span>'

        # Pitcher section labels — flag which side is the bullpen game
        away_bp_flag = (g.get("away_bullpen") or {}).get("is_bullpen", False)
        home_bp_flag = (g.get("home_bullpen") or {}).get("is_bullpen", False)
        away_sp_label = "AWAY RP ⚠ (faces home lineup)" if away_bp_flag else "AWAY SP (faces home lineup)"
        home_sp_label = "HOME RP ⚠ (faces away lineup)" if home_bp_flag else "HOME SP (faces away lineup)"

        return f"""
    <div class="game-card" style="border-left-color: {f5_color}"
         data-f5="{f5_filter}" data-yrfi="{yrfi_filter}" data-nrfi="{nrfi_filter}"
         data-bullpen="{'yes' if has_bullpen else 'no'}">

      <div class="game-header">
        <div class="matchup-row">
          <span class="matchup">{g['matchup']}</span>
          <span class="header-badges">
            {f5_badge}
            {yrfi_badge}
            {nrfi_chip}
            {bullpen_badge_html}
            {odds_badge(g)}
          </span>
        </div>
        <div class="game-meta">{g['venue']} {park_badge(g.get('park'))} · {time_str} · {weather_str(g['weather'])}</div>
      </div>

      <div class="game-body">
        {f5_headline(g)}
        {bet_panel(g)}

        <div class="pitchers-row">
          <div class="pitcher-section">
            <div class="section-label">{away_sp_label}</div>
            {pitcher_card(g['away_pitcher'], g['away_pitcher_stats'], g['away_pitcher_score'], g.get('away_pitcher_hand', '?'), g.get('away_fi'))}
          </div>
          <div class="vs-divider">vs</div>
          <div class="pitcher-section">
            <div class="section-label">{home_sp_label}</div>
            {pitcher_card(g['home_pitcher'], g['home_pitcher_stats'], g['home_pitcher_score'], g.get('home_pitcher_hand', '?'), g.get('home_fi'))}
          </div>
        </div>

        {nrfi_compact_panel(g)}

        <details class="lineup-details">
          <summary>Top-of-Order Batters & Matchup History {lineup_source_badge(g)}</summary>
          <div class="lineups-row">
            <div class="lineup-section">
              <div class="section-label">HOME lineup (vs {g['away_pitcher'].split()[-1] if g['away_pitcher'] != 'TBD' else 'TBD'}) · Threat: {g['home_lineup_threat']}</div>
              <div class="badge-row">{bvp_summary_badge(g.get('home_bvp'))} {streak_summary_badge(g.get('home_streaks'))} {platoon_summary_badge(g.get('home_platoon'))}</div>
              <table class="batter-table">
                <thead><tr><th>Name</th><th>AVG</th><th>OBP</th><th>OPS</th><th>L/R</th><th>Last 7</th><th>HR</th><th>vs SP</th></tr></thead>
                <tbody>{batter_rows(g['home_top_order'])}</tbody>
              </table>
            </div>
            <div class="lineup-section">
              <div class="section-label">AWAY lineup (vs {g['home_pitcher'].split()[-1] if g['home_pitcher'] != 'TBD' else 'TBD'}) · Threat: {g['away_lineup_threat']}</div>
              <div class="badge-row">{bvp_summary_badge(g.get('away_bvp'))} {streak_summary_badge(g.get('away_streaks'))} {platoon_summary_badge(g.get('away_platoon'))}</div>
              <table class="batter-table">
                <thead><tr><th>Name</th><th>AVG</th><th>OBP</th><th>OPS</th><th>L/R</th><th>Last 7</th><th>HR</th><th>vs SP</th></tr></thead>
                <tbody>{batter_rows(g['away_top_order'])}</tbody>
              </table>
            </div>
          </div>
        </details>
      </div>
    </div>"""

    cards_html = "\n".join(game_card(g) for g in games)

    # ---------------- Bet-selection JavaScript (plain string, no f-string escaping) ----------------
    bet_js = r"""
(function() {
  const ANALYSIS_DATE = window.NRFI_DASHBOARD_DATE || "unknown";
  const LS_UNIT_SIZE = "nrfi_unit_size_dollars";
  const LS_SELECTIONS = "nrfi_selections:" + ANALYSIS_DATE;

  function parseNum(v, fallback) {
    const n = Number(v);
    return (v === "" || v === null || v === undefined || !isFinite(n)) ? fallback : n;
  }

  function loadUnitSize() {
    const v = localStorage.getItem(LS_UNIT_SIZE);
    return v !== null ? parseNum(v, 50) : 50;
  }
  function saveUnitSize(v) { localStorage.setItem(LS_UNIT_SIZE, String(v)); }

  function loadSelections() {
    try {
      const raw = localStorage.getItem(LS_SELECTIONS);
      return raw ? JSON.parse(raw) : {};
    } catch (e) { return {}; }
  }
  function saveSelections(s) {
    localStorage.setItem(LS_SELECTIONS, JSON.stringify(s));
  }

  function hydrate() {
    const unitSize = loadUnitSize();
    const sizeInput = document.getElementById("unit-size-input");
    if (sizeInput) sizeInput.value = unitSize;

    const saved = loadSelections();
    document.querySelectorAll(".bet-row").forEach(row => {
      const key = row.dataset.betKey;
      const s = saved[key];
      if (!s) return;
      const checkEl = row.querySelector('[data-role="check"]');
      const unitsEl = row.querySelector('[data-role="units"]');
      const oddsEl = row.querySelector('[data-role="odds"]');
      const lineEl = row.querySelector('[data-role="line"]');
      if (checkEl && typeof s.checked === "boolean") checkEl.checked = s.checked;
      if (unitsEl && s.units !== undefined && s.units !== null) unitsEl.value = s.units;
      if (oddsEl && s.odds !== undefined && s.odds !== null) oddsEl.value = s.odds;
      if (lineEl && s.line !== undefined && s.line !== null) lineEl.value = s.line;
    });
  }

  function persist() {
    const state = {};
    document.querySelectorAll(".bet-row").forEach(row => {
      const key = row.dataset.betKey;
      const checkEl = row.querySelector('[data-role="check"]');
      const unitsEl = row.querySelector('[data-role="units"]');
      const oddsEl = row.querySelector('[data-role="odds"]');
      const lineEl = row.querySelector('[data-role="line"]');
      const checked = checkEl ? checkEl.checked : false;
      const entry = {
        checked: checked,
        units: unitsEl ? parseNum(unitsEl.value, 0) : 0,
        odds: oddsEl ? parseNum(oddsEl.value, -110) : -110
      };
      if (lineEl) entry.line = parseNum(lineEl.value, null);
      state[key] = entry;
    });
    saveSelections(state);
  }

  function recompute() {
    const sizeInput = document.getElementById("unit-size-input");
    const unitSize = parseNum(sizeInput ? sizeInput.value : 50, 50);
    saveUnitSize(unitSize);

    let nBets = 0, totalUnits = 0, totalDollars = 0;
    document.querySelectorAll(".bet-row").forEach(row => {
      const checkEl = row.querySelector('[data-role="check"]');
      const unitsEl = row.querySelector('[data-role="units"]');
      const dollarsEl = row.querySelector('[data-role="dollars"]');
      const checked = checkEl ? checkEl.checked : false;
      const units = unitsEl ? parseNum(unitsEl.value, 0) : 0;
      const dollars = units * unitSize;
      if (dollarsEl) dollarsEl.textContent = "$" + dollars.toFixed(2);
      if (checked) {
        nBets++;
        totalUnits += units;
        totalDollars += dollars;
        row.classList.add("bet-row-selected");
      } else {
        row.classList.remove("bet-row-selected");
      }
    });

    const pill = document.getElementById("bet-count");
    if (pill) {
      pill.textContent =
        nBets + " bet" + (nBets === 1 ? "" : "s") + " selected · " +
        totalUnits.toFixed(1) + "u · $" + totalDollars.toFixed(2);
    }
    persist();
  }

  function exportBets() {
    const sizeInput = document.getElementById("unit-size-input");
    const unitSize = parseNum(sizeInput ? sizeInput.value : 50, 50);

    const bets = [];
    document.querySelectorAll(".bet-row").forEach(row => {
      const checkEl = row.querySelector('[data-role="check"]');
      if (!checkEl || !checkEl.checked) return;
      const d = row.dataset;
      const unitsEl = row.querySelector('[data-role="units"]');
      const oddsEl = row.querySelector('[data-role="odds"]');
      const lineEl = row.querySelector('[data-role="line"]');
      const units = unitsEl ? parseNum(unitsEl.value, 0) : 0;
      const odds = oddsEl ? parseNum(oddsEl.value, -110) : -110;
      const line = lineEl ? parseNum(lineEl.value, null) : null;

      let pick = d.pick;
      if (d.market === "F5_TOTAL" && line !== null) {
        pick = d.lean + " " + line;
      }

      bets.push({
        game_pk: parseInt(d.gamePk, 10),
        matchup: d.matchup || "",
        market: d.market,
        pick: pick,
        line: line,
        units: units,
        odds: odds,
        model_confidence: d.modelConfidence || null,
        model_units: d.modelUnits ? parseFloat(d.modelUnits) : null,
        model_score_or_edge: d.modelScore ? parseFloat(d.modelScore) : null
      });
    });

    if (bets.length === 0) {
      alert("No bets selected. Check at least one bet before exporting.");
      return;
    }

    const payload = {
      date: ANALYSIS_DATE,
      unit_size_dollars: unitSize,
      exported_at: new Date().toISOString(),
      bets: bets
    };

    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bets_" + ANALYSIS_DATE + ".json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function clearBets() {
    if (!confirm("Clear all bet selections for " + ANALYSIS_DATE + "?")) return;
    localStorage.removeItem(LS_SELECTIONS);
    document.querySelectorAll(".bet-row").forEach(row => {
      const checkEl = row.querySelector('[data-role="check"]');
      if (checkEl) checkEl.checked = false;
    });
    recompute();
  }

  document.addEventListener("DOMContentLoaded", () => {
    hydrate();
    const sizeInput = document.getElementById("unit-size-input");
    const exportBtn = document.getElementById("export-bets-btn");
    const clearBtn = document.getElementById("clear-bets-btn");
    if (sizeInput) sizeInput.addEventListener("input", recompute);
    if (exportBtn) exportBtn.addEventListener("click", exportBets);
    if (clearBtn) clearBtn.addEventListener("click", clearBets);

    // Prevent clicking inside inputs from toggling the parent label's checkbox
    document.querySelectorAll(".bet-row input:not(.bet-check), .bet-row select").forEach(el => {
      el.addEventListener("click", (e) => e.stopPropagation());
    });

    document.querySelectorAll(".bet-row").forEach(row => {
      row.addEventListener("change", recompute);
      row.addEventListener("input", recompute);
    });

    recompute();
  });
})();
"""

    # ---------------- HTML Shell ----------------
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F5 + NRFI Dashboard — {analysis_date}</title>
<style>
  :root {{
    --bg: #0f172a;
    --surface: #1e293b;
    --surface2: #334155;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --accent: #38bdf8;
    --f5-accent: #4ade80;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    padding: 20px;
    max-width: 1280px;
    margin: 0 auto;
  }}
  .header {{
    text-align: center;
    margin-bottom: 24px;
    padding: 20px;
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--surface2);
  }}
  .header h1 {{
    font-size: 1.9em;
    margin-bottom: 6px;
    background: linear-gradient(90deg, #4ade80, #38bdf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }}
  .header .subtitle {{ color: var(--text-dim); font-size: 0.9em; }}
  .header-meta {{
    margin-top: 10px;
    display: flex;
    justify-content: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .meta-pill {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    border: 1px solid var(--surface2);
    background: var(--bg);
    color: var(--text);
    font-size: 0.78em;
    letter-spacing: 0.2px;
  }}
  .meta-pill strong {{ color: var(--text-dim); font-weight: 600; }}

  /* ---- Bet toolbar (sticky top) ---- */
  .bet-toolbar {{
    position: sticky;
    top: 8px;
    z-index: 50;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    background: var(--surface);
    border: 1px solid var(--surface2);
    border-radius: 10px;
    padding: 10px 14px;
    margin-bottom: 18px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.35);
  }}
  .bet-toolbar-label {{
    font-size: 0.78em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }}
  .bet-toolbar input[type="number"] {{
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--surface2);
    border-radius: 6px;
    padding: 5px 8px;
    font-size: 0.95em;
    width: 72px;
    font-weight: 700;
  }}
  .bet-count-pill {{
    flex: 1;
    min-width: 220px;
    font-size: 0.88em;
    color: var(--text);
    font-weight: 600;
  }}
  .btn-primary, .btn-ghost {{
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 0.82em;
    font-weight: 700;
    cursor: pointer;
    border: none;
    transition: all 0.12s;
  }}
  .btn-primary {{ background: var(--f5-accent); color: #000; }}
  .btn-primary:hover {{ opacity: 0.85; transform: translateY(-1px); }}
  .btn-ghost {{
    background: transparent;
    color: var(--text-dim);
    border: 1px solid var(--surface2);
  }}
  .btn-ghost:hover {{ background: var(--surface2); color: var(--text); }}

  /* ---- Bet selection panel (inside each game card) ---- */
  .bet-panel {{
    background: rgba(56,189,248,0.04);
    border: 1px dashed var(--surface2);
    border-radius: 8px;
    padding: 10px 12px;
    margin-bottom: 14px;
  }}
  .bet-panel-label {{
    font-size: 0.7em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-weight: 700;
    margin-bottom: 6px;
  }}
  .bet-row {{
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 8px;
    border-radius: 6px;
    cursor: pointer;
    flex-wrap: wrap;
    font-size: 0.82em;
    border-left: 3px solid transparent;
    transition: background 0.12s, border-color 0.12s;
  }}
  .bet-row:hover {{ background: rgba(255,255,255,0.03); }}
  .bet-row-selected {{
    background: rgba(74,222,128,0.08);
    border-left-color: var(--f5-accent);
  }}
  .bet-check {{ margin: 0; width: 16px; height: 16px; cursor: pointer; accent-color: var(--f5-accent); }}
  .bet-label {{
    font-weight: 600;
    color: var(--text);
    min-width: 150px;
  }}
  .bet-input-group {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }}
  .bet-input-lbl {{
    font-size: 0.72em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .bet-units, .bet-odds, .bet-line {{
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--surface2);
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 0.88em;
    width: 64px;
  }}
  .bet-line {{ width: 70px; }}
  .bet-dollars {{
    margin-left: auto;
    font-weight: 700;
    color: var(--f5-accent);
    font-size: 0.92em;
    min-width: 62px;
    text-align: right;
  }}

  /* ---- Top summary (F5 + YRFI primary, NRFI secondary) ---- */
  .summary-section {{ margin-bottom: 20px; }}
  .summary-row {{
    display: flex;
    gap: 10px;
    justify-content: center;
    margin-bottom: 10px;
    flex-wrap: wrap;
    align-items: center;
  }}
  .summary-row-label {{
    color: var(--text-dim);
    font-size: 0.75em;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-right: 4px;
    font-weight: 700;
  }}
  .summary-chip {{
    padding: 6px 14px;
    border-radius: 18px;
    font-weight: 700;
    font-size: 0.82em;
    cursor: pointer;
    border: 2px solid transparent;
    transition: all 0.15s;
  }}
  .summary-chip.secondary {{
    padding: 5px 11px;
    font-size: 0.75em;
    font-weight: 600;
  }}
  .summary-chip:hover {{ opacity: 0.85; transform: translateY(-1px); }}

  .filter-row {{
    display: flex;
    gap: 8px;
    justify-content: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .filter-btn {{
    background: var(--surface);
    border: 1px solid var(--surface2);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.82em;
    transition: all 0.15s;
  }}
  .filter-btn:hover {{ background: var(--surface2); }}
  .filter-btn.active {{ background: var(--f5-accent); color: #000; font-weight: 700; }}

  /* ---- Game card ---- */
  .game-card {{
    background: var(--surface);
    border-radius: 12px;
    margin-bottom: 18px;
    border-left: 5px solid;
    overflow: hidden;
    transition: transform 0.1s;
  }}
  .game-card:hover {{ transform: translateX(2px); }}

  .game-header {{
    padding: 14px 18px;
    background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent);
    border-bottom: 1px solid var(--surface2);
  }}
  .matchup-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .matchup {{ font-size: 1.25em; font-weight: 700; }}
  .header-badges {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .headline-badge {{
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 0.82em;
    font-weight: 700;
    white-space: nowrap;
    letter-spacing: 0.02em;
  }}
  .f5-badge {{ font-size: 0.85em; }}
  .yrfi-badge {{
    box-shadow: 0 0 0 2px rgba(239,68,68,0.15);
    animation: yrfi-pulse 2.5s ease-in-out infinite;
  }}
  @keyframes yrfi-pulse {{
    0%, 100% {{ box-shadow: 0 0 0 2px rgba(239,68,68,0.15); }}
    50% {{ box-shadow: 0 0 0 4px rgba(239,68,68,0.35); }}
  }}
  .nrfi-mini {{
    background: transparent;
    font-size: 0.75em;
    padding: 3px 9px;
    font-weight: 600;
  }}

  .odds-badge {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 0.78em;
    background: var(--surface2);
    white-space: nowrap;
  }}
  .odds-book {{ color: var(--text-dim); font-size: 0.85em; }}
  .odds-price {{ font-weight: 700; color: var(--text); }}
  .odds-prob {{ color: var(--text-dim); font-size: 0.9em; }}
  .odds-value {{ font-weight: 700; font-size: 0.85em; margin-left: 2px; }}

  .game-meta {{ color: var(--text-dim); font-size: 0.82em; margin-top: 6px; }}
  .game-body {{ padding: 16px 18px; }}

  /* ---- F5 HEADLINE PANEL (the new star of the show) ---- */
  .f5-headline {{
    background: linear-gradient(135deg, rgba(74,222,128,0.06), rgba(56,189,248,0.06));
    border: 1px solid var(--surface2);
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 16px;
  }}
  .f5-headline-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
  }}
  .f5-cell {{
    background: var(--bg);
    border-radius: 8px;
    padding: 12px 10px;
    text-align: center;
    border: 1px solid var(--surface2);
  }}
  .f5-cell-label {{
    font-size: 0.7em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 6px;
    font-weight: 700;
  }}
  .f5-cell-val {{
    font-size: 1.6em;
    font-weight: 800;
    line-height: 1.1;
  }}
  .f5-cell-sub {{
    font-size: 0.78em;
    color: var(--text);
    margin-top: 6px;
  }}
  .f5-cell-sub strong {{ color: var(--text); }}
  .f5-cell-extra {{
    font-size: 0.7em;
    color: var(--text-dim);
    margin-top: 3px;
  }}
  .f5-lines-row {{
    display: flex;
    gap: 6px;
    margin-top: 12px;
    justify-content: center;
    flex-wrap: wrap;
    padding-top: 10px;
    border-top: 1px dashed var(--surface2);
  }}
  .f5-lines-label {{
    font-size: 0.72em;
    color: var(--text-dim);
    align-self: center;
    margin-right: 4px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .f5-line-pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 3px 9px;
    border-radius: 5px;
    font-size: 0.74em;
    background: var(--surface);
    border: 1px solid var(--surface2);
  }}
  .f5-line-num {{ color: var(--text-dim); font-weight: 600; }}
  .f5-units-pill {{
    display: inline-block;
    margin-left: 6px;
    padding: 1px 7px;
    border-radius: 999px;
    font-size: 0.85em;
    font-weight: 800;
    letter-spacing: 0.02em;
    vertical-align: baseline;
  }}

  /* ---- Pitchers (compressed, secondary) ---- */
  .pitchers-row {{
    display: flex;
    gap: 14px;
    align-items: stretch;
    margin-bottom: 14px;
  }}
  .pitcher-section {{ flex: 1; }}
  .vs-divider {{
    display: flex;
    align-items: center;
    color: var(--text-dim);
    font-weight: 700;
    font-size: 0.9em;
  }}
  .section-label {{
    font-size: 0.7em;
    text-transform: uppercase;
    color: var(--text-dim);
    letter-spacing: 0.5px;
    margin-bottom: 6px;
  }}
  .pitcher-card {{
    background: var(--bg);
    padding: 12px;
    border-radius: 8px;
  }}
  .pitcher-name {{ font-weight: 600; font-size: 1em; margin-bottom: 4px; }}
  .pitcher-score {{ font-size: 1.5em; font-weight: 800; margin-bottom: 6px; }}
  .stat-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; }}
  .stat {{ text-align: center; }}
  .stat-label {{ display: block; font-size: 0.65em; color: var(--text-dim); text-transform: uppercase; }}
  .stat-value {{ display: block; font-size: 0.9em; font-weight: 600; }}

  /* ---- NRFI compact (collapsible, secondary) ---- */
  .nrfi-secondary {{
    margin-top: 4px;
    border: 1px solid var(--surface2);
    border-radius: 8px;
    background: rgba(0,0,0,0.18);
  }}
  .nrfi-secondary summary {{
    cursor: pointer;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    gap: 10px;
    list-style: none;
    flex-wrap: wrap;
  }}
  .nrfi-secondary summary::-webkit-details-marker {{ display: none; }}
  .nrfi-secondary summary::before {{
    content: "▸";
    color: var(--text-dim);
    font-size: 0.85em;
    transition: transform 0.15s;
  }}
  .nrfi-secondary[open] summary::before {{ transform: rotate(90deg); display: inline-block; }}
  .nrfi-summary-label {{
    font-size: 0.85em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-weight: 700;
  }}
  .nrfi-score-pill {{
    padding: 3px 10px;
    border-radius: 5px;
    font-weight: 700;
    font-size: 0.85em;
  }}
  .yrfi-inline {{
    padding: 3px 10px;
    border-radius: 5px;
    font-weight: 700;
    font-size: 0.78em;
  }}
  .nrfi-body {{ padding: 0 14px 12px 14px; }}

  /* ---- Lineups ---- */
  .lineup-details {{ margin-top: 12px; }}
  .lineup-details summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.85em;
    padding: 6px 0;
  }}
  .lineups-row {{ display: flex; gap: 16px; margin-top: 8px; }}
  .lineup-section {{ flex: 1; }}
  .batter-table {{ width: 100%; font-size: 0.78em; border-collapse: collapse; }}
  .batter-table th {{
    text-align: left;
    color: var(--text-dim);
    padding: 4px 6px;
    border-bottom: 1px solid var(--surface2);
    font-weight: 600;
  }}
  .batter-table td {{
    padding: 3px 6px;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }}

  .lineup-src {{
    font-size: 0.72em;
    padding: 2px 8px;
    border-radius: 4px;
    margin-left: 8px;
    vertical-align: middle;
  }}
  .lineup-confirmed {{ background: rgba(34,197,94,0.15); color: #4ade80; }}
  .lineup-partial {{ background: rgba(234,179,8,0.15); color: #eab308; }}
  .lineup-estimated {{ background: rgba(239,68,68,0.15); color: #f87171; }}

  .badge-row {{ display: flex; gap: 6px; flex-wrap: wrap; margin: 4px 0 6px 0; }}

  .park-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.72em;
    font-weight: 600;
    margin-left: 6px;
    vertical-align: middle;
  }}
  .park-hitter {{ background: rgba(239,68,68,0.2); color: #ef4444; }}
  .park-pitcher {{ background: rgba(34,197,94,0.2); color: #22c55e; }}
  .park-neutral {{ background: rgba(107,114,128,0.15); color: #9ca3af; }}

  .bvp-badge {{
    display: inline-block;
    padding: 3px 10px;
    border-radius: 4px;
    font-size: 0.72em;
    font-weight: 600;
    margin: 4px 0 6px 0;
  }}
  .bvp-danger {{ background: rgba(239,68,68,0.2); color: #ef4444; }}
  .bvp-warn {{ background: rgba(234,179,8,0.2); color: #eab308; }}
  .bvp-neutral {{ background: rgba(107,114,128,0.2); color: #9ca3af; }}
  .bvp-good {{ background: rgba(34,197,94,0.15); color: #4ade80; }}
  .bvp-safe {{ background: rgba(34,197,94,0.25); color: #22c55e; }}

  .hand-badge {{
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 0.7em;
    font-weight: 700;
    margin-left: 6px;
    vertical-align: middle;
  }}
  .hand-left {{ background: rgba(96,165,250,0.2); color: #60a5fa; }}
  .hand-right {{ background: rgba(251,146,60,0.2); color: #fb923c; }}

  .fi-row {{ margin-top: 6px; }}
  .fi-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.68em;
    font-weight: 600;
  }}
  .fi-good {{ background: rgba(34,197,94,0.2); color: #22c55e; }}
  .fi-neutral {{ background: rgba(107,114,128,0.15); color: #9ca3af; }}
  .fi-bad {{ background: rgba(239,68,68,0.2); color: #ef4444; }}

  .score-breakdown {{
    display: flex;
    gap: 14px;
    margin-top: 8px;
    font-size: 0.75em;
    color: var(--text-dim);
    padding: 8px 10px;
    background: rgba(0,0,0,0.18);
    border-radius: 6px;
    flex-wrap: wrap;
  }}

  .tend-panel {{
    margin-top: 12px;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--surface2);
    border-radius: 8px;
  }}
  .tend-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }}
  .tend-title {{
    font-size: 0.78em;
    font-weight: 600;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .tend-adj {{ font-size: 0.85em; font-weight: 700; }}
  .tend-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .tend-cell {{
    background: var(--surface2);
    border-radius: 6px;
    padding: 8px 10px;
  }}
  .tend-label {{
    font-size: 0.7em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    margin-bottom: 2px;
  }}
  .tend-val {{ font-size: 1.05em; font-weight: 700; }}
  .tend-sub {{ font-size: 0.72em; color: var(--text-dim); font-weight: 400; }}
  .tend-foot {{
    margin-top: 6px;
    font-size: 0.7em;
    color: var(--text-dim);
    font-style: italic;
  }}

  .methodology {{
    background: var(--surface);
    border-radius: 12px;
    padding: 18px;
    margin-top: 30px;
    font-size: 0.82em;
    color: var(--text-dim);
    border: 1px solid var(--surface2);
  }}
  .methodology h3 {{ color: var(--text); margin-bottom: 8px; }}

  .no-games {{ text-align: center; padding: 60px 20px; color: var(--text-dim); font-size: 1.1em; }}

  @media (max-width: 800px) {{
    .pitchers-row, .lineups-row {{ flex-direction: column; }}
    .vs-divider {{ justify-content: center; padding: 4px 0; }}
    .f5-headline-grid {{ grid-template-columns: 1fr; }}
    body {{ padding: 10px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>F5 + NRFI Dashboard</h1>
  <div class="subtitle">{analysis_date} · Generated {datetime.now().strftime('%I:%M %p')} · {len(games)} games analyzed · F5 prioritized</div>
  <div class="header-meta">
    <span class="meta-pill"><strong>Stats Through</strong> {stats_through}</span>
  </div>
</div>

<div class="bet-toolbar">
  <label class="bet-toolbar-label">Unit size $
    <input id="unit-size-input" type="number" min="0" step="5" value="50">
  </label>
  <span id="bet-count" class="bet-count-pill">0 bets selected · 0.0u · $0.00</span>
  <button id="export-bets-btn" class="btn-primary">Export Bets</button>
  <button id="clear-bets-btn" class="btn-ghost">Clear</button>
</div>

<div class="summary-section">
  <div class="summary-row">
    <span class="summary-row-label">F5 Conviction</span>
    <div class="summary-chip" style="background:#22c55e;color:#000" onclick="filterF5('lock')">F5 LOCK · {f5_lock_count}</div>
    <div class="summary-chip" style="background:#4ade80;color:#000" onclick="filterF5('strong')">F5 STRONG · {f5_strong_count}</div>
    <div class="summary-chip" style="background:#eab308;color:#000" onclick="filterF5('lean')">F5 LEAN · {f5_lean_count}</div>
  </div>
  <div class="summary-row">
    <span class="summary-row-label">YRFI Watch</span>
    <div class="summary-chip" style="background:#ef4444;color:#000" onclick="filterYRFI('strong')">⚡ YRFI STRONG · {yrfi_strong_count}</div>
    <div class="summary-chip" style="background:#fb923c;color:#000" onclick="filterYRFI('lean')">⚡ YRFI LEAN · {yrfi_lean_count}</div>
  </div>
  <div class="summary-row">
    <span class="summary-row-label">NRFI (secondary)</span>
    <div class="summary-chip secondary" style="background:#22c55e;color:#000" onclick="filterNRFI('strong')">NRFI STRONG · {len(nrfi_strong)}</div>
    <div class="summary-chip secondary" style="background:#eab308;color:#000" onclick="filterNRFI('lean')">NRFI LEAN · {len(nrfi_lean)}</div>
  </div>
</div>

<div class="filter-row">
  <button class="filter-btn active" onclick="resetFilter(this)">All Games</button>
  <button class="filter-btn" onclick="filterF5Picks(this)">F5 Picks Only</button>
  <button class="filter-btn" onclick="filterYRFIAny(this)">YRFI Watch</button>
  <button class="filter-btn" onclick="filterNRFIPicks(this)">NRFI Picks</button>
  <button class="filter-btn" onclick="filterBullpen(this)" style="color:#ff6b35">⚠ Bullpen</button>
</div>

<div id="games-container">
  {'<div class="no-games">No games found for this date.</div>' if not games else cards_html}
</div>

<div class="methodology">
  <h3>F5 (First 5 Innings) — Primary Models</h3>
  <p>F5 is now the headline because over 5 innings the starting pitcher's quality dominates and these signals have shown the best historical hit rate. Each game shows three F5 calls plus a conviction tier (LOCK / STRONG / LEAN / SLIGHT / PASS) summarizing how many strong signals align.</p>
  <p style="margin-top:6px"><strong>F5 Moneyline:</strong> Power rating per side = 70% pitcher score + 30% inverted lineup threat + adjustments (BvP, platoon, streaks, rest, pitch-count efficiency). High pitches-per-inning adds an early-exit penalty. Differential between sides → pick + edge.</p>
  <p style="margin-top:4px"><strong>F5 Spread:</strong> Derived from the ML edge. Edge &gt;1 supports -0.5; edge &gt;5 supports -1.5.</p>
  <p style="margin-top:4px"><strong>F5 Total:</strong> Per-side run projection × pitcher quality vs lineup, summed with park/weather adjustments. Compared to lines 4.0 / 4.5 / 5.0 / 5.5. ★ = STRONG, • = LEAN.</p>

  <h3 style="margin-top:18px">⚡ YRFI Watch</h3>
  <p>Calibrated against logged outcomes: when the NRFI score is below {YRFI_STRONG_MAX} the historical YRFI hit rate is ~78% (n=27); scores from {YRFI_STRONG_MAX} to {YRFI_LEAN_MAX-1} hit ~100% in a small sample (n=5). Above {YRFI_LEAN_MAX}, the edge over the ~46% league YRFI baseline disappears, so no flag is shown. STRONG = play with confidence; LEAN = treat as a soft tip and re-check after lineups post. These map naturally to 1st-inning over 0.5 / YRFI prop bets. Re-calibrate thresholds as the log grows.</p>

  <h3 style="margin-top:18px">⚠ Bullpen Games</h3>
  <p>When a listed "starter" is actually a reliever (0-2 GS with 5+ relief appearances this season), the game is flagged as a bullpen game. NRFI scores receive a -6 penalty per bullpen side. F5 predictions are penalized harder: the bullpen side's power rating drops by 10 points, run projections bump +0.5 per bullpen side, and all F5 confidence tiers are capped (never STRONG/MODERATE). These games have elevated variance — treat with extra caution.</p>

  <h3 style="margin-top:18px">NRFI (Secondary) — Scoring Inputs</h3>
  <p>NRFI score (0-100) = composite of pitching (45%, weakest-link weighted), lineup threat (25%, most-dangerous weighted), and adjustments. Higher = more likely no run scores in the 1st. Tiers: STRONG ≥72, LEAN 62-71.9, TOSS-UP 50-61.9, FADE &lt;50.</p>
  <p style="margin-top:4px"><strong>Adjustments:</strong> First-inning ERA & clean%, batter vs pitcher, L/R platoon, recent form, park factor, weather, team 1st-inning tendency, pitcher rest.</p>

  <p style="margin-top:14px;color:#ef4444"><strong>Disclaimer:</strong> This is an analytical tool, not financial advice. All sports betting carries risk. Sample sizes are still modest — use as one input, not as the whole story.</p>
</div>

<script>
function _activeBtn(el) {{
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  if (el) el.classList.add('active');
}}

function resetFilter(el) {{
  document.querySelectorAll('.game-card').forEach(c => c.style.display = 'block');
  _activeBtn(el);
}}

function filterF5Picks(el) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    const v = c.dataset.f5;
    c.style.display = (v === 'lock' || v === 'strong' || v === 'lean') ? 'block' : 'none';
  }});
  _activeBtn(el);
}}

function filterYRFIAny(el) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    c.style.display = (c.dataset.yrfi !== 'none') ? 'block' : 'none';
  }});
  _activeBtn(el);
}}

function filterNRFIPicks(el) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    const v = c.dataset.nrfi;
    c.style.display = (v === 'strong' || v === 'lean') ? 'block' : 'none';
  }});
  _activeBtn(el);
}}

function filterBullpen(el) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    c.style.display = (c.dataset.bullpen === 'yes') ? 'block' : 'none';
  }});
  _activeBtn(el);
}}

// Chip-driven filters — clear the All button highlight when used
function filterF5(level) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    c.style.display = (c.dataset.f5 === level) ? 'block' : 'none';
  }});
  _activeBtn(null);
}}

function filterYRFI(level) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    c.style.display = (c.dataset.yrfi === level) ? 'block' : 'none';
  }});
  _activeBtn(null);
}}

function filterNRFI(level) {{
  document.querySelectorAll('.game-card').forEach(c => {{
    c.style.display = (c.dataset.nrfi === level) ? 'block' : 'none';
  }});
  _activeBtn(null);
}}
</script>

<script>window.NRFI_DASHBOARD_DATE = "{analysis_date}";</script>
<script>
{bet_js}
</script>

</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)
    print(f"Dashboard saved: {output_path}")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python dashboard.py <path_to_nrfi_json>")
        sys.exit(1)

    json_path = sys.argv[1]
    with open(json_path) as f:
        data = json.load(f)

    out_dir = os.path.dirname(json_path)
    date_str = data.get("date", "unknown")
    out_path = os.path.join(out_dir, f"nrfi_dashboard_{date_str}.html")
    generate_dashboard(data, out_path)
