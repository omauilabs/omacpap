"""Printable, self-contained HTML report — bring it to a sleep-clinic visit or
save it as PDF from the browser's print dialog."""

from __future__ import annotations

import datetime as dt
import html
from typing import Any

from . import __version__, analysis, db, secrets_store


def _svg_bars(values: list[float | None], labels: list[str], *, ref: float | None, color: str,
              ref_color: str = "#b00020", height: int = 140, unit: str = "") -> str:
    w, pad_l, pad_b, pad_t = 720, 34, 18, 10
    vmax = max([v for v in values if v is not None] + [ref or 0, 1]) * 1.1
    n = max(len(values), 1)
    bw = (w - pad_l) / n
    ph = height - pad_b - pad_t
    parts = [f'<svg viewBox="0 0 {w} {height}" class="chart" role="img">']
    for frac in (0, 0.5, 1):
        y = pad_t + ph - frac * ph
        parts.append(f'<line x1="{pad_l}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
                     f'<text x="{pad_l - 4}" y="{y + 3:.1f}" class="ax" text-anchor="end">{vmax * frac:.0f}</text>')
    for i, v in enumerate(values):
        if v is None:
            continue
        h = v / vmax * ph
        parts.append(f'<rect x="{pad_l + i * bw + bw * 0.12:.1f}" y="{pad_t + ph - h:.1f}" width="{bw * 0.76:.1f}" '
                     f'height="{h:.1f}" fill="{color}"><title>{html.escape(labels[i])}: {v:.1f}{unit}</title></rect>')
    if ref is not None:
        y = pad_t + ph - ref / vmax * ph
        parts.append(f'<line x1="{pad_l}" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" stroke="{ref_color}" '
                     f'stroke-dasharray="4 3" stroke-width="1.2"/>')
    step = max(1, n // 8)
    for i in range(0, n, step):
        parts.append(f'<text x="{pad_l + i * bw + bw / 2:.1f}" y="{height - 4}" class="ax" '
                     f'text-anchor="middle">{html.escape(labels[i][5:])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _fmt(v: Any, spec: str = ".1f", dash: str = "—") -> str:
    if v is None:
        return dash
    try:
        return format(v, spec)
    except (TypeError, ValueError):
        return html.escape(str(v))


def render(days: int = 90) -> str:
    cfg = secrets_store.load_config()
    goal, leak_ref, ahi_ref = float(cfg["usage_goal_hours"]), float(cfg["leak_reference"]), float(cfg["ahi_reference"])
    with db.session() as con:
        rows = db.nights(con)
        dev = con.execute("SELECT * FROM devices ORDER BY updated_at DESC LIMIT 1").fetchone()
        first_name = db.meta_get(con, "patient_first_name") or ""
    if not rows:
        return "<!doctype html><title>OmaCPAP report</title><p>No data yet — sync myAir first.</p>"
    end = dt.date.today() - dt.timedelta(days=1)
    all_days = analysis.calendar_fill(rows, end=max(end, dt.date.fromisoformat(rows[-1]["date"])))
    if all_days and all_days[-1].get("missing"):
        all_days = all_days[:-1]
    period = all_days[-days:]
    prior = all_days[-2 * days:-days] if len(all_days) > days else []
    s = analysis.window_summary(period, goal, leak_ref)
    p = analysis.window_summary(prior, goal, leak_ref) if prior else {}
    best = analysis.best_30_day_window(period, goal)
    labels = [n["date"] for n in period]
    usage = [(n.get("usage_min") or 0) / 60 for n in period]
    ahi = [n.get("ahi") for n in period]
    leak = [n.get("leak_lpm") for n in period]

    def row(label: str, key: str, spec: str = ".1f", unit: str = "") -> str:
        return (f"<tr><th>{label}</th><td>{_fmt(s.get(key), spec)}{unit}</td>"
                f"<td>{_fmt(p.get(key), spec) + unit if p else '—'}</td></tr>")

    nightly = "".join(
        f"<tr><td>{n['date']}</td><td>{_fmt((n.get('usage_min') or 0) / 60, '.2f')}</td>"
        f"<td>{_fmt(n.get('ahi'))}</td><td>{_fmt(n.get('leak_lpm'))}</td><td>{_fmt(n.get('mask_pairs'), 'd')}</td>"
        f"<td>{_fmt(n.get('sleep_score'), 'd')}</td><td class='note'>{html.escape(n.get('note') or '')}</td></tr>"
        for n in reversed(period))
    dev_line = ""
    if dev:
        dev_line = f"{html.escape(dev['name'] or 'ResMed device')} · serial ending {html.escape((dev['serial'] or '')[-4:])}"
        if dev["mask_code"]:
            dev_line += f" · mask {html.escape(dev['mask_code'])}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CPAP report — {period[0]['date']} to {period[-1]['date']}</title>
<style>
  :root {{ --ink:#1b1d22; --muted:#5b6070; --line:#d9dce3; --accent:#2f5bd3; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--ink);
         max-width: 800px; margin: 24px auto; padding: 0 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 2px; }} h2 {{ font-size: 15px; margin: 26px 0 8px;
         border-bottom: 1px solid var(--line); padding-bottom: 4px; }}
  .sub {{ color: var(--muted); }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--line); }}
  thead th {{ font-weight: 600; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }}
  td.note {{ color: var(--muted); max-width: 240px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 14px; }}
  .kpi {{ border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }}
  .kpi b {{ display: block; font-size: 20px; }} .kpi span {{ color: var(--muted); font-size: 11px; }}
  .chart {{ width: 100%; height: auto; }} .chart .grid {{ stroke: var(--line); }}
  .chart .ax {{ font-size: 9px; fill: var(--muted); }}
  .legend {{ color: var(--muted); font-size: 11px; margin-top: -2px; }}
  .fine {{ color: var(--muted); font-size: 11px; margin-top: 28px; }}
  @media print {{ body {{ margin: 0; }} h2 {{ break-after: avoid; }} tr {{ break-inside: avoid; }}
                  .noprint {{ display: none; }} }}
