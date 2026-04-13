#!/usr/bin/env python3
"""
Hit Rate Tracker
=================
Reads predictions.csv + outcomes.csv, computes cumulative and 7-day rolling
hit rates per market and confidence tier, and generates a self-contained HTML
dashboard with SVG line charts showing how accuracy evolves over time.

Push rules (pushes count as losses — sportsbooks run three-way F5 markets):
  - F5 ML: tie after 5 innings = loss
  - F5 Spread -0.5: win by exactly 0 = loss
  - F5 Spread -1.5: win by exactly 1 = loss
  - F5 Total: actual == line = loss

Usage (called automatically by run_nrfi.py):
    from hit_rate_tracker import generate_hit_rate_dashboard
    generate_hit_rate_dashboard(predictions_csv, outcomes_csv, output_path)
"""

import csv
import json
import os
from collections import defaultdict


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_csv(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Hit determination — pushes = losses
# ---------------------------------------------------------------------------

def _f5_ml_hit(pred, winner_side):
    """True = hit, False = loss (including ties). None = skip (no pick)."""
    pick = pred.get("f5_ml_pick", "").strip()
    if not pick or not winner_side:
        return None
    if winner_side == "tie":
        return False  # push = loss
    away = pred.get("away_team", "").strip()
    home = pred.get("home_team", "").strip()
    if pick == away or (away and pick in away):
        pick_side = "away"
    elif pick == home or (home and pick in home):
        pick_side = "home"
    else:
        return None
    return pick_side == winner_side


def _f5_spread_hit(pred, away_f5, home_f5):
    """True = covers, False = miss/push. None = no pick or unparseable."""
    label = pred.get("f5_spread_label", "").strip()
    pick = pred.get("f5_ml_pick", "").strip()
    if not label or not pick:
        return None
    # Parse line from last token, e.g. "NYY -0.5" -> -0.5
    parts = label.split()
    try:
        line = float(parts[-1])
    except (ValueError, IndexError):
        return None
    away = pred.get("away_team", "").strip()
    home = pred.get("home_team", "").strip()
    if pick == away or (away and pick in away):
        margin = away_f5 - home_f5
    elif pick == home or (home and pick in home):
        margin = home_f5 - away_f5
    else:
        return None
    # Covers if margin strictly exceeds the spread threshold
    # -0.5: need margin >= 1   → margin > 0.5 ✓
    # -1.5: need margin >= 2   → margin > 1.5 ✓
    # push (margin = 0 or 1 depending on line) falls through to False
    return margin > abs(line)


def _f5_total_hit(pred, actual_total):
    """True = correct side, False = wrong side or push. None = no data."""
    lean = pred.get("f5_total_lean", "").strip()
    line_str = pred.get("f5_total_line", "").strip()
    if not lean or not line_str:
        return None
    try:
        line = float(line_str)
        actual = float(actual_total)
    except (ValueError, TypeError):
        return None
    if lean == "OVER":
        return actual > line  # exact = False (loss)
    if lean == "UNDER":
        return actual < line  # exact = False (loss)
    return None


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_hit_rates(predictions_csv, outcomes_csv):
    """
    Join predictions + outcomes on game_pk and compute per-day cumulative
    hit-rate series for all four markets.

    Returns:
    {
      "nrfi":      { "STRONG": [{date, day_hits, day_n, cum_hits, cum_n, cum_rate}, ...], ... },
      "f5_ml":     { "STRONG": [...], ... },
      "f5_spread": { "STRONG": [...], ... },
      "f5_total":  { "STRONG OVER": [...], ... },
      "summary":   { total_resolved, overall_nrfi_hits, overall_nrfi_n, overall_nrfi_rate, days }
    }
    """
    preds = _load_csv(predictions_csv)
    outcomes = _load_csv(outcomes_csv)
    if not preds or not outcomes:
        return {}

    outcome_map = {
        str(r.get("game_pk", "")).strip(): r
        for r in outcomes
        if str(r.get("game_pk", "")).strip()
    }

    # Inner-join; only Final games count
    joined = []
    for p in preds:
        pk = str(p.get("game_pk", "")).strip()
        o = outcome_map.get(pk)
        if not o or o.get("game_status") != "Final":
            continue
        game_date = o.get("game_date", "").strip()
        if not game_date:
            continue
        joined.append((game_date, p, o))

    if not joined:
        return {}

    # Bucket raw results by (market, tier, date)
    # Structure: market_buckets[tier][date] -> [True/False, ...]
    nrfi_b      = defaultdict(lambda: defaultdict(list))
    f5_ml_b     = defaultdict(lambda: defaultdict(list))
    f5_spread_b = defaultdict(lambda: defaultdict(list))
    f5_total_b  = defaultdict(lambda: defaultdict(list))

    for game_date, p, o in joined:
        # --- NRFI ---
        nrfi_raw = o.get("nrfi_actual", "").strip()
        if nrfi_raw in ("0", "1"):
            tier = p.get("tier", "").strip()
            if tier:
                nrfi_b[tier][game_date].append(nrfi_raw == "1")

        # --- F5 markets (only when both sides completed 5 innings) ---
        if str(o.get("f5_innings_complete", "")).strip() != "1":
            continue
        try:
            away_f5 = float(o.get("away_runs_f5", ""))
            home_f5 = float(o.get("home_runs_f5", ""))
            total_f5 = float(o.get("f5_total_actual", ""))
        except (ValueError, TypeError):
            continue

        winner = o.get("f5_ml_winner_side", "").strip()

        # F5 ML
        ml_conf = p.get("f5_ml_confidence", "").strip()
        if ml_conf:
            h = _f5_ml_hit(p, winner)
            if h is not None:
                f5_ml_b[ml_conf][game_date].append(h)

        # F5 Spread
        sp_conf = p.get("f5_spread_confidence", "").strip()
        if sp_conf:
            h = _f5_spread_hit(p, away_f5, home_f5)
            if h is not None:
                f5_spread_b[sp_conf][game_date].append(h)

        # F5 Total
        tot_conf = p.get("f5_total_confidence", "").strip()
        tot_lean = p.get("f5_total_lean", "").strip()
        if tot_conf and tot_lean:
            key = f"{tot_conf} {tot_lean}"
            h = _f5_total_hit(p, total_f5)
            if h is not None:
                f5_total_b[key][game_date].append(h)

    def build_series(buckets):
        """Convert bucketed results into sorted cumulative series per tier."""
        result = {}
        for tier, date_hits in buckets.items():
            cum_hits = cum_n = 0
            series = []
            for d in sorted(date_hits):
                hits = date_hits[d]
                day_h = sum(1 for x in hits if x)
                day_n = len(hits)
                cum_hits += day_h
                cum_n += day_n
                series.append({
                    "date": d,
                    "day_hits": day_h,
                    "day_n": day_n,
                    "cum_hits": cum_hits,
                    "cum_n": cum_n,
                    "cum_rate": round(100.0 * cum_hits / cum_n, 1) if cum_n else 0.0,
                })
            if series:
                result[tier] = series
        return result

    # Summary stats
    all_nrfi = [x for tier_series in build_series(nrfi_b).values() for x in tier_series]
    total_nrfi_hits = sum(s["day_hits"] for s in all_nrfi)
    total_nrfi_n    = sum(s["day_n"] for s in all_nrfi)
    all_dates = sorted({game_date for game_date, _, _ in joined})

    return {
        "nrfi":      build_series(nrfi_b),
        "f5_ml":     build_series(f5_ml_b),
        "f5_spread": build_series(f5_spread_b),
        "f5_total":  build_series(f5_total_b),
        "summary": {
            "total_resolved":    len(joined),
            "overall_nrfi_hits": total_nrfi_hits,
            "overall_nrfi_n":    total_nrfi_n,
            "overall_nrfi_rate": round(100.0 * total_nrfi_hits / total_nrfi_n, 1) if total_nrfi_n else 0.0,
            "days":              len(all_dates),
            "first_date":        all_dates[0] if all_dates else "",
            "last_date":         all_dates[-1] if all_dates else "",
        },
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

# Tier display order and colors
TIER_META = {
    # NRFI tiers
    "STRONG":    {"color": "#4ade80", "label": "Strong"},
    "LEAN":      {"color": "#facc15", "label": "Lean"},
    "TOSS-UP":   {"color": "#94a3b8", "label": "Toss-Up"},
    "FADE":      {"color": "#f87171", "label": "Fade"},
    # F5 ML confidence
    "MODERATE":  {"color": "#60a5fa", "label": "Moderate"},
    # F5 Total combinations
    "STRONG OVER":  {"color": "#4ade80", "label": "Strong Over"},
    "LEAN OVER":    {"color": "#86efac", "label": "Lean Over"},
    "STRONG UNDER": {"color": "#818cf8", "label": "Strong Under"},
    "LEAN UNDER":   {"color": "#a5b4fc", "label": "Lean Under"},
    "SLIGHT OVER":  {"color": "#d4d4d8", "label": "Slight Over"},
    "SLIGHT UNDER": {"color": "#a1a1aa", "label": "Slight Under"},
    # F5 Spread confidence (same as NRFI tiers, reuse)
}

MARKET_CONFIG = [
    {
        "key": "nrfi",
        "title": "NRFI Hit Rate",
        "subtitle": "No Run First Inning — by confidence tier",
        "tier_order": ["STRONG", "LEAN", "TOSS-UP", "FADE"],
    },
    {
        "key": "f5_ml",
        "title": "F5 Moneyline Hit Rate",
        "subtitle": "First 5 innings ML — by confidence tier (ties = loss)",
        "tier_order": ["STRONG", "MODERATE", "LEAN"],
    },
    {
        "key": "f5_spread",
        "title": "F5 Spread Hit Rate",
        "subtitle": "First 5 innings spread — by confidence (pushes = loss)",
        "tier_order": ["STRONG", "LEAN", "SLIGHT"],
    },
    {
        "key": "f5_total",
        "title": "F5 Total Hit Rate",
        "subtitle": "First 5 innings total — by lean & confidence (pushes = loss)",
        "tier_order": ["STRONG OVER", "LEAN OVER", "STRONG UNDER", "LEAN UNDER"],
    },
]


_CSS = """
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #0f1117;
    --surface:  #1a1d27;
    --surface2: #222636;
    --border:   #2e3347;
    --text:     #e2e8f0;
    --muted:    #94a3b8;
    --accent:   #4ade80;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', 'Consolas', monospace;
    font-size: 13px;
    line-height: 1.5;
    padding: 24px 20px 48px;
    min-height: 100vh;
  }

  .header {
    display: flex;
    align-items: baseline;
    gap: 16px;
    margin-bottom: 24px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 16px;
  }
  .header-title { font-size: 1.4em; font-weight: 700; letter-spacing: 0.04em; color: var(--text); }
  .header-sub   { color: var(--muted); font-size: 0.85em; }

  .summary-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 32px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .stat-label { color: var(--muted); font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }
  .stat-value { font-size: 1.6em; font-weight: 700; color: var(--text); }
  .stat-sub   { color: var(--muted); font-size: 0.75em; margin-top: 2px; }

  .charts-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(480px, 1fr));
    gap: 20px;
  }
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px 20px 16px;
    overflow: hidden;
  }
  .chart-title { font-size: 1em; font-weight: 700; margin-bottom: 2px; }
  .chart-sub   { color: var(--muted); font-size: 0.78em; margin-bottom: 14px; }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    margin-bottom: 14px;
  }
  .legend-item  { display: flex; align-items: center; gap: 6px; font-size: 0.8em; }
  .legend-dot   { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { color: var(--muted); }
  .legend-n     { color: var(--muted); font-size: 0.9em; }

  .chart-svg { display: block; width: 100%; overflow: visible; }
  .chart-svg text { font-family: inherit; }

  #tooltip {
    position: fixed;
    pointer-events: none;
    background: #1e2235;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 0.82em;
    line-height: 1.6;
    white-space: nowrap;
    z-index: 999;
    display: none;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5);
  }
  #tooltip .tt-date { font-weight: 700; color: var(--text); margin-bottom: 4px; }
  #tooltip .tt-row  { display: flex; justify-content: space-between; gap: 20px; color: var(--muted); }
  #tooltip .tt-hit  { font-weight: 600; }

  .no-data       { text-align: center; padding: 80px 24px; color: var(--muted); }
  .no-data-icon  { font-size: 3em; margin-bottom: 16px; }
  .no-data-title { font-size: 1.2em; font-weight: 700; color: var(--text); margin-bottom: 8px; }
  .no-data-sub   { font-size: 0.9em; line-height: 1.7; }
  code { background: var(--surface2); padding: 1px 6px; border-radius: 4px; font-size: 0.95em; }

  @media (max-width: 600px) {
    .charts-grid { grid-template-columns: 1fr; }
  }
"""

_JS = """
const PAD = { top: 18, right: 28, bottom: 46, left: 50 };
const VW  = 560;
const VH  = 260;

function xOf(allDates, dateStr) {
  const n = allDates.length;
  const i = allDates.indexOf(dateStr);
  if (n <= 1) return PAD.left + (VW - PAD.left - PAD.right) / 2;
  return PAD.left + (i / (n - 1)) * (VW - PAD.left - PAD.right);
}
function yOf(rate) {
  return PAD.top + (1 - rate / 100) * (VH - PAD.top - PAD.bottom);
}

function rollingPoints(series, W) {
  W = W || 7;
  var pts = [];
  for (var i = W - 1; i < series.length; i++) {
    var rh = 0, rn = 0;
    for (var j = i - W + 1; j <= i; j++) { rh += series[j].day_hits; rn += series[j].day_n; }
    if (rn > 0) pts.push({ date: series[i].date, rate: 100 * rh / rn });
  }
  return pts;
}

function svgEl(tag, attrs, inner) {
  var attrStr = Object.keys(attrs).map(function(k) { return k + '="' + attrs[k] + '"'; }).join(' ');
  return inner !== undefined
    ? '<' + tag + ' ' + attrStr + '>' + inner + '</' + tag + '>'
    : '<' + tag + ' ' + attrStr + '/>';
}

function buildChart(cfg) {
  var marketData = CHARTS_DATA[cfg.key] || {};
  var tiers = cfg.tiers.filter(function(t) { return (marketData[t.key] || []).length > 0; });
  if (!tiers.length) return null;

  var dateSet = {};
  tiers.forEach(function(t) {
    (marketData[t.key] || []).forEach(function(p) { dateSet[p.date] = 1; });
  });
  var allDates = Object.keys(dateSet).sort();
  var nDates   = allDates.length;
  var innerW   = VW - PAD.left - PAD.right;
  var innerH   = VH - PAD.top  - PAD.bottom;

  var els = [];

  // Background
  els.push(svgEl('rect', { x:0, y:0, width:VW, height:VH, fill:'#1a1d27', rx:0 }));

  // Gridlines + Y labels
  [0, 25, 50, 75, 100].forEach(function(v) {
    var y = yOf(v).toFixed(1);
    els.push(svgEl('line', { x1:PAD.left, y1:y, x2:VW-PAD.right, y2:y,
      stroke: v === 50 ? '#334155' : '#1e2435', 'stroke-width': v === 50 ? 1 : 0.8 }));
    els.push(svgEl('text', { x: PAD.left - 6, y: (parseFloat(y)+4).toFixed(1),
      'text-anchor':'end', fill:'#475569', 'font-size':10 }, v + '%'));
  });

  // Reference line at 52.4%
  var refY = yOf(52.4).toFixed(1);
  els.push(svgEl('line', { x1:PAD.left, y1:refY, x2:VW-PAD.right, y2:refY,
    stroke:'rgba(250,204,21,0.45)', 'stroke-width':'1.2', 'stroke-dasharray':'5,4' }));
  els.push(svgEl('text', { x: VW-PAD.right+4, y: (parseFloat(refY)+4).toFixed(1),
    fill:'rgba(250,204,21,0.6)', 'font-size':9 }, '52.4%'));

  // X-axis ticks + labels
  var tickEvery = nDates > 14 ? 7 : nDates > 7 ? 3 : 1;
  allDates.forEach(function(d, i) {
    if (i % tickEvery !== 0 && i !== nDates - 1) return;
    var x = xOf(allDates, d).toFixed(1);
    var axisY = (PAD.top + innerH).toFixed(1);
    els.push(svgEl('line', { x1:x, y1:axisY, x2:x, y2:(parseFloat(axisY)+4).toFixed(1),
      stroke:'#334155', 'stroke-width':1 }));
    els.push(svgEl('text', { x:x, y:(parseFloat(axisY)+15).toFixed(1),
      'text-anchor':'middle', fill:'#475569', 'font-size':'9.5' }, d.slice(5)));
  });

  // Axes
  els.push(svgEl('line', { x1:PAD.left, y1:PAD.top, x2:PAD.left, y2:PAD.top+innerH,
    stroke:'#334155', 'stroke-width':1 }));
  els.push(svgEl('line', { x1:PAD.left, y1:PAD.top+innerH, x2:VW-PAD.right, y2:PAD.top+innerH,
    stroke:'#334155', 'stroke-width':1 }));

  // Lines + dots per tier
  var dotData = [];
  tiers.forEach(function(t) {
    var series = marketData[t.key] || [];
    if (!series.length) return;
    var col = t.color;

    // Cumulative solid line
    if (series.length > 1) {
      var pts = series.map(function(p) {
        return xOf(allDates, p.date).toFixed(1) + ',' + yOf(p.cum_rate).toFixed(1);
      }).join(' ');
      els.push(svgEl('polyline', { points:pts, fill:'none', stroke:col,
        'stroke-width':'2.2', 'stroke-linejoin':'round', 'stroke-linecap':'round' }));
    }

    // 7-day rolling dashed line
    var roll = rollingPoints(series, 7);
    if (roll.length > 1) {
      var rpts = roll.map(function(p) {
        return xOf(allDates, p.date).toFixed(1) + ',' + yOf(p.rate).toFixed(1);
      }).join(' ');
      els.push(svgEl('polyline', { points:rpts, fill:'none', stroke:col,
        'stroke-width':'1.4', 'stroke-dasharray':'5,3', opacity:'0.5',
        'stroke-linejoin':'round' }));
    }

    // Collect dots
    series.forEach(function(p) {
      dotData.push(Object.assign({}, p, { tier: t.label, color: col }));
    });
  });

  // Dots on top (so they render above lines)
  dotData.forEach(function(p) {
    var x = xOf(allDates, p.date).toFixed(1);
    var y = yOf(p.cum_rate).toFixed(1);
    var enc = encodeURIComponent(JSON.stringify(p));
    els.push('<circle cx="' + x + '" cy="' + y + '" r="4" fill="' + p.color +
      '" stroke="#1a1d27" stroke-width="1.5" class="chart-dot" data-pt="' + enc +
      '" style="cursor:pointer"/>');
  });

  return '<svg class="chart-svg" viewBox="0 0 ' + VW + ' ' + VH +
    '" preserveAspectRatio="xMidYMid meet">' + els.join('') + '</svg>';
}

function buildLegend(cfg) {
  var marketData = CHARTS_DATA[cfg.key] || {};
  var tiers = cfg.tiers.filter(function(t) { return (marketData[t.key] || []).length > 0; });
  if (!tiers.length) return '';
  var items = tiers.map(function(t) {
    var series = marketData[t.key] || [];
    var last = series[series.length - 1];
    var n = last ? last.cum_n : 0;
    return '<div class="legend-item">' +
      '<span class="legend-dot" style="background:' + t.color + '"></span>' +
      '<span class="legend-label">' + t.label + '</span>' +
      '<span class="legend-n">(' + n + ')</span>' +
      '</div>';
  });
  items.push('<div class="legend-item" style="margin-left:8px;opacity:0.55">' +
    '<span style="display:inline-block;width:18px;height:0;border-top:2px dashed #94a3b8;vertical-align:middle"></span>' +
    '&nbsp;<span class="legend-label" style="font-size:0.85em">7-day rolling</span>' +
    '</div>');
  return '<div class="legend">' + items.join('') + '</div>';
}

function renderCharts() {
  var grid = document.getElementById('chartsGrid');
  if (!grid) return;
  MARKET_CONFIGS.forEach(function(cfg) {
    var svgHtml = buildChart(cfg);
    if (!svgHtml) return;
    var card = document.createElement('div');
    card.className = 'chart-card';
    card.innerHTML =
      '<div class="chart-title">' + cfg.title + '</div>' +
      '<div class="chart-sub">' + cfg.subtitle + '</div>' +
      buildLegend(cfg) + svgHtml;
    grid.appendChild(card);
  });
}

function initTooltip() {
  var tt = document.getElementById('tooltip');
  document.addEventListener('mousemove', function(e) {
    var dot = e.target.closest('.chart-dot');
    if (!dot) { tt.style.display = 'none'; return; }
    var p = JSON.parse(decodeURIComponent(dot.dataset.pt));
    var dayRate = p.day_n ? (100 * p.day_hits / p.day_n).toFixed(1) : '—';
    tt.innerHTML =
      '<div class="tt-date">' + p.date + '</div>' +
      '<div class="tt-row"><span>' + p.tier + '</span></div>' +
      '<div class="tt-row"><span>Cumulative</span>' +
        '<span class="tt-hit" style="color:' + p.color + '">' + p.cum_rate + '%</span></div>' +
      '<div class="tt-row"><span>Today</span>' +
        '<span class="tt-hit">' + dayRate + '% (' + p.day_hits + '/' + p.day_n + ')</span></div>' +
      '<div class="tt-row"><span>Total</span>' +
        '<span>' + p.cum_hits + '/' + p.cum_n + '</span></div>';
    tt.style.display = 'block';
    var margin = 14;
    var left = e.clientX + margin;
    var top  = e.clientY - tt.offsetHeight / 2;
    if (left + tt.offsetWidth > window.innerWidth - 8) left = e.clientX - tt.offsetWidth - margin;
    if (top < 8) top = 8;
    tt.style.left = left + 'px';
    tt.style.top  = top  + 'px';
  });
}

renderCharts();
initTooltip();
"""


def _build_html(data):
    summary = data.get("summary", {})
    total   = summary.get("total_resolved", 0)
    nrfi_rate = summary.get("overall_nrfi_rate", 0.0)
    nrfi_n    = summary.get("overall_nrfi_n", 0)
    days      = summary.get("days", 0)
    first_d   = summary.get("first_date", "—")
    last_d    = summary.get("last_date", "—")

    has_data = bool(total)

    # Serialise chart data as JSON for the JS renderer
    charts_json = json.dumps({
        cfg["key"]: {
            tier: data.get(cfg["key"], {}).get(tier, [])
            for tier in cfg["tier_order"]
            if tier in data.get(cfg["key"], {})
        }
        for cfg in MARKET_CONFIG
    }, indent=None)

    market_configs_json = json.dumps([
        {
            "key": cfg["key"],
            "title": cfg["title"],
            "subtitle": cfg["subtitle"],
            "tiers": [
                {
                    "key": t,
                    "label": TIER_META.get(t, {}).get("label", t),
                    "color": TIER_META.get(t, {}).get("color", "#ffffff"),
                }
                for t in cfg["tier_order"]
                if t in data.get(cfg["key"], {})
            ],
        }
        for cfg in MARKET_CONFIG
    ])

    nrfi_rate_str = f"{nrfi_rate:.1f}%" if nrfi_n else "—"
    date_range    = f"{first_d} \u2192 {last_d}" if first_d and first_d != "—" else "—"

    no_data_block = (
        ""
        if has_data else
        '<div class="no-data">'
        '<div class="no-data-icon">\U0001f4ca</div>'
        '<div class="no-data-title">No resolved predictions yet</div>'
        '<div class="no-data-sub">Charts will appear once outcomes have been fetched '
        'for at least one completed game date.<br>'
        'Run <code>python backtest.py</code> to pull outcomes.</div>'
        '</div>'
    )
    charts_block = '<div class="charts-grid" id="chartsGrid"></div>' if has_data else ""

    summary_cards = (
        '<div class="summary-row">'
        '<div class="stat-card">'
        '<div class="stat-label">Resolved Games</div>'
        '<div class="stat-value">' + str(total) + '</div>'
        '<div class="stat-sub">' + date_range + '</div>'
        '</div>'
        '<div class="stat-card">'
        '<div class="stat-label">NRFI Hit Rate</div>'
        '<div class="stat-value">' + nrfi_rate_str + '</div>'
        '<div class="stat-sub">' + str(nrfi_n) + ' graded predictions</div>'
        '</div>'
        '<div class="stat-card">'
        '<div class="stat-label">Days of Data</div>'
        '<div class="stat-value">' + str(days) + '</div>'
        '<div class="stat-sub">game-days resolved</div>'
        '</div>'
        '<div class="stat-card">'
        '<div class="stat-label">Break-Even</div>'
        '<div class="stat-value">52.4%</div>'
        '<div class="stat-sub">for -110 lines</div>'
        '</div>'
        '</div>'
    )

    data_script = (
        "const CHARTS_DATA = " + charts_json + ";\n"
        "const MARKET_CONFIGS = " + market_configs_json + ";\n"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>NRFI / F5 Hit Rate Tracker</title>\n"
        "<style>\n" + _CSS + "\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="header">'
        '<span class="header-title">NRFI / F5 Hit Rate Tracker</span>'
        '<span class="header-sub">Cumulative accuracy over time \u00b7 pushes = losses</span>'
        '</div>\n'
        + summary_cards
        + no_data_block
        + "\n"
        + charts_block
        + '\n<div id="tooltip"></div>\n'
        "<script>\n"
        + data_script
        + _JS
        + "\n</script>\n"
        "</body>\n"
        "</html>\n"
    )
def generate_hit_rate_dashboard(predictions_csv, outcomes_csv, output_path):
    """
    Compute hit rates from predictions + outcomes CSVs and write a self-contained
    HTML dashboard to output_path. Safe to call even when CSVs are missing or empty.
    """
    try:
        data = compute_hit_rates(predictions_csv, outcomes_csv)
        html = _build_html(data)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write(html)
    except Exception as e:
        # Never crash the main workflow
        print(f"  [hit_rate_tracker] Warning: could not generate dashboard — {e}")
