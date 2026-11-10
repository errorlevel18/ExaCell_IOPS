"""Gráficos SVG autocontenidos (sin dependencias ni Internet).

* Líneas de 2 px, rejilla recesiva de 1 px, máx. 3 series (paleta validada all-pairs).
* Referencias como línea DISCONTINUA con etiqueta en tinta de texto
  («máximo publicado/teórico»), nunca con color de serie ni como «capacidad garantizada».
* Las series largas se reducen por MÁXIMO en cada intervalo para no ocultar picos.
* Cada gráfico lleva los datos en JSON para el tooltip con cruceta (JS en línea del informe).
"""
from __future__ import annotations

import html
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

W, H = 860, 300
ML, MR, MT, MB = 70, 200, 18, 40
OUT_OF_SCALE = 5.0  # una referencia > 5x el pico observado se indica fuera de escala
MAX_POINTS = 720


def downsample_max(values: Dict[int, Decimal], max_points: int = MAX_POINTS) -> Tuple[List[Tuple[int, float]], int]:
    """Reduce a <= max_points tomando el máximo por intervalo. Devuelve (puntos, minutos por punto)."""
    items = sorted(values.items())
    if not items:
        return [], 1
    if len(items) <= max_points:
        return [(k, float(v)) for k, v in items], 1
    span = items[-1][0] - items[0][0]
    bucket = max(60, int(math.ceil(span / max_points / 60.0)) * 60)
    out: Dict[int, Tuple[int, float]] = {}
    for k, v in items:
        b = items[0][0] + (k - items[0][0]) // bucket * bucket
        if b not in out or float(v) > out[b][1]:
            out[b] = (b, float(v))
    return [out[b] for b in sorted(out)], bucket // 60


def nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * exp >= v:
            return m * exp
    return 10 * exp


