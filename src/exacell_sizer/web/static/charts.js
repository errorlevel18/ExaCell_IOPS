/* Gráficos SVG sin dependencias (funcionan sin Internet).
   Reglas: líneas de 2 px, máx. 3 series (paleta validada), rejilla recesiva, referencias como
   línea DISCONTINUA con etiqueta en tinta de texto ("máx. publicado/teórico"), tooltip con cruceta. */
(function (global) {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const nf = (d) => new Intl.NumberFormat('es-ES', { maximumFractionDigits: d });
  const fmt = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—' : nf(d).format(v);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const e = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * e >= v) return m * e;
    return 10 * e;
  }
  function axisFmt(v) {
    if (v >= 1e6) return nf(1).format(v / 1e6) + ' M';
    if (v >= 1e4) return nf(1).format(v / 1e3) + ' K';
    return nf(2).format(v);
  }
  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function text(parent, x, y, s, cls, anchor) {
    const t = el('text', { x, y, class: cls || 'axis', 'text-anchor': anchor || 'start' }, parent);
    t.textContent = s;
    return t;
  }
  function timeLabel(t, spanH) {
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const hm = `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
    return spanH > 30 ? `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)} ${hm}` : hm;
  }

  /* Agrupa referencias de igual valor y separa las fuera de escala (> 5x el pico observado). */
  function prepareRefs(refs, ymaxData) {
    const groups = new Map();
    (refs || []).forEach((r) => { const k = +r.value; if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); });
    const inScale = [], offScale = [];
    groups.forEach((list, v) => ((ymaxData <= 0 || v <= ymaxData * 5) ? inScale : offScale).push({ value: v, list }));
    return { inScale, offScale };
  }
  function refLines(group, unit) {
    const head = group.list.every((r) => /probada/.test(r.label)) ? 'capacidad probada' : 'máx. publicado/teórico';
    const lines = [head];
    group.list.slice(0, 3).forEach((r) => lines.push(r.short || r.label.replace(/^.*?·\s*/, '')));
    if (group.list.length > 3) lines.push(`+${group.list.length - 3} más`);
    lines.push(`= ${fmt(group.value, 0)} ${unit}`);
    return lines.map((l) => (l.length > 34 ? l.slice(0, 33) + '…' : l));
  }

  /* Línea temporal. opts: {series:[{name,slot,points:[[t,v],...]}], refs:[{label,value}], unit, height, step} */
  function lineChart(container, opts) {
    container.innerHTML = '';
    container.classList.add('chart');
    const W = 940, H = opts.height || 300, ML = 64, MR = opts.refs && opts.refs.length ? 210 : 24, MT = 14, MB = 36;
    const series = (opts.series || []).filter((s) => s.points && s.points.length);
    if (!series.length) { container.innerHTML = '<div class="empty">Sin minutos completos para esta medida.</div>'; return; }
    if (series.length >= 2) {
      const lg = document.createElement('div');
      lg.className = 'legend';
      lg.innerHTML = series.map((s) => `<span><i class="sw s${s.slot}"></i>${esc(s.name)}</span>`).join('')
        + ((opts.refs || []).length ? '<span><i class="swref"></i>máx. publicado/teórico (no es capacidad garantizada)</span>' : '');
      container.appendChild(lg);
    } else if ((opts.refs || []).length) {
      const lg = document.createElement('div');
      lg.className = 'legend';
      lg.innerHTML = '<span><i class="swref"></i>máx. publicado/teórico (no es capacidad garantizada)</span>';
      container.appendChild(lg);
    }
    let x0 = Infinity, x1 = -Infinity, ymaxData = 0;
    series.forEach((s) => s.points.forEach(([t, v]) => { x0 = Math.min(x0, t); x1 = Math.max(x1, t); ymaxData = Math.max(ymaxData, v); }));
    if (x1 === x0) x1 = x0 + 60;
    const { inScale, offScale } = prepareRefs(opts.refs, ymaxData);
    const limits = (opts.limits || []);
    const ymax = niceMax(Math.max(ymaxData, ...inScale.map((g) => g.value), ...limits.map((l) => l.value)) * 1.08);
    const pw = W - ML - MR, ph = H - MT - MB;
    const sx = (t) => ML + (t - x0) / (x1 - x0) * pw;
    const sy = (v) => MT + ph - (v / ymax) * ph;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.title || 'gráfico' }, container);
    for (let i = 0; i <= 5; i++) {
      const v = ymax * i / 5, y = sy(v);
      el('line', { class: 'grid', x1: ML, x2: ML + pw, y1: y, y2: y }, svg);
      text(svg, ML - 6, y + 4, axisFmt(v), 'axis', 'end');
    }
    const spanH = (x1 - x0) / 3600;
    const tick = [1, 2, 3, 4, 6, 12, 24, 48, 168].find((h) => spanH / h <= 8) || 336;
    for (let t = Math.ceil(x0 / (tick * 3600)) * tick * 3600; t <= x1; t += tick * 3600) {
      const x = sx(t);
      el('line', { class: 'grid', x1: x, x2: x, y1: MT, y2: MT + ph }, svg);
      text(svg, x, H - MB + 16, timeLabel(t, spanH), 'axis', 'middle');
    }
    text(svg, ML + pw / 2, H - 3, 'tiempo (UTC)', 'axis', 'middle');
    text(svg, 12, MT + ph / 2, opts.unit || '', 'axis', 'middle').setAttribute('transform', `rotate(-90 12 ${MT + ph / 2})`);
    // límites (p. ej. utilización máxima admitida) y referencias
    limits.forEach((l) => {
      const y = sy(l.value);
      el('line', { class: 'ref limit', x1: ML, x2: ML + pw, y1: y, y2: y }, svg);
      text(svg, ML + pw + 6, y + 4, l.label, 'reflabel');
    });
    let lastY = -1e9;
    inScale.sort((a, b) => b.value - a.value).forEach((g) => {
      const y = sy(g.value);
      el('line', { class: 'ref', x1: ML, x2: ML + pw, y1: y, y2: y }, svg);
      let ly = Math.max(y - 2, lastY + 14);
      refLines(g, opts.unit).forEach((l, i) => text(svg, ML + pw + 6, ly + 12 * i, l, 'reflabel'));
      lastY = ly + 12 * (refLines(g, opts.unit).length - 1);
    });
    let oy = MT + 10;
    offScale.sort((a, b) => a.value - b.value).forEach((g) => {
      ['↑ fuera de escala:'].concat(refLines(g, opts.unit)).forEach((l) => { text(svg, ML + pw + 6, oy, l, 'reflabel'); oy += 12; });
      oy += 4;
    });
    series.forEach((s, idx) => {
      if (idx === 0 && series.length === 1) {
        const area = 'M' + s.points.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(' L')
          + ` L${sx(s.points[s.points.length - 1][0]).toFixed(1)},${sy(0)} L${sx(s.points[0][0]).toFixed(1)},${sy(0)} Z`;
        el('path', { d: area, class: 'area1' }, svg);
      }
      el('path', { d: 'M' + s.points.map(([t, v]) => `${sx(t).toFixed(1)},${sy(v).toFixed(1)}`).join(' L'), class: `line s${s.slot}` }, svg);
      const mx = s.points.reduce((a, b) => (b[1] > a[1] ? b : a));
      el('circle', { cx: sx(mx[0]), cy: sy(mx[1]), r: 4.5, class: `dot s${s.slot}` }, svg);
    });
    const cross = el('line', { class: 'cross', x1: 0, x2: 0, y1: MT, y2: MT + ph, visibility: 'hidden' }, svg);
    const hit = el('rect', { x: ML, y: MT, width: pw, height: ph, fill: 'transparent' }, svg);
    const tip = document.createElement('div');
    tip.className = 'tip'; tip.hidden = true;
    container.appendChild(tip);
    const nearest = (pts, t) => {
      let lo = 0, hi = pts.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (pts[mid][0] < t) lo = mid; else hi = mid; }
      return Math.abs(pts[lo][0] - t) <= Math.abs(pts[hi][0] - t) ? pts[lo] : pts[hi];
    };
    hit.addEventListener('mousemove', (ev) => {
      const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      const t = x0 + (p.x - ML) / pw * (x1 - x0);
      const rows = []; let tt = null;
      series.forEach((s) => { const q = nearest(s.points, t); tt = q[0]; rows.push(`<i class="sw s${s.slot}" style="display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px"></i>${esc(s.name)}: <b>${fmt(q[1])}</b> ${esc(opts.unit || '')}`); });
      cross.setAttribute('x1', sx(tt)); cross.setAttribute('x2', sx(tt)); cross.setAttribute('visibility', 'visible');
      const d = new Date(tt * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC' + (opts.step > 1 ? ` · máx. de ${opts.step} min` : '');
      tip.innerHTML = `<div class="muted tiny">${d}</div>` + rows.join('<br>');
      tip.hidden = false;
      const r = container.getBoundingClientRect();
      let left = ev.clientX - r.left + 14;
      if (left > r.width - 260) left = ev.clientX - r.left - 270;
      tip.style.left = Math.max(0, left) + 'px'; tip.style.top = (ev.clientY - r.top + 10) + 'px';
    });
    hit.addEventListener('mouseleave', () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); });
    if (opts.step > 1) {
      const n = document.createElement('div');
      n.className = 'muted tiny';
      n.textContent = `Cada punto es el máximo de ${opts.step} minutos (para no ocultar picos).`;
      container.appendChild(n);
    }
  }

  /* Barras horizontales de una serie. opts: {bars:[{label,value,note}], unit, refs:[{label,value}], max, fmtValue} */
  function barChart(container, opts) {
    container.innerHTML = '';
    container.classList.add('chart');
    const bars = opts.bars || [];
    if (!bars.length) { container.innerHTML = '<div class="empty">Sin datos.</div>'; return; }
    const W = 940, bh = 20, gap = 12, LW = opts.labelWidth || 190, MR = 90, MT = 26;
    const H = MT + bars.length * (bh + gap) + 26;
    const vmax = opts.max || niceMax(Math.max(...bars.map((b) => b.value || 0), ...(opts.refs || []).map((r) => r.value)) * 1.08);
    const pw = W - LW - MR;
    const sx = (v) => LW + Math.min(v, vmax) / vmax * pw;
    const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.title || 'barras' }, container);
    for (let i = 0; i <= 5; i++) {
      const x = sx(vmax * i / 5);
      el('line', { class: 'grid', x1: x, x2: x, y1: MT - 6, y2: H - 22 }, svg);
      text(svg, x, H - 6, (opts.axisFmt || axisFmt)(vmax * i / 5), 'axis', 'middle');
    }
    const vf = opts.fmtValue || ((v) => fmt(v, 1));
    bars.forEach((b, i) => {
      const y = MT + i * (bh + gap);
      const w = Math.max(sx(b.value || 0) - LW, 0.5), r = Math.min(4, w / 2);
      const path = el('path', { class: 'bar1', d: `M${LW},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${bh - 2 * r} a${r},${r} 0 0 1 -${r},${r} h-${w - r} z` }, svg);
      const ttl = el('title', {}, path); ttl.textContent = `${b.label}: ${vf(b.value)} ${opts.unit || ''}`;
      text(svg, LW - 8, y + 14, b.label.length > 30 ? b.label.slice(0, 29) + '…' : b.label, 'axis', 'end');
      const lbl = vf(b.value) + (b.note ? ' ' + b.note : '');
      if (LW + w + 8 + lbl.length * 6.6 > W - 4) text(svg, LW + w - 8, y + 14, lbl, 'valin', 'end');
      else text(svg, LW + w + 6, y + 14, lbl, 'val');
    });
    (opts.refs || []).forEach((r, i) => {
      const x = sx(r.value);
      el('line', { class: 'ref' + (r.kind === 'limit' ? ' limit' : ''), x1: x, x2: x, y1: MT - 8, y2: H - 22 }, svg);
      text(svg, x + 4, 10 + i * 12, r.label, 'reflabel');
    });
  }

  /* Mapa de cobertura: rows x cols con valor 0..1, o estado n/a / no disponible. */
  function heatmap(container, opts) {
    const cols = opts.cols, rows = opts.rows;
    const bins = [[0.999, 6], [0.99, 5], [0.95, 4], [0.8, 3], [0.5, 2], [0, 1]];
    const color = (v) => { for (const [th, k] of bins) if (v >= th) return k; return 1; };
    let h = `<div class="heat" style="grid-template-columns:minmax(150px,1.4fr) repeat(${cols.length},minmax(58px,1fr))"><div></div>`
      + cols.map((c) => `<div class="h" title="${esc(c)}">${esc(c)}</div>`).join('');
    rows.forEach((r) => {
      h += `<div class="h" title="${esc(r.label)}">${esc(r.label)}</div>`;
      cols.forEach((c) => {
        const cell = opts.cell(r, c);
        if (!cell || cell.status === 'NOT_APPLICABLE') h += `<div class="c na" title="${esc(r.label)} · ${esc(c)}: no aplica">n/a</div>`;
        else if (cell.status === 'UNAVAILABLE') h += `<div class="c un" title="${esc(cell.reason || '')}">✖ no disp.</div>`;
        else {
          const k = color(cell.value);
          h += `<div class="c k${k}" title="${esc(r.label)} · ${esc(c)}: ${fmt(cell.value * 100, 2)} % (${cell.complete}/${cell.expected} min)">${fmt(cell.value * 100, 1)}%</div>`;
        }
      });
    });
    h += '</div><div class="legend-seq" style="margin-top:8px">Cobertura (minutos completos):'
      + [['< 50 %', 1], ['50–80 %', 2], ['80–95 %', 3], ['95–99 %', 4], ['99–99,9 %', 5], ['≥ 99,9 %', 6]].map(([l, k]) => `<i style="background:var(--seq-${k})"></i>${l}`).join(' ')
      + ' <i class="na" style="background:repeating-linear-gradient(45deg,var(--surface-3),var(--surface-3) 3px,var(--surface-1) 3px,var(--surface-1) 6px)"></i>no aplica</div>';
    container.innerHTML = h;
  }

  global.Charts = { lineChart, barChart, heatmap, fmt, esc, axisFmt };
})(window);