</style></head><body>
<p class="noprint sub">Tip: press <b>Ctrl+P</b> → “Save as PDF” to keep a copy.</p>
<h1>CPAP therapy report{' — ' + html.escape(first_name) if first_name else ''}</h1>
<div class="sub">{period[0]['date']} to {period[-1]['date']} ({len(period)} nights) · {dev_line}</div>
<div class="kpis">
  <div class="kpi"><b>{_fmt(s['compliance_pct'], '.0f')}%</b><span>nights ≥ {goal:g} h ({s['nights_goal']}/{s['days']})</span></div>
  <div class="kpi"><b>{_fmt(s['usage_avg_h'])} h</b><span>avg use, nights used</span></div>
  <div class="kpi"><b>{_fmt(s['ahi_weighted'])}</b><span>AHI (usage-weighted)</span></div>
  <div class="kpi"><b>{_fmt(s['leak_median'])}</b><span>median leak, L/min</span></div>
</div>

<h2>Summary</h2>
<table><thead><tr><th></th><th>Last {len(period)} nights</th><th>Previous {len(prior)} nights</th></tr></thead><tbody>
{row('Nights used', 'nights_used', 'd')}
{row(f'Nights ≥ {goal:g} h', 'nights_goal', 'd')}
{row('Compliance', 'compliance_pct', '.1f', '%')}
{row('Average use (nights used)', 'usage_avg_h', '.2f', ' h')}
{row('Median use', 'usage_median_h', '.2f', ' h')}
{row('AHI, usage-weighted', 'ahi_weighted', '.2f')}
{row('AHI, median night', 'ahi_median', '.2f')}
{row('AHI, highest night', 'ahi_max', '.1f')}
{row('Nights with AHI ≥ 5', 'nights_ahi_over_5', 'd')}
{row('Leak, median (L/min)', 'leak_median', '.1f')}
{row(f'Nights with leak ≥ {leak_ref:g} L/min', 'nights_leak_over_ref', 'd')}
{row('Mask on/off per night', 'mask_pairs_avg', '.1f')}
{row('myAir score, average', 'score_avg', '.0f')}
</tbody></table>
{f"<p class='sub'>Best 30-day stretch in this period: {best['nights']}/30 nights ≥ {goal:g} h ({best['pct']:.0f}%), {best['start']} to {best['end']}.</p>" if best else ""}

<h2>Nightly use (hours)</h2>
{_svg_bars(usage, labels, ref=goal, color="#2f5bd3", unit=" h")}
<div class="legend">Dashed line: {goal:g}-hour goal.</div>
<h2>AHI (events per hour)</h2>
{_svg_bars(ahi, labels, ref=ahi_ref, color="#7a4fd1")}
<div class="legend">Dashed line: {ahi_ref:g} events/hr reference.</div>
<h2>Mask leak (L/min)</h2>
{_svg_bars(leak, labels, ref=leak_ref, color="#1f8a70", unit=" L/min")}
<div class="legend">Dashed line: {leak_ref:g} L/min (ResMed's large-leak threshold).</div>

<h2>Night by night</h2>
<table><thead><tr><th>Date</th><th>Hours</th><th>AHI</th><th>Leak</th><th>Mask on/off</th><th>Score</th><th>Note</th></tr></thead>
<tbody>{nightly}</tbody></table>

<p class="fine">Generated by OmaCPAP {__version__} on {dt.date.today():%B %-d, %Y} from data recorded by the
CPAP machine and reported through ResMed myAir. Figures are what the device reported; this report is not a
medical assessment. Discuss any therapy questions or changes with your clinician.</p>
</body></html>"""