def fmt_axis(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if v >= 10_000:
        return f"{v / 1000:g}K"
    return f"{v:g}"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def line_chart(chart_id: str, title: str, unit: str, series: Sequence[Tuple[str, int, Dict[int, Decimal]]],
               refs: Sequence[Tuple[str, Decimal]] = (), subtitle: str = "") -> str:
    """series: [(nombre, slot 1..3, {epoch_min: valor})]; refs: [(etiqueta, valor)]."""
    pts = []
    step = 1
    for name, slot, vals in series:
        p, st = downsample_max(vals)
        step = max(step, st)
        pts.append((name, slot, p))
    allx = [x for _, _, p in pts for x, _ in p]
    if not allx:
        return f'<figure class="chart"><figcaption>{_esc(title)}</figcaption><p class="muted">Sin datos completos.</p></figure>'
    x0, x1 = min(allx), max(allx)
    if x1 == x0:
        x1 = x0 + 60
    ymax_data = max((y for _, _, p in pts for _, y in p), default=0.0)
    # agrupar referencias de igual valor; separar las que están fuera de escala
    grouped: Dict[float, List[str]] = {}
    for label, v in refs:
        grouped.setdefault(float(v), []).append(label)
    in_scale = {v: l for v, l in grouped.items() if ymax_data <= 0 or v <= ymax_data * OUT_OF_SCALE}
    off_scale = {v: l for v, l in grouped.items() if v not in in_scale}
    ymax = nice_max(max([ymax_data] + list(in_scale)) * 1.08)
    pw, ph = W - ML - MR, H - MT - MB

    def sx(x):
        return ML + (x - x0) / (x1 - x0) * pw

    def sy(y):
        return MT + ph - (y / ymax) * ph

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_esc(title)}" class="svgchart" '
             f'data-chart="{_esc(chart_id)}" data-x0="{x0}" data-x1="{x1}" data-ml="{ML}" data-pw="{pw}">']
    for i in range(6):
        yv = ymax * i / 5
        y = sy(yv)
        parts.append(f'<line class="grid" x1="{ML}" x2="{ML + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="axis" x="{ML - 6}" y="{y + 4:.1f}" text-anchor="end">{fmt_axis(yv)}</text>')
    span_h = (x1 - x0) / 3600
    tick_h = next((h for h in (1, 2, 3, 6, 12, 24, 48, 168) if span_h / h <= 8), 336)
    t = (x0 // (tick_h * 3600) + 1) * tick_h * 3600
    while t <= x1:
        x = sx(t)
        lab = datetime.fromtimestamp(t, timezone.utc).strftime("%d/%m %H:%M")
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{MT}" y2="{MT + ph}"/>')
        parts.append(f'<text class="axis" x="{x:.1f}" y="{H - MB + 16}" text-anchor="middle">{lab}</text>')
        t += tick_h * 3600
    parts.append(f'<text class="axis" x="{ML + pw / 2}" y="{H - 4}" text-anchor="middle">tiempo (UTC)</text>')
    parts.append(f'<text class="axis" x="12" y="{MT + ph / 2}" transform="rotate(-90 12 {MT + ph / 2})" '
                 f'text-anchor="middle">{_esc(unit)}</text>')
    # referencias: discontinuas, etiqueta neutra a la derecha
    last_y = -1e9
    for v in sorted(in_scale, reverse=True):
        y = sy(v)
        parts.append(f'<line class="ref" x1="{ML}" x2="{ML + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        ly = max(y - 2, last_y + 14)
        lines = _ref_lines(in_scale[v], v, unit)
        for i, t in enumerate(lines):
            parts.append(f'<text class="reflabel" x="{ML + pw + 6}" y="{ly + 12 * i:.1f}">{_esc(t)}</text>')
        last_y = ly + 12 * (len(lines) - 1)
    oy = MT + 10
    for v in sorted(off_scale):
        for t in ["↑ fuera de escala:"] + _ref_lines(off_scale[v], v, unit):
            parts.append(f'<text class="reflabel" x="{ML + pw + 6}" y="{oy:.1f}">{_esc(t)}</text>')
            oy += 12
        oy += 4
    data = {"unit": unit, "step": step, "series": []}
    for name, slot, p in pts:
        if not p:
            continue
        d = "M" + " L".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in p)
        parts.append(f'<path class="line s{slot}" d="{d}"><title>{_esc(name)}</title></path>')
        mx = max(p, key=lambda q: q[1])
        parts.append(f'<circle class="dot s{slot}" cx="{sx(mx[0]):.1f}" cy="{sy(mx[1]):.1f}" r="4"/>')
        data["series"].append({"name": name, "slot": slot, "x": [q[0] for q in p], "y": [round(q[1], 3) for q in p]})
    parts.append(f'<line class="cross" x1="0" x2="0" y1="{MT}" y2="{MT + ph}" visibility="hidden"/>')
    parts.append(f'<rect class="hit" x="{ML}" y="{MT}" width="{pw}" height="{ph}" fill="transparent"/>')
    parts.append("</svg>")
    legend = ""
    if len(data["series"]) >= 2:
        legend = '<div class="legend">' + "".join(
            f'<span><i class="sw s{s["slot"]}"></i>{_esc(s["name"])}</span>' for s in data["series"]) + "</div>"
    note = f'<p class="muted small">Agregado por máximo cada {step} min para conservar los picos.</p>' if step > 1 else ""
    return (f'<figure class="chart" id="{_esc(chart_id)}"><figcaption><b>{_esc(title)}</b>'
            f'{"<br><span class=muted>" + _esc(subtitle) + "</span>" if subtitle else ""}</figcaption>{legend}'
            + "".join(parts) + note
            + f'<script type="application/json" class="chartdata">{json.dumps(data)}</script>'
            + '<div class="tip" hidden></div></figure>')


def _ref_lines(labels: List[str], v: float, unit: str) -> List[str]:
    out = []
    head = "capacidad probada" if all(l.startswith("capacidad probada") for l in labels) else "máx. publicado/teórico"
    out.append(head)
    for l in labels[:3]:
        out.append(_short(l.replace("máximo publicado/teórico ", "").replace("capacidad probada ", ""), 30))
    if len(labels) > 3:
        out.append(f"+{len(labels) - 3} más")
    out.append(f"= {fmt_es_short(v)} {unit}")
    return out


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_es_short(v: float) -> str:
    s = f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def bar_chart(chart_id: str, title: str, unit: str, bars: Sequence[Tuple[str, Decimal]],
              ref: Optional[Tuple[str, Decimal]] = None, subtitle: str = "") -> str:
    """Barras horizontales (una serie) con referencia discontinua opcional."""
    if not bars:
        return ""
    bh, gap = 20, 10
    height = MT + len(bars) * (bh + gap) + 30
    vmax = nice_max(max([float(v) for _, v in bars] + ([float(ref[1])] if ref else [])) * 1.1)
    lw = 120
    pw = W - lw - MR

    def sx(v):
        return lw + float(v) / vmax * pw

    parts = [f'<svg viewBox="0 0 {W} {height}" role="img" aria-label="{_esc(title)}" class="svgchart">']
    for i in range(6):
        x = sx(vmax * i / 5)
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{MT - 6}" y2="{height - 24}"/>')
        parts.append(f'<text class="axis" x="{x:.1f}" y="{height - 8}" text-anchor="middle">{fmt_axis(vmax * i / 5)}</text>')
    for i, (name, v) in enumerate(bars):
        y = MT + i * (bh + gap)
        w = max(sx(v) - lw, 0.5)
        r = min(4, w / 2)
        # extremo de datos redondeado 4 px, base recta
        parts.append(f'<path class="bar s1" d="M{lw},{y} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{bh - 2 * r} '
                     f'a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z"><title>{_esc(name)}: {v} {_esc(unit)}</title></path>')
        parts.append(f'<text class="axis" x="{lw - 6}" y="{y + 14}" text-anchor="end">{_esc(name)}</text>')
        parts.append(f'<text class="val" x="{lw + w + 6:.1f}" y="{y + 14}">{fmt_es_short(float(v))}</text>')
    if ref:
        x = sx(ref[1])
        parts.append(f'<line class="ref" x1="{x:.1f}" x2="{x:.1f}" y1="{MT - 6}" y2="{height - 24}"/>')
        parts.append(f'<text class="reflabel" x="{x + 4:.1f}" y="{MT - 8}">{_esc(ref[0])}: {fmt_axis(float(ref[1]))}</text>')
    parts.append("</svg>")
    return (f'<figure class="chart" id="{_esc(chart_id)}"><figcaption><b>{_esc(title)}</b>'
            f'{"<br><span class=muted>" + _esc(subtitle) + "</span>" if subtitle else ""}</figcaption>'
            + "".join(parts) + "</figure>")


CHART_CSS = """
.svgchart{width:100%;height:auto;display:block;overflow:visible}
.svgchart .grid{stroke:var(--grid);stroke-width:1}
.svgchart .axis,.svgchart .val{fill:var(--text-secondary);font-size:11px}
.svgchart .val{fill:var(--text-primary)}
.svgchart .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.svgchart .line.s1{stroke:var(--series-1)}.svgchart .line.s2{stroke:var(--series-2)}.svgchart .line.s3{stroke:var(--series-3)}
.svgchart .dot{stroke:var(--surface-1);stroke-width:2}
.svgchart .dot.s1,.svgchart .bar.s1{fill:var(--series-1)}.svgchart .dot.s2{fill:var(--series-2)}.svgchart .dot.s3{fill:var(--series-3)}
.svgchart .ref{stroke:var(--text-secondary);stroke-width:1.5;stroke-dasharray:6 4}
.svgchart .reflabel{fill:var(--text-secondary);font-size:11px}
.svgchart .cross{stroke:var(--text-secondary);stroke-width:1}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--text-secondary);margin:4px 0}
.legend .sw{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.sw.s1{background:var(--series-1)}.sw.s2{background:var(--series-2)}.sw.s3{background:var(--series-3)}
figure.chart{position:relative;margin:16px 0 24px}
.tip{position:absolute;pointer-events:none;background:var(--surface-2);color:var(--text-primary);border:1px solid var(--grid);
 border-radius:6px;padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.15);white-space:nowrap}
"""

CHART_JS = """
document.querySelectorAll('figure.chart').forEach(function(fig){
  var svg=fig.querySelector('svg[data-chart]'); if(!svg) return;
  var data=JSON.parse(fig.querySelector('script.chartdata').textContent);
  var tip=fig.querySelector('.tip'), cross=svg.querySelector('.cross'), hit=svg.querySelector('.hit');
  var x0=+svg.dataset.x0,x1=+svg.dataset.x1,ml=+svg.dataset.ml,pw=+svg.dataset.pw;
  function fmt(v){return v.toLocaleString('es-ES',{maximumFractionDigits:2});}
  hit.addEventListener('mousemove',function(ev){
    var pt=svg.createSVGPoint();pt.x=ev.clientX;pt.y=ev.clientY;var p=pt.matrixTransform(svg.getScreenCTM().inverse());
    var t=x0+(p.x-ml)/pw*(x1-x0);var rows=[],best=null;
    data.series.forEach(function(s){var i=0,bd=1e18;for(var k=0;k<s.x.length;k++){var d=Math.abs(s.x[k]-t);if(d<bd){bd=d;i=k;}}
      best=s.x[i];rows.push('<span class="sw s'+s.slot+'" style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px"></span>'+s.name+': <b>'+fmt(s.y[i])+'</b> '+data.unit);});
    var cx=ml+(best-x0)/(x1-x0)*pw;cross.setAttribute('x1',cx);cross.setAttribute('x2',cx);cross.setAttribute('visibility','visible');
    var d=new Date(best*1000).toISOString().replace('T',' ').slice(0,16)+' UTC'+(data.step>1?' (máx. '+data.step+' min)':'');
    tip.innerHTML=d+'<br>'+rows.join('<br>');tip.hidden=false;
    var r=fig.getBoundingClientRect();var left=ev.clientX-r.left+12;if(left>r.width-220)left=ev.clientX-r.left-230;
    tip.style.left=left+'px';tip.style.top=(ev.clientY-r.top+12)+'px';});
  hit.addEventListener('mouseleave',function(){tip.hidden=true;cross.setAttribute('visibility','hidden');});
});
"""
