#!/usr/bin/env python3
"""
NRFI Dashboard Generator
=========================
Takes the JSON output from nrfi_analyzer.py and produces an interactive
single-file HTML dashboard.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta

# Mirror of analyzer constant — used for tendency display only.
LEAGUE_FI_SCORE_RATE = 0.27


def fmt(val, precision=2, fallback="—"):
    """Format a numeric value, or return fallback."""
    if val is None:
        return fallback
    try:
        return f"{float(val):.{precision}f}"
    except (TypeError, ValueError):
        return fallback


def generate_dashboard(data: dict, output_path: str):
    games = data.get("games", [])
    analysis_date = data.get("date", "Unknown")
    generated = data.get("generated", "")
    stats_through = data.get("stats_through")
    if not stats_through:
        try:
            stats_through = (datetime.fromisoformat(analysis_date) - timedelta(days=1)).date().isoformat()
        except Exception:
            stats_through = "Unknown"

    # Separate top picks
    strong = [g for g in games if g["nrfi"]["tier"] == "STRONG"]
    lean = [g for g in games if g["nrfi"]["tier"] == "LEAN"]
    tossup = [g for g in games if g["nrfi"]["tier"] == "TOSS-UP"]
    fade = [g for g in games if g["nrfi"]["tier"] == "FADE"]

    def tier_color(tier):
        return {"STRONG": "#22c55e", "LEAN": "#eab308", "TOSS-UP": "#f97316", "FADE": "#ef4444"}.get(tier, "#6b7280")

    def tier_bg(tier):
        return {"STRONG": "#052e16", "LEAN": "#422006", "TOSS-UP": "#431407", "FADE": "#450a0a"}.get(tier, "#1f2937")

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
        """Render first-inning stats as a compact badge on pitcher card."""
        if not fi_stats or not fi_stats.get("has_data"):
            return '<span class="fi-badge fi-neutral">1st Inn: No data</span>'
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
        return f'<span class="fi-badge {cls}" title="{clean}/{starts} clean 1st innings">1st Inn ERA: {era_str} · {pct_str} clean ({starts}GS)</span>'

    def hand_badge(hand):
        """Render pitcher handedness as a small indicator."""
        if hand == "L":
            return '<span class="hand-badge hand-left" title="Left-handed pitcher">LHP</span>'
        elif hand == "R":
            return '<span class="hand-badge hand-right" title="Right-handed pitcher">RHP</span>'
        return ''

    def pitcher_card(name, stats, score, hand="?", fi_stats=None):
        era = fmt(stats.get("era"))
        whip = fmt(stats.get("whip"))
        k9 = fmt(stats.get("k9"), 1)
        bb9 = fmt(stats.get("bb9"), 1)
        hr9 = fmt(stats.get("hr9"), 1)
        ip = fmt(stats.get("ip"), 1)
        return f"""
        <div class="pitcher-card">
          <div class="pitcher-name">{name} {hand_badge(hand)}</div>
          <div class="pitcher-score" style="color: {'#22c55e' if score >= 65 else '#eab308' if score >= 50 else '#ef4444'}">
            {score}
          </div>
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
        """Render BvP stats as a compact cell with color coding."""
        if not bvp or not bvp.get("has_data") or bvp.get("ab", 0) == 0:
            return '<span style="color:#6b7280">—</span>'
        ab = bvp["ab"]
        h = bvp["hits"]
        hr = bvp["hr"]
        avg = bvp.get("avg", "—")
        ops = bvp.get("ops", "—")
        try:
            ops_f = float(ops)
            color = "#ef4444" if ops_f >= .800 else "#eab308" if ops_f >= .650 else "#22c55e"
        except (TypeError, ValueError):
            color = "#6b7280"
        return f'<span style="color:{color}" title="{h}-for-{ab}, {hr} HR">{avg}/{ops} ({ab}AB)</span>'

    def bvp_summary_badge(bvp_sum):
        """Render the BvP summary as a compact badge."""
        if not bvp_sum or not bvp_sum.get("has_meaningful_data"):
            ab = bvp_sum.get("total_ab", 0) if bvp_sum else 0
            if ab > 0:
                return f'<span class="bvp-badge bvp-neutral">BvP: {ab}AB (limited)</span>'
            return '<span class="bvp-badge bvp-neutral">BvP: No history</span>'
        w_ops = bvp_sum.get("weighted_ops")
        ab = bvp_sum["total_ab"]
        hr = bvp_sum["total_hr"]
        if w_ops is not None:
            if w_ops >= .800:
                cls = "bvp-danger"
                label = "Batters own SP"
            elif w_ops >= .700:
                cls = "bvp-warn"
                label = "Slight batter edge"
            elif w_ops <= .550:
                cls = "bvp-safe"
                label = "SP dominates"
            elif w_ops <= .650:
                cls = "bvp-good"
                label = "SP has edge"
            else:
                cls = "bvp-neutral"
                label = "Even matchup"
            return f'<span class="bvp-badge {cls}">BvP: .{str(w_ops)[2:5]} OPS · {ab}AB · {hr}HR — {label}</span>'
        return f'<span class="bvp-badge bvp-neutral">BvP: {ab}AB</span>'

    def streak_cell(recent):
        """Render recent form as a compact cell with hot/cold indicator."""
        if not recent or not recent.get("has_data"):
            return '<span style="color:#6b7280">—</span>'
        status = recent.get("streak_status", "unknown")
        ops = recent.get("ops", "—")
        delta = recent.get("ops_delta")
        avg = recent.get("avg", "—")
        games = recent.get("games", 7)

        icons = {"hot": "🔥", "warm": "📈", "neutral": "➖", "cool": "📉", "cold": "🧊"}
        colors = {"hot": "#ef4444", "warm": "#fb923c", "neutral": "#9ca3af", "cool": "#60a5fa", "cold": "#38bdf8"}
        icon = icons.get(status, "")
        color = colors.get(status, "#6b7280")

        delta_str = ""
        if delta is not None:
            delta_str = f" ({'+' if delta >= 0 else ''}{delta:.3f})"

        return f'<span style="color:{color}" title="Last {games}G: {avg} AVG / {ops} OPS{delta_str}">{icon} {avg}/{ops}</span>'

    def platoon_summary_badge(platoon_sum):
        """Render lineup-level platoon split badge."""
        if not platoon_sum or not platoon_sum.get("has_data"):
            return '<span class="bvp-badge bvp-neutral">L/R: No data</span>'
        vs_hand = platoon_sum.get("vs_hand", "?")
        w_ops = platoon_sum.get("weighted_ops")
        adv = platoon_sum.get("advantage_count", 0)
        disadv = platoon_sum.get("disadvantage_count", 0)
        total = platoon_sum.get("batters_with_data", 0)

        if w_ops is None:
            return f'<span class="bvp-badge bvp-neutral">vs {vs_hand}HP: No splits</span>'

        if w_ops >= .800:
            cls = "bvp-danger"
            label = "Lineup rakes"
        elif w_ops >= .700:
            cls = "bvp-warn"
            label = "Lineup hits well"
        elif w_ops <= .580:
            cls = "bvp-safe"
            label = "Lineup struggles"
        elif w_ops <= .650:
            cls = "bvp-good"
            label = "SP has edge"
        else:
            cls = "bvp-neutral"
            label = "Neutral"

        ops_str = f".{str(w_ops)[2:5]}"
        comp_str = f"{disadv}dis/{adv}adv" if (adv + disadv) > 0 else ""
        return f'<span class="bvp-badge {cls}">vs {vs_hand}HP: {ops_str} OPS · {comp_str} — {label}</span>'

    def platoon_cell(platoon_data):
        """Render platoon split for a single batter."""
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
        """Render lineup-level streak badge."""
        if not streak_sum or not streak_sum.get("has_data"):
            return '<span class="bvp-badge bvp-neutral">Form: No data</span>'
        avg_delta = streak_sum.get("avg_ops_delta")
        hot = streak_sum.get("hot_count", 0)
        cold = streak_sum.get("cold_count", 0)
        recent_ops = streak_sum.get("avg_recent_ops")

        if avg_delta is None:
            return '<span class="bvp-badge bvp-neutral">Form: N/A</span>'

        if avg_delta <= -.150:
            cls = "bvp-safe"
            label = "Ice cold"
        elif avg_delta <= -.050:
            cls = "bvp-good"
            label = "Cooling off"
        elif avg_delta >= .150:
            cls = "bvp-danger"
            label = "On fire"
        elif avg_delta >= .050:
            cls = "bvp-warn"
            label = "Heating up"
        else:
            cls = "bvp-neutral"
            label = "Steady"

        ops_str = f".{str(recent_ops)[2:5]}" if recent_ops else "?"
        delta_str = f"{'+' if avg_delta >= 0 else ''}{avg_delta:.3f}"
        return f'<span class="bvp-badge {cls}">L7: {ops_str} OPS ({delta_str}) · {hot}🔥 {cold}🧊 — {label}</span>'

    def batter_rows(top_order):
        batters = top_order.get("batters", [])
        if not batters:
            return '<tr><td colspan="9" style="text-align:center;color:#6b7280;">No data</td></tr>'
        rows = ""
        for b in batters:
            bvp_data = b.get("bvp", {})
            recent_data = b.get("recent", {})
            platoon_data = b.get("platoon", {})
            rows += f"""<tr>
              <td>{b['name']}</td>
              <td>{b['avg']}</td>
              <td>{b['obp']}</td>
              <td>{b['ops']}</td>
              <td>{platoon_cell(platoon_data)}</td>
              <td>{streak_cell(recent_data)}</td>
              <td>{b['hr']}</td>
              <td>{bvp_cell(bvp_data)}</td>
            </tr>"""
        return rows

    def team_tendency_panel(g):
        """Render a compact panel showing each team's recent 1st-inning scoring rate."""
        ht = g.get("home_team_tendency") or {}
        at = g.get("away_team_tendency") or {}
        meta = g.get("team_tendency_meta") or {}
        if not ht.get("has_data") and not at.get("has_data"):
            return ""

        def cell(label, t, side):
            if not t.get("has_data"):
                return f'<div class="tend-cell"><div class="tend-label">{label}</div><div class="tend-val">—</div></div>'
            scored = t.get(f"{side}_scored", 0)
            n = t.get(f"{side}_games", 0)
            rate = t.get(f"{side}_score_rate")
            overall = t.get("overall_score_rate", 0)
            ttl = t.get("games", 0)
            rate_str = f"{rate:.0%}" if rate is not None and n > 0 else "—"
            # Color code: green = quiet (low rate, NRFI-friendly), red = loud
            if rate is None or n == 0:
                color = "#888"
            elif rate <= 0.20:
                color = "#1fb86e"
            elif rate <= 0.27:
                color = "#9bd15a"
            elif rate <= 0.33:
                color = "#ffb84d"
            else:
                color = "#ff5e5e"
            return (
                f'<div class="tend-cell">'
                f'<div class="tend-label">{label}</div>'
                f'<div class="tend-val" style="color:{color}">{rate_str} <span class="tend-sub">({scored}/{n})</span></div>'
                f'<div class="tend-sub">overall {overall:.0%} · {ttl}g</div>'
                f'</div>'
            )

        adj = meta.get("adj", 0)
        adj_color = "#1fb86e" if adj > 0 else ("#ff5e5e" if adj < 0 else "#aaa")
        adj_str = f"{'+' if adj >= 0 else ''}{adj}"
        blended = meta.get("blended_rate")
        blended_str = f"{blended:.0%}" if blended is not None else "—"

        return f"""
        <div class="tend-panel">
          <div class="tend-header">
            <span class="tend-title">Team 1st-Inning Tendencies (last 30g)</span>
            <span class="tend-adj" style="color:{adj_color}">Adj: {adj_str}</span>
          </div>
          <div class="tend-grid">
            {cell(f"{g['home_abbr']} batting at HOME", ht, "home")}
            {cell(f"{g['away_abbr']} batting on ROAD", at, "away")}
          </div>
          <div class="tend-foot">Blended {blended_str} vs league {LEAGUE_FI_SCORE_RATE:.0%} · shrunk toward team-overall + league prior</div>
        </div>
        """

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
        """Convert bookmaker API key to a readable display name."""
        return {
            "fanduel": "FanDuel", "draftkings": "DraftKings",
            "betmgm": "BetMGM", "caesars": "Caesars",
            "pointsbet": "PointsBet", "betrivers": "BetRivers",
            "bovada": "Bovada", "betonlineag": "BetOnline",
            "mybookieag": "MyBookie", "wynnbet": "WynnBET",
            "espnbet": "ESPN BET", "fanatics": "Fanatics",
            "hardrockbet": "Hard Rock", "bet365": "bet365",
        }.get(key, key.replace("_", " ").title())

    def odds_badge(g):
        """Render an odds badge for the game header, or empty string if no odds."""
        odds = g.get("odds", {})
        if not odds.get("has_odds"):
            return ""

        price = odds.get("nrfi_price")
        impl_prob = odds.get("nrfi_implied_prob")
        if price is None:
            return ""

        price_str = f"+{price}" if price > 0 else str(price)
        prob_str = f"{impl_prob:.0%}" if impl_prob else ""

        # Value comparison: model score / 100 is our implied probability.
        # If model prob > market prob, we're seeing value.
        model_prob = g["nrfi"]["score"] / 100.0
        value_diff = model_prob - (impl_prob or 0)

        if value_diff >= 0.08:
            value_label = "VALUE"
            value_color = "#22c55e"
        elif value_diff >= 0.03:
            value_label = "FAIR"
            value_color = "#eab308"
        elif value_diff >= -0.03:
            value_label = "CLOSE"
            value_color = "#94a3b8"
        else:
            value_label = "NO VALUE"
            value_color = "#ef4444"

        book_name = _pretty_book(odds.get("book", ""))

        return (
            f'<span class="odds-badge">'
            f'<span class="odds-book">{book_name}</span> '
            f'<span class="odds-price">{price_str}</span> '
            f'<span class="odds-prob">({prob_str})</span> '
            f'<span class="odds-value" style="color:{value_color}">{value_label}</span>'
            f'</span>'
        )

    def f5_panel(g):
        """Render the F5 (First 5 Innings) predictions panel."""
        f5 = g.get("f5")
        if not f5:
            return ""

        ml = f5.get("ml", {})
        spread = f5.get("spread", {})
        total = f5.get("total", {})

        def conf_color(conf):
            return {"STRONG": "#22c55e", "MODERATE": "#4ade80", "LEAN": "#eab308",
                    "SLIGHT": "#fbbf24", "TOSS-UP": "#94a3b8"}.get(conf, "#6b7280")

        def lean_color(lean):
            return {"OVER": "#ef4444", "UNDER": "#22c55e", "PUSH": "#94a3b8"}.get(lean, "#6b7280")

        # ML cell
        ml_conf = ml.get("confidence", "TOSS-UP")
        ml_pick = ml.get("pick", "—")
        ml_edge = ml.get("edge", 0)
        ml_edge_str = f"{'+' if ml_edge > 0 else ''}{ml_edge}"
        ml_color = conf_color(ml_conf)
        away_eff_adj = ml.get("away_pitch_eff_adj")
        home_eff_adj = ml.get("home_pitch_eff_adj")

        def fmt_signed(v):
            try:
                fv = float(v)
                return f"{fv:+.1f}"
            except (TypeError, ValueError):
                return "n/a"

        eff_sub = ""
        if away_eff_adj is not None or home_eff_adj is not None:
            eff_sub = f"<br/>P/IP adj A {fmt_signed(away_eff_adj)} · H {fmt_signed(home_eff_adj)}"

        # Spread cell
        sp_label = spread.get("recommended_label", "—")
        sp_conf = spread.get("confidence", "TOSS-UP")
        sp_color = conf_color(sp_conf)

        # Total cell
        t_proj = total.get("projected_total", "—")
        t_lean = total.get("lean", "PUSH")
        t_conf = total.get("confidence", "TOSS-UP")
        t_line = total.get("primary_line", 4.5)
        t_lean_color = lean_color(t_lean)
        t_conf_color = conf_color(t_conf)

        # Line calls for all common lines
        line_calls = total.get("line_calls", {})
        line_pills = ""
        for line_val in ["4.0", "4.5", "5.0", "5.5"]:
            call = line_calls.get(line_val, {})
            call_lean = call.get("lean", "PUSH")
            call_conf = call.get("confidence", "TOSS-UP")
            lc = lean_color(call_lean)
            pill_opacity = "1.0" if call_conf in ("STRONG", "LEAN") else "0.6"
            line_pills += (
                f'<span class="f5-line-pill" style="opacity:{pill_opacity}">'
                f'<span class="f5-line-num">{line_val}</span>'
                f'<span style="color:{lc};font-weight:700">{call_lean}</span>'
                f'</span>'
            )

        return f"""
        <div class="f5-panel">
          <div class="f5-header">
            <span class="f5-title">First 5 Innings</span>
          </div>
          <div class="f5-grid">
            <div class="f5-cell">
              <div class="f5-cell-label">MONEYLINE</div>
              <div class="f5-cell-val" style="color:{ml_color}">{ml_pick}</div>
              <div class="f5-cell-sub">Edge: {ml_edge_str} · {ml_conf}{eff_sub}</div>
            </div>
            <div class="f5-cell">
              <div class="f5-cell-label">SPREAD</div>
              <div class="f5-cell-val" style="color:{sp_color}">{sp_label}</div>
              <div class="f5-cell-sub">{sp_conf}</div>
            </div>
            <div class="f5-cell">
              <div class="f5-cell-label">TOTAL (proj {t_proj})</div>
              <div class="f5-cell-val" style="color:{t_lean_color}">{t_lean} {t_line}</div>
              <div class="f5-cell-sub">{t_conf}</div>
            </div>
          </div>
          <div class="f5-lines-row">
            {line_pills}
          </div>
        </div>"""

    def game_card(g):
        nrfi = g["nrfi"]
        tc = tier_color(nrfi["tier"])
        tb = tier_bg(nrfi["tier"])

        # Parse game time — convert UTC to US Eastern
        try:
            gt_utc = datetime.fromisoformat(g["game_time"].replace("Z", "+00:00"))
            # US Eastern: UTC-4 (EDT, covers MLB regular season)
            est = timezone(timedelta(hours=-4))
            gt_et = gt_utc.astimezone(est)
            time_str = gt_et.strftime("%-I:%M %p ET")
        except:
            time_str = "TBD"

        return f"""
    <div class="game-card" style="border-color: {tc}">
      <div class="game-header" style="background: {tb}">
        <div class="matchup-row">
          <span class="matchup">{g['matchup']}</span>
          <span class="header-badges">
            {odds_badge(g)}
            <span class="nrfi-badge" style="background:{tc};color:#000;font-weight:700">
              {nrfi['score']} {nrfi['tier']}
            </span>
          </span>
        </div>
        <div class="game-meta">{g['venue']} {park_badge(g.get('park'))} · {time_str} · {weather_str(g['weather'])}</div>
      </div>

      <div class="game-body">
        <div class="pitchers-row">
          <div class="pitcher-section">
            <div class="section-label">AWAY SP (faces home lineup)</div>
            {pitcher_card(g['away_pitcher'], g['away_pitcher_stats'], g['away_pitcher_score'], g.get('away_pitcher_hand', '?'), g.get('away_fi'))}
          </div>
          <div class="vs-divider">vs</div>
          <div class="pitcher-section">
            <div class="section-label">HOME SP (faces away lineup)</div>
            {pitcher_card(g['home_pitcher'], g['home_pitcher_stats'], g['home_pitcher_score'], g.get('home_pitcher_hand', '?'), g.get('home_fi'))}
          </div>
        </div>

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

        {team_tendency_panel(g)}

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

        {f5_panel(g)}
      </div>
    </div>"""

    # Count by tier
    cards_html = "\n".join(game_card(g) for g in games)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NRFI + F5 Dashboard — {analysis_date}</title>
<style>
  :root {{
    --bg: #0f172a;
    --surface: #1e293b;
    --surface2: #334155;
    --text: #e2e8f0;
    --text-dim: #94a3b8;
    --accent: #38bdf8;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace;
    background: var(--bg);
    color: var(--text);
    padding: 20px;
    max-width: 1200px;
    margin: 0 auto;
  }}
  .header {{
    text-align: center;
    margin-bottom: 30px;
    padding: 20px;
    background: var(--surface);
    border-radius: 12px;
    border: 1px solid var(--surface2);
  }}
  .header h1 {{
    font-size: 1.8em;
    margin-bottom: 6px;
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
  .meta-pill strong {{
    color: var(--text-dim);
    font-weight: 600;
  }}

  .summary-bar {{
    display: flex;
    gap: 12px;
    justify-content: center;
    margin-bottom: 24px;
    flex-wrap: wrap;
  }}
  .summary-chip {{
    padding: 8px 18px;
    border-radius: 20px;
    font-weight: 600;
    font-size: 0.85em;
    cursor: pointer;
    border: 2px solid transparent;
    transition: all 0.2s;
  }}
  .summary-chip:hover {{ opacity: 0.85; }}
  .summary-chip.active {{ border-color: white; }}

  .filter-row {{
    display: flex;
    gap: 10px;
    justify-content: center;
    margin-bottom: 20px;
    flex-wrap: wrap;
  }}
  .filter-btn {{
    background: var(--surface);
    border: 1px solid var(--surface2);
    color: var(--text);
    padding: 6px 16px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.85em;
    transition: all 0.2s;
  }}
  .filter-btn:hover {{ background: var(--surface2); }}
  .filter-btn.active {{ background: var(--accent); color: #000; font-weight: 600; }}

  .game-card {{
    background: var(--surface);
    border-radius: 12px;
    margin-bottom: 16px;
    border-left: 4px solid;
    overflow: hidden;
    transition: transform 0.1s;
  }}
  .game-card:hover {{ transform: translateX(2px); }}

  .game-header {{
    padding: 14px 18px;
  }}
  .matchup-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .matchup {{ font-size: 1.2em; font-weight: 700; }}
  .nrfi-badge {{
    padding: 4px 14px;
    border-radius: 6px;
    font-size: 0.9em;
    white-space: nowrap;
  }}
  .header-badges {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .odds-badge {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 0.82em;
    background: var(--surface2);
    white-space: nowrap;
  }}
  .odds-book {{
    color: var(--text-dim);
    font-size: 0.85em;
  }}
  .odds-price {{
    font-weight: 700;
    color: var(--text);
  }}
  .odds-prob {{
    color: var(--text-dim);
    font-size: 0.9em;
  }}
  .odds-value {{
    font-weight: 700;
    font-size: 0.85em;
    margin-left: 2px;
  }}
  .game-meta {{ color: var(--text-dim); font-size: 0.82em; margin-top: 4px; }}

  .game-body {{ padding: 14px 18px; }}

  .pitchers-row {{
    display: flex;
    gap: 16px;
    align-items: stretch;
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
  .pitcher-score {{ font-size: 1.6em; font-weight: 800; margin-bottom: 6px; }}
  .stat-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 4px;
  }}
  .stat {{ text-align: center; }}
  .stat-label {{ display: block; font-size: 0.65em; color: var(--text-dim); text-transform: uppercase; }}
  .stat-value {{ display: block; font-size: 0.9em; font-weight: 600; }}

  .lineup-details {{
    margin-top: 12px;
  }}
  .lineup-details summary {{
    cursor: pointer;
    color: var(--accent);
    font-size: 0.85em;
    padding: 6px 0;
  }}
  .lineups-row {{
    display: flex;
    gap: 16px;
    margin-top: 8px;
  }}
  .lineup-section {{ flex: 1; }}
  .batter-table {{
    width: 100%;
    font-size: 0.78em;
    border-collapse: collapse;
  }}
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

  .badge-row {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin: 4px 0 6px 0;
  }}

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
    gap: 16px;
    margin-top: 10px;
    font-size: 0.75em;
    color: var(--text-dim);
    padding-top: 8px;
    border-top: 1px solid var(--surface2);
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
  .tend-adj {{
    font-size: 0.85em;
    font-weight: 700;
  }}
  .tend-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }}
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
  .tend-val {{
    font-size: 1.05em;
    font-weight: 700;
  }}
  .tend-sub {{
    font-size: 0.72em;
    color: var(--text-dim);
    font-weight: 400;
  }}
  .tend-foot {{
    margin-top: 6px;
    font-size: 0.7em;
    color: var(--text-dim);
    font-style: italic;
  }}

  .f5-panel {{
    margin-top: 12px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--surface2);
    border-radius: 8px;
  }}
  .f5-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
  }}
  .f5-title {{
    font-size: 0.78em;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .f5-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 8px;
  }}
  .f5-cell {{
    background: var(--surface);
    border-radius: 6px;
    padding: 8px 10px;
    text-align: center;
  }}
  .f5-cell-label {{
    font-size: 0.65em;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 2px;
  }}
  .f5-cell-val {{
    font-size: 1.05em;
    font-weight: 700;
  }}
  .f5-cell-sub {{
    font-size: 0.7em;
    color: var(--text-dim);
    margin-top: 2px;
  }}
  .f5-lines-row {{
    display: flex;
    gap: 6px;
    margin-top: 8px;
    justify-content: center;
    flex-wrap: wrap;
  }}
  .f5-line-pill {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.7em;
    background: var(--surface);
  }}
  .f5-line-num {{
    color: var(--text-dim);
    font-weight: 600;
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

  .no-games {{
    text-align: center;
    padding: 60px 20px;
    color: var(--text-dim);
    font-size: 1.1em;
  }}

  @media (max-width: 700px) {{
    .pitchers-row, .lineups-row {{ flex-direction: column; }}
    .vs-divider {{ justify-content: center; padding: 4px 0; }}
    body {{ padding: 10px; }}
  }}
</style>
</head>
<body>

<div class="header">
  <h1>NRFI + F5 Dashboard</h1>
  <div class="subtitle">{analysis_date} · Generated {datetime.now().strftime('%I:%M %p')} · {len(games)} games analyzed</div>
  <div class="header-meta">
    <span class="meta-pill"><strong>Stats Through</strong> {stats_through}</span>
  </div>
</div>

<div class="summary-bar">
  <div class="summary-chip" style="background:#22c55e;color:#000" onclick="filterTier('STRONG')">STRONG {len(strong)}</div>
  <div class="summary-chip" style="background:#eab308;color:#000" onclick="filterTier('LEAN')">LEAN {len(lean)}</div>
  <div class="summary-chip" style="background:#f97316;color:#000" onclick="filterTier('TOSS-UP')">TOSS-UP {len(tossup)}</div>
  <div class="summary-chip" style="background:#ef4444;color:#000" onclick="filterTier('FADE')">FADE {len(fade)}</div>
</div>

<div class="filter-row">
  <button class="filter-btn active" onclick="filterTier('ALL')">All Games</button>
  <button class="filter-btn" onclick="filterTier('PICKS')">Top Picks Only</button>
</div>

<div id="games-container">
  {'<div class="no-games">No games found for this date.</div>' if not games else cards_html}
</div>

<div class="methodology">
  <h3>Scoring Methodology</h3>
  <p>Each game is scored 0-100 based on a base composite plus six adjustment factors:</p>
  <p style="margin-top:8px"><strong>Pitching (45%):</strong> Weighted toward the weaker starter (the "chain" model — NRFI needs BOTH pitchers to perform). Factors: ERA, WHIP, K/9, BB/9, HR/9.</p>
  <p style="margin-top:4px"><strong>Lineup Threat (25%):</strong> Top-of-order OPS and OBP for both teams. Weighted toward the more dangerous lineup. Lower threat = higher NRFI score.</p>
  <p style="margin-top:4px"><strong>First-Inning ERA:</strong> How each pitcher performs specifically in the first inning, computed from game logs and linescores. Adjusts -10 to +10. A pitcher with a sub-2.00 first-inning ERA and 85%+ clean first innings gets a significant NRFI boost. Confidence-scaled by starts (15+ = full weight).</p>
  <p style="margin-top:4px"><strong>Batter vs Pitcher:</strong> Career matchup history for each top-of-order batter against the opposing starter. Adjusts -10 to +10 based on OPS in head-to-head at-bats. Confidence-scaled by sample size (30+ AB = full weight).</p>
  <p style="margin-top:4px"><strong>L/R Platoon Splits:</strong> How each batter's top of the order performs against the opposing pitcher's handedness. Same-hand matchups (e.g., LHB vs LHP) favor the pitcher; opposite-hand matchups favor the batter. Adjusts -6 to +6.</p>
  <p style="margin-top:4px"><strong>Recent Form:</strong> Compares each batter's last 7 games OPS to their season OPS. Adjusts -6 to +6. Icons: 🔥 hot, 📈 warm, ➖ steady, 📉 cool, 🧊 cold.</p>
  <p style="margin-top:4px"><strong>Weather/Park:</strong> Temperature, wind, indoor status, and park factor (94-115 scale). Cold + calm + pitcher-friendly park = NRFI boost.</p>
  <p style="margin-top:4px"><strong>Team 1st-Inning Tendency:</strong> Each team's rolling 30-game rate of scoring in the 1st inning, split by home/away role. The home team's HOME rate and the away team's AWAY rate are used (since that's the role they'll be in tonight). Both rates are Bayesian-shrunk toward the team overall (k=10) and then league average ~27% (k=5) so a 14-15 game side sample doesn't dominate. Adjusts ±3 — a deliberately light weight to avoid double-counting the lineup-threat signal already in the model. Acts as a stabilizing prior, especially valuable in early April when individual stats are noisy.</p>
  <h3 style="margin-top:16px">First 5 Innings (F5) Models</h3>
  <p style="margin-top:8px">F5 predictions extend the NRFI analysis to project outcomes over the first 5 innings, where starting pitcher quality dominates.</p>
  <p style="margin-top:4px"><strong>F5 Moneyline:</strong> Power rating per side = 70% pitcher score + 30% inverted lineup threat + adjustments (BvP, platoon, streaks, rest, and pitch-count efficiency). High pitches-per-inning adds an early-exit penalty for that starter. The differential between sides determines the pick and edge strength.</p>
  <p style="margin-top:4px"><strong>F5 Spread:</strong> Derived from the ML edge. A moderate edge (&gt;1 point) supports -0.5; a large edge (&gt;5 points) supports -1.5 coverage.</p>
  <p style="margin-top:4px"><strong>F5 Total:</strong> Projects runs per side based on pitcher quality vs opposing lineup, then sums with park/weather environment adjustments. Compared against common lines (4.0, 4.5, 5.0, 5.5).</p>
  <p style="margin-top:4px"><strong>Confidence Tiers:</strong> STRONG = high-conviction play; LEAN/MODERATE = worth a look; SLIGHT/TOSS-UP = no actionable edge.</p>
  <p style="margin-top:8px;color:#ef4444"><strong>Disclaimer:</strong> This is an analytical tool, not financial advice. All sports betting carries risk. Use this data to inform your own judgment.</p>
</div>

<script>
function filterTier(tier) {{
  const cards = document.querySelectorAll('.game-card');
  const btns = document.querySelectorAll('.filter-btn');
  btns.forEach(b => b.classList.remove('active'));

  if (tier === 'ALL') {{
    cards.forEach(c => c.style.display = 'block');
    btns[0].classList.add('active');
  }} else if (tier === 'PICKS') {{
    cards.forEach(c => {{
      const badge = c.querySelector('.nrfi-badge').textContent.trim();
      c.style.display = (badge.includes('STRONG') || badge.includes('LEAN')) ? 'block' : 'none';
    }});
    btns[1].classList.add('active');
  }} else {{
    cards.forEach(c => {{
      const badge = c.querySelector('.nrfi-badge').textContent.trim();
      c.style.display = badge.includes(tier) ? 'block' : 'none';
    }});
  }}
}}
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
