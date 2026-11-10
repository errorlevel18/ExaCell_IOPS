/* ExaCell Sizer — aplicación web local (sin dependencias, funciona sin Internet). */
(function () {
  'use strict';
  const { lineChart, barChart, heatmap, fmt, esc } = window.Charts;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const view = $('#view');
  const state = { catalog: null, info: null, scenario: null, scenarioSel: { source: '', dest: '' } };

  const VERDICT = {
    'Compatible bajo las referencias y supuestos indicados': ['good', '✔'],
    'No compatible con los límites evaluados': ['critical', '✖'],
    'Datos insuficientes para concluir': ['warning', '?'],
  };
  const MEASURE_TABS = [
    ['flash_read_iops_small', 'Flash · lecturas IOPS'], ['flash_write_iops_small', 'Flash · escrituras IOPS'],
    ['flash_read_mbps', 'Flash · MB/s lectura'], ['flash_write_mbps', 'Flash · MB/s escritura'],
    ['hdd_iops_small', 'HDD · IOPS'], ['hdd_mbps', 'HDD · MB/s'], ['network_mbps', 'Red · MB/s'],
    ['cell_cpu_pct', 'CPU celda %'], ['flash_read_latency_us', 'Latencia lectura flash'],
    ['hdd_read_latency_us', 'Latencia lectura HDD'], ['hdd_util_pct', 'Utilización HDD %'],
    ['flashcache_read_iops', 'Flash Cache (info)'],
  ];
  const ROLE = { origen: 'Origen', destino: 'Destino actual', otro: 'Otro' };

  // ------------------------------------------------------------------ utilidades
  async function api(path, opts = {}) {
    const r = await fetch(path, opts);
    const ct = r.headers.get('content-type') || '';
    const body = ct.includes('json') ? await r.json() : await r.text();
    if (!r.ok) throw new Error((body && body.error) || `HTTP ${r.status}`);
    return body;
  }
  const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
  function toast(msg, err) {
    const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : ''); t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
  }
  async function pollJob(id, onLog) {
    for (;;) {
      const j = await api(`/api/jobs/${id}`);
      onLog && onLog(j);
      if (j.status !== 'running') return j;
      await new Promise((r) => setTimeout(r, 700));
    }
  }
  const num = (v, d = 2) => fmt(v === null || v === undefined || v === '' ? null : +v, d);
  const pct = (v, d = 1) => (v === null || v === undefined || v === '' || v === 'None') ? '—' : fmt(+v * 100, d) + ' %';
  const verdictHtml = (v, big) => { const [c, i] = VERDICT[v] || ['warning', '?']; return `<span class="verdict ${c}"${big ? ' style="font-size:15px"' : ''}><span class="ic" aria-hidden="true">${i}</span>${esc(v)}</span>`; };
  const statusChip = (s) => {
    const cls = { COMPLETE: 'good', OK: 'good', INCOMPLETE: 'critical', MISMATCH: 'critical', MISSING: 'warning', PREVALIDATION_FAILED: 'critical', INTERRUPTED: 'critical' }[s] || 'info';
    const txt = { COMPLETE: 'Completa', INCOMPLETE: 'Incompleta', OK: 'Íntegra', MISMATCH: 'Alterada', MISSING: 'Sin sumas' }[s] || s;
    return `<span class="chip ${cls}"><span class="dot"></span>${esc(txt)}</span>`;
  };
  const kpi = (label, value, sub) => `<div class="card kpi"><span class="label">${esc(label)}</span><span class="value">${value}</span>${sub ? `<span class="sub">${sub}</span>` : ''}</div>`;
  const table = (headers, rows, numeric = []) => `<div class="tablewrap"><table><thead><tr>${headers.map((h, i) => `<th class="${numeric.includes(i) ? 'n' : ''}">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map((r) => `<tr${r._attrs || ''}>${r.map((c, i) => `<td class="${numeric.includes(i) ? 'n' : ''}">${c}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${headers.length}" class="muted">Sin datos</td></tr>`}</tbody></table></div>`;
  const head = (title, sub, crumbs, actions) => `<div class="page-head"><div>${crumbs ? `<div class="crumbs">${crumbs}</div>` : ''}<h1>${title}</h1>${sub ? `<div class="muted">${sub}</div>` : ''}</div><div class="btn-row">${actions || ''}</div></div>`;
  async function getCatalog() { if (!state.catalog) state.catalog = await api('/api/catalog'); return state.catalog; }
  const evidenceBadge = (e) => /probado/.test(e) ? `<span class="badge pub">probado</span>` : /aproximación/.test(e) ? `<span class="badge appr">publicado · aproximación</span>` : e === 'publicado' ? `<span class="badge pub">publicado</span>` : e === 'derivado' ? `<span class="badge der">derivado</span>` : `<span class="badge">${esc(e || '—')}</span>`;

  // ------------------------------------------------------------------ enrutado
  const routes = [
    [/^#?\/?$/, 'dashboard', pageDashboard],
    [/^#\/recolector$/, 'collector', pageCollector],
    [/^#\/capturas$/, 'captures', pageCaptures],
    [/^#\/capturas\/(\d+)$/, 'captures', pageCapture],
    [/^#\/dimensionamiento$/, 'sizing', pageSizing],
    [/^#\/resultados\/([A-Za-z0-9_]+)$/, 'sizing', pageRun],
    [/^#\/catalogo$/, 'catalog', pageCatalog],
    [/^#\/guia(?:\/([a-z]+))?$/, 'guide', pageGuide],
  ];
  async function router() {
    const h = location.hash || '#/';
    for (const [re, name, fn] of routes) {
      const m = h.match(re);
      if (m) {
        $$('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.route === name));
        $('#sidebar').classList.remove('open');
        view.innerHTML = '<div class="loading">Cargando… <span class="muted small">(la primera vez que se abre una captura grande, su análisis puede tardar unos segundos)</span></div>';
        try { await fn(...m.slice(1)); } catch (e) { view.innerHTML = `<div class="callout crit"><b>Error:</b> ${esc(e.message)}</div>`; }
        view.focus({ preventScroll: true });
        window.scrollTo(0, 0);
        return;
      }
    }
    location.hash = '#/';
  }

  // ------------------------------------------------------------------ Panel
  async function pageDashboard() {
    const [caps, runs] = await Promise.all([api('/api/captures'), api('/api/runs')]);
    const complete = caps.filter((c) => c.import_status === 'COMPLETE').length;
    const real = caps.filter((c) => !c.simulated).length;
    const steps = [
      ['Descargar el recolector', 'Genere el comando y entregue el script al cliente para ejecutarlo en un dbnode del origen (24 h o más).', '#/recolector', 'Abrir recolector', false],
      ['Subir la captura', 'Suba el .tar.gz/.zip que entrega el cliente. Se valida integridad, bloques, huecos y unidades.', '#/capturas', 'Subir captura', caps.length > 0],
      ['Revisar carga y cobertura', 'Compruebe picos, minutos completos por celda, incidencias y avisos de calendario.', caps.length ? `#/capturas/${caps[0].id}` : '#/capturas', 'Ver captura', caps.length > 0],
      ['Dimensionar candidatos', 'Defina crecimiento, reserva, N-1 y configuraciones Exadata candidatas. Obtenga el dictamen.', '#/dimensionamiento', 'Dimensionar', runs.length > 0],
    ];
    view.innerHTML = head('Panel', '¿Cuántas celdas de almacenamiento necesitaría el Exadata destino para la carga real del origen?')
      + `<div class="grid g4">${kpi('Capturas importadas', caps.length, `${real} reales · ${caps.length - real} simuladas`)}${kpi('Capturas completas', complete, caps.length - complete ? `${caps.length - complete} incompletas` : 'todas íntegras')}${kpi('Dimensionamientos', runs.length, runs.length ? `último: ${esc(runs[0].generated_utc)}` : 'ninguno todavía')}${kpi('Modelos en catálogo', '6', 'X6-2 · X7-2 · X8M-2 · X9M-2 · X10M · X11M')}</div>`
      + `<div class="card section"><h2>Cómo empezar</h2><div class="steps">${steps.map((s, i) => `<div class="step ${s[4] ? 'done' : ''}"><span class="n">${s[4] ? '✓' : i + 1}</span><span class="t">${s[0]}</span><span class="muted small">${s[1]}</span><a class="btn ghost small" href="${s[2]}" style="align-self:flex-start;margin-top:auto">${s[3]} →</a></div>`).join('')}</div>`
      + (caps.length ? '' : `<div class="callout info" style="margin-top:14px">¿Sin capturas reales todavía? En <a href="#/capturas">Capturas</a> puede generar una captura <b>simulada</b> de demostración para probar toda la aplicación.</div>`) + '</div>'
      + `<div class="grid g2 section"><div class="card"><h2>Últimas capturas</h2>${table(['Captura', 'Rol', 'Estado', 'Duración', 'Celdas'], caps.slice(0, 6).map((c) => Object.assign([`<a href="#/capturas/${c.id}">${esc(c.label)}</a>${c.simulated ? ' <span class="badge">simulada</span>' : ''}`, ROLE[c.role] || c.role, statusChip(c.import_status), num(c.hours, 1) + ' h', c.cells], {})), [3, 4])}</div>`
      + `<div class="card"><h2>Últimos dimensionamientos</h2>${runs.length ? runs.slice(0, 5).map((r) => `<div style="padding:8px 0;border-bottom:1px solid var(--border)"><a href="#/resultados/${r.run_id}"><b>${esc(r.scenario_name || r.run_id)}</b></a> <span class="muted tiny">${esc(r.generated_utc)} · ${esc(r.source)}${r.dest ? ' + ' + esc(r.dest) : ''}</span><div class="btn-row" style="margin-top:6px">${r.verdicts.map((v) => `<span class="chip ${(VERDICT[v.verdict] || ['warning'])[0]}"><span class="dot"></span>${esc(v.id)}${v.n_recommended ? ' · ' + v.n_recommended + ' celdas' : ''}</span>`).join('')}</div></div>`).join('') : '<div class="empty">Todavía no hay dimensionamientos.</div>'}</div></div>`
      + `<div class="callout warn section"><b>Prudencia:</b> las cifras de datasheet son máximos comerciales, no capacidad garantizada. Un resultado «compatible» es teórico; la aprobación operativa requiere capacidad probada a la latencia objetivo, captura ≥ 24 h y periodos críticos confirmados.</div>`;
  }

  // ------------------------------------------------------------------ Recolector
  async function pageCollector() {
    const c = await api('/api/collector');
    view.innerHTML = head('Recolector para el dbnode', `exacell_collect.sh v${esc(c.version)} · solo lectura · SHA-256 <span class="mono">${esc(c.sha256.slice(0, 16))}…</span>`, null,
      `<a class="btn" href="/api/collector/download">⬇ Descargar script</a>`)
      + `<div class="grid g2"><div class="card"><h2>1 · Generar el comando</h2><div class="form-grid">
        <label class="f">Directorio local (existente en el dbnode)<input id="cDir" value="/home/oracle/capturas"></label>
        <label class="f">Archivo cell_group<input id="cGroup" value="/home/oracle/cell_group"></label>
        <label class="f">Usuario remoto<select id="cUser"><option>celladmin</option><option>root</option></select></label>
        <label class="f">Inicio (fecha y hora local)<input id="cStart" type="datetime-local" step="3600"></label>
        <label class="f">Zona horaria<select id="cTz">${['+00:00', '+01:00', '+02:00', '-03:00', '-04:00', '-05:00', '-06:00', '+05:30', '+08:00'].map((z) => `<option ${z === '+01:00' ? 'selected' : ''}>${z}</option>`).join('')}</select></label>
        <label class="f">Duración<select id="cDur"><option value="24" selected>24 horas</option><option value="48">48 horas</option><option value="72">72 horas</option><option value="168">7 días</option></select></label>
        <label class="f">Celdas en paralelo<input id="cPar" type="number" min="1" max="18" value="4"></label></div>
        <h3>Prueba sin contactar celdas (simulación)</h3><div class="cmd"><pre id="cmdDry"></pre><button class="btn ghost small" data-copy="cmdDry">Copiar</button></div>
        <h3>Ejecución real</h3><div class="cmd"><pre id="cmdRun"></pre><button class="btn ghost small" data-copy="cmdRun">Copiar</button></div>
        <h3>Empaquetar para entregar</h3><div class="cmd"><pre id="cmdTar"></pre><button class="btn ghost small" data-copy="cmdTar">Copiar</button></div>
        <div class="callout info small">Después, suba el <span class="mono">.tar.gz</span> en <a href="#/capturas">Capturas</a>. No modifique la carpeta de captura: sus sumas SHA-256 se verifican al importar.</div></div>
      <div class="card"><h2>2 · Qué hace y qué garantiza</h2><ul class="small">
        <li><b>Prevalida todas las celdas</b> con <span class="mono">dcli</span> y SSH antes de recoger nada; si alguna falla no recoge histórico (código 2).</li>
        <li>Selecciona métricas del <b>catálogo de cada celda</b> por pareja exacta (ámbito, nombre); solo celda, discos, flash y red. Nunca DB, PDB ni IORM.</li>
        <li>Recoge en <b>bloques horarios</b> con <span class="mono">collectionTime NOT &lt; inicio AND collectionTime &lt; fin</span> y <span class="mono">name = '…'</span> explícito.</li>
        <li>Toda salida se redirige <b>en el dbnode</b>. En las celdas solo se ejecuta <span class="mono">cellcli -e "LIST …"</span>: no crea archivos, no copia scripts, no cambia configuración, claves, IORM, retención ni servicios.</li>
        <li>Un bloque vacío, error CellCLI o corte de red marca la captura <b>INCOMPLETA</b>, nunca carga cero.</li></ul>
        ${table(['Código', 'Estado', 'Significado'], [['0', 'COMPLETE', 'Todos los bloques OK'], ['1', 'USAGE_ERROR', 'Parámetros inválidos (no contacta celdas)'], ['2', 'PREVALIDATION_FAILED', 'Alguna celda no superó la prevalidación'], ['3', 'INCOMPLETE', 'Bloques con error, vacíos o interrumpidos'], ['4', 'LOCAL_ERROR', 'Problema en el dbnode'], ['130', 'INTERRUPTED', 'Interrumpido por señal']].map((r) => r.map(esc)))}
        <div class="callout warn small">La sintaxis de CellCLI se ha validado con un emulador; pruebe primero con <span class="mono">-n</span> y una captura corta en la versión del cliente.</div></div></div>
      <div class="card section"><details><summary><b>Ver el script completo</b> <span class="muted small">(${c.script.split('\n').length} líneas)</span></summary><pre class="code" style="margin-top:10px">${esc(c.script)}</pre></details></div>`;
    const d = new Date(); d.setUTCDate(d.getUTCDate() - 1); d.setUTCHours(0, 0, 0, 0);
    $('#cStart').value = d.toISOString().slice(0, 16);
    const upd = () => {
      const st = $('#cStart').value, tz = $('#cTz').value, dur = +$('#cDur').value;
      if (!st) return;
      const base = new Date(st + ':00Z');
      const end = new Date(base.getTime() + dur * 3600e3).toISOString().slice(0, 19);
      const s = `${st}:00${tz}`, e = `${end}${tz}`;
      const args = `-d ${$('#cDir').value} -g ${$('#cGroup').value} -u ${$('#cUser').value} -s ${s} -e ${e} -p ${$('#cPar').value}`;
      $('#cmdDry').textContent = `./exacell_collect.sh ${args} -n`;
      $('#cmdRun').textContent = `./exacell_collect.sh ${args}`;
      $('#cmdTar').textContent = `cd ${$('#cDir').value} && tar czf exacell_captura.tar.gz exacell_capture_*`;
    };
    $$('#view input, #view select').forEach((i) => i.addEventListener('input', upd));
    upd();
    $$('[data-copy]').forEach((b) => b.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText($('#' + b.dataset.copy).textContent); toast('Copiado al portapapeles'); } catch { toast('No se pudo copiar', true); }
    }));
  }

  // ------------------------------------------------------------------ Capturas
  async function pageCaptures() {
    const caps = await api('/api/captures');
    view.innerHTML = head('Capturas', 'Suba la captura del cliente o genere una simulada para probar.')
      + `<div class="grid g2"><div class="card"><h2>⇪ Subir captura</h2>
        <div class="drop" id="drop"><div class="big">⇪</div><div><b>Arrastre aquí</b> el archivo <span class="mono">.tar.gz</span>, <span class="mono">.tgz</span>, <span class="mono">.tar</span> o <span class="mono">.zip</span></div><div class="muted small">o <a href="#" id="pick">seleccione un archivo</a></div><input type="file" id="file" accept=".gz,.tgz,.tar,.zip" hidden><div id="fileName" class="small" style="margin-top:8px"></div></div>
        <div class="form-grid" style="margin-top:12px"><label class="f">Etiqueta<input id="upLabel" placeholder="p. ej. origen_erp"></label><label class="f">Rol<select id="upRole"><option value="origen">Origen</option><option value="destino">Destino actual</option><option value="otro">Otro</option></select></label></div>
        <div class="btn-row" style="margin-top:12px"><button class="btn" id="upBtn" disabled>Subir e importar</button></div>
        <div id="upStatus" style="margin-top:12px"></div></div>
      <div class="card"><h2>⚗ Captura simulada de demostración</h2><p class="muted small">Genera datos <b>sintéticos</b> con el mismo formato que el recolector, para probar la aplicación sin Exadata. No representa ningún sistema real.</p>
        <div class="form-grid"><label class="f">Etiqueta<input id="smLabel" value="demo_origen"></label><label class="f">Rol<select id="smRole"><option value="origen">Origen</option><option value="destino">Destino actual</option></select></label>
        <label class="f">Celdas<input id="smCells" type="number" min="1" max="18" value="3"></label><label class="f">Horas<input id="smHours" type="number" min="1" max="72" value="24"></label>
        <label class="f">HDD por celda<input id="smHdd" type="number" min="0" max="12" value="12"></label><label class="f">Flash por celda<input id="smFlash" type="number" min="1" max="8" value="4"></label>
        <label class="f">Factor de carga<input id="smLoad" type="number" step="0.1" min="0.1" value="2"></label></div>
        <div class="btn-row" style="margin-top:12px"><button class="btn ghost" id="smBtn">Generar e importar</button></div><div id="smStatus" style="margin-top:12px"></div></div></div>
      <div class="card section"><h2>Capturas importadas</h2>${table(['Captura', 'Rol', 'Importación', 'Integridad', 'Periodo', 'Duración', 'Celdas', 'Muestras', 'Errores', 'Avisos'],
        caps.map((c) => Object.assign([`<a href="#/capturas/${c.id}"><b>${esc(c.label)}</b></a>${c.simulated ? ' <span class="badge">simulada</span>' : ''}`, ROLE[c.role] || c.role, statusChip(c.import_status), statusChip(c.checksum_status), `<span class="small">${esc(c.period_start)}<br>${esc(c.period_end)}</span>`, num(c.hours, 1) + ' h', c.cells, num(c.samples, 0), c.errors ? `<b style="color:var(--critical)">${c.errors}</b>` : '0', c.warnings], { _attrs: ` class="click" data-href="#/capturas/${c.id}"` })), [5, 6, 7, 8, 9])}</div>`;
    $$('tr.click').forEach((tr) => tr.addEventListener('click', (e) => { if (e.target.tagName !== 'A') location.hash = tr.dataset.href; }));
    let file = null;
    const setFile = (f) => { file = f; $('#fileName').innerHTML = f ? `Seleccionado: <b>${esc(f.name)}</b> (${num(f.size / 1048576, 1)} MB)` : ''; $('#upBtn').disabled = !f; if (f && !$('#upLabel').value) $('#upLabel').value = f.name.replace(/\.(tar\.gz|tgz|tar|zip)$/i, ''); };
    const drop = $('#drop');
    $('#pick').addEventListener('click', (e) => { e.preventDefault(); $('#file').click(); });
    $('#file').addEventListener('change', () => setFile($('#file').files[0]));
    ['dragenter', 'dragover'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
    ['dragleave', 'drop'].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
    drop.addEventListener('drop', (e) => setFile(e.dataTransfer.files[0]));
    $('#upBtn').addEventListener('click', () => {
      const st = $('#upStatus'), btn = $('#upBtn');
      st.innerHTML = '<div class="small">Subiendo…</div><div class="progress"><div id="upBar"></div></div><div class="log" id="upLog" style="margin-top:8px"></div>';
      const bar = $('#upBar'), logEl = $('#upLog');
      btn.disabled = true;
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/captures/upload');
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
      xhr.setRequestHeader('X-Label', encodeURIComponent($('#upLabel').value.trim()));
      xhr.setRequestHeader('X-Role', $('#upRole').value);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable) bar.style.width = (100 * e.loaded / e.total).toFixed(0) + '%'; };
      xhr.onload = async () => {
        let body = {}; try { body = JSON.parse(xhr.responseText); } catch { }
        if (xhr.status !== 202) { st.innerHTML = `<div class="callout crit">${esc(body.error || 'Error ' + xhr.status)}</div>`; btn.disabled = false; return; }
        await followJob(body.job, logEl, st);
      };
      xhr.onerror = () => { st.innerHTML = '<div class="callout crit">Error de red durante la subida.</div>'; btn.disabled = false; };
      xhr.send(file);
    });
    $('#smBtn').addEventListener('click', async () => {
      const st = $('#smStatus'), btn = $('#smBtn');
      st.innerHTML = '<div class="small muted">Iniciando…</div>';
      btn.disabled = true;
      try {
        const r = await post('/api/captures/simulate', { label: $('#smLabel').value.trim(), role: $('#smRole').value, cells: +$('#smCells').value, hours: +$('#smHours').value, hdd: +$('#smHdd').value, flash: +$('#smFlash').value, load_factor: +$('#smLoad').value, prefix: $('#smRole').value === 'destino' ? 'dstcel' : 'simcel' });
        st.innerHTML = '<div class="log"></div>';
        await followJob(r.job, st.querySelector('.log'), st);
      } catch (e) { st.innerHTML = `<div class="callout crit">${esc(e.message)}</div>`; }
      btn.disabled = false;
    });
  }
  async function followJob(id, logEl, statusEl) {
    const j = await pollJob(id, (job) => { logEl.textContent = job.log.join('\n'); logEl.scrollTop = 1e9; });
    if (j.status === 'done') {
      const r = j.result;
      statusEl.insertAdjacentHTML('beforeend', `<div class="callout ${r.import_status === 'COMPLETE' ? 'ok' : 'warn'}" style="margin-top:8px">Importada con estado <b>${esc(r.import_status)}</b> · ${num(r.samples, 0)} muestras. <a href="#/capturas/${r.capture_id}">Ver captura →</a></div>`);
      toast('Captura importada');
    } else statusEl.insertAdjacentHTML('beforeend', `<div class="callout crit" style="margin-top:8px">${esc(j.error)}</div>`);
  }

  // ------------------------------------------------------------------ Detalle de captura
  async function pageCapture(id) {
    const d = await api(`/api/captures/${id}`);
    const ms = Object.fromEntries(d.measures.map((m) => [m.id, m]));
    const res = d.measures.filter((m) => m.resource && m.system);
    const minCov = res.length ? Math.min(...res.filter((m) => m.system.status === 'OK').map((m) => +m.system.coverage)) : null;
    const nErr = d.issues_summary.filter((i) => i.severity === 'ERROR').reduce((a, b) => a + b.count, 0);
    const nWarn = d.issues_summary.filter((i) => i.severity === 'WARNING').reduce((a, b) => a + b.count, 0);
    const blocksTot = Object.values(d.blocks).reduce((a, b) => a + b, 0);
    const models = [...new Set(Object.values(d.cell_models).filter(Boolean))];
    view.innerHTML = head(esc(d.label), `${ROLE[d.role] || d.role} · ${esc(d.period.start_raw)} → ${esc(d.period.end_raw)} · ${esc(models.join(', ') || 'modelo desconocido')}`,
      `<a href="#/capturas">Capturas</a> / ${esc(d.label)}`, `${statusChip(d.import_status)} ${statusChip(d.checksum_status)} ${(d.collector_version || '').includes('-sim') ? '<span class="badge">simulada</span>' : ''}`)
      + `<div class="grid g4">${kpi('Duración', num(d.calendar.hours, 1) + ' h', esc(d.calendar.weekdays.join(', ')))}${kpi('Celdas', d.cells.length, esc(d.cells.join(', ')))}${kpi('Bloques OK', `${num(d.blocks.OK || 0, 0)}<span class="muted" style="font-size:15px"> / ${num(blocksTot, 0)}</span>`, `${num(d.sample_count, 0)} muestras`)}${kpi('Cobertura mínima', minCov === null ? '—' : pct(minCov, 2), `${nErr} errores · ${nWarn} avisos`)}</div>`
      + `<div class="callout ${d.calendar.hours < 24 ? 'crit' : 'warn'} section"><b>Cobertura temporal:</b><ul>${d.calendar.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></div>`
      + `<div class="card section"><h2>Carga del sistema</h2><div class="seg" id="mTabs">${MEASURE_TABS.filter(([k]) => ms[k]).map(([k, l]) => `<button data-m="${k}">${esc(l)}</button>`).join('')}</div>
         <div class="btn-row" style="margin:12px 0 4px"><label class="f" style="flex-direction:row;align-items:center;gap:8px">Comparar con celda <select id="cellSel" style="width:auto"><option value="">— ninguna —</option>${d.cells.map((c) => `<option>${esc(c)}</option>`).join('')}</select></label><span class="muted small" id="mNote"></span></div>
         <div id="mChart"></div><div class="stats" id="mStats"></div></div>
      <div class="grid g2 section"><div class="card"><h2>Por celda</h2><div id="cellBars"></div><div id="cellTable" style="margin-top:12px"></div></div>
      <div class="card"><h2>Cobertura por medida y celda</h2><p class="muted small">Minutos completos / minutos del periodo. Un minuto incompleto no cuenta como cero.</p><div id="heat" style="overflow-x:auto"></div></div></div>
      <div class="card section"><h2>Incidencias de importación</h2><div class="seg" id="issFilter"><button data-s="" class="active">Todas</button><button data-s="ERROR">Errores (${nErr})</button><button data-s="WARNING">Avisos (${nWarn})</button><button data-s="INFO">Info</button></div><div id="issTable" style="margin-top:10px"></div>
      ${d.counter_events.count ? `<div class="callout info small" style="margin-top:10px">${d.counter_events.count} eventos de contadores (reinicios/huecos) registrados; no generan tasa.</div>` : ''}</div>`;
    let cur = MEASURE_TABS.find(([k]) => ms[k])[0];
    async function draw() {
      $$('#mTabs button').forEach((b) => b.classList.toggle('active', b.dataset.m === cur));
      const m = ms[cur];
      const cell = $('#cellSel').value;
      const sysLabel = m.summable ? 'Sistema (suma de celdas)' : 'Máximo entre celdas (no sumable)';
      $('#mNote').textContent = m.note || (m.summable ? '' : 'Porcentajes y latencias nunca se suman entre celdas.');
      const s1 = await api(`/api/captures/${id}/series?measure=${cur}&host=*`);
      const series = [{ name: sysLabel, slot: 1, points: s1.points }];
      if (cell) { const s2 = await api(`/api/captures/${id}/series?measure=${cur}&host=${encodeURIComponent(cell)}`); series.push({ name: `Celda ${cell}`, slot: 2, points: s2.points }); }
      lineChart($('#mChart'), { series, unit: m.unit, step: s1.step_minutes, title: m.label });
      const st = m.summable ? m.system : null;
      const stats = st ? st.stats : m.cells_max_stats;
      $('#mStats').innerHTML = [['Máximo observado', num(stats.max) + ' ' + m.unit], ['Instante (UTC)', esc(stats.max_minute_utc || '—')], ['p99', num(stats.p99)], ['p95', num(stats.p95)], ['Media', num(stats.mean)], ['Cobertura', st ? pct(st.coverage, 2) + ` (${st.complete_minutes}/${st.expected_minutes} min)` : 'por celda']].map(([a, b]) => `<div><span>${a}</span><b>${b}</b></div>`).join('')
        + (st && st.status !== 'OK' ? `<div class="callout crit small">${esc(st.status)}: ${esc(st.reason)}</div>` : '');
      const bars = Object.entries(m.per_cell).filter(([, s]) => s.stats.max !== null).map(([h, s]) => ({ label: h, value: +s.stats.max }));
      barChart($('#cellBars'), { bars, unit: m.unit, title: 'Máximo por celda' });
      $('#cellTable').innerHTML = table(['Celda', 'Estado', 'Origen dato', 'Máximo', 'p95', 'Cobertura'], Object.entries(m.per_cell).map(([h, s]) => [esc(h), s.status === 'OK' ? 'OK' : `<span title="${esc(s.reason)}">${esc(s.status)}</span>`, esc(s.source), num(s.stats.max), num(s.stats.p95), pct(s.coverage, 2)]), [3, 4, 5]);
    }
    $$('#mTabs button').forEach((b) => b.addEventListener('click', () => { cur = b.dataset.m; draw(); }));
    $('#cellSel').addEventListener('change', draw);
    await draw();
    heatmap($('#heat'), {
      cols: d.cells, rows: d.measures.filter((m) => m.group !== 'info').map((m) => ({ id: m.id, label: m.label })),
      cell: (r, c) => { const s = ms[r.id].per_cell[c]; return s && { status: s.status, value: +s.coverage, complete: s.complete_minutes, expected: s.expected_minutes, reason: s.reason }; },
    });
    const drawIss = (sev) => {
      $$('#issFilter button').forEach((b) => b.classList.toggle('active', b.dataset.s === sev));
      const rows = d.issues_all.filter((i) => !sev || i.severity === sev);
      $('#issTable').innerHTML = rows.length ? table(['Severidad', 'Código', 'Celda', 'Métrica', 'Mensaje', 'Origen'], rows.slice(0, 300).map((i) => [`<span class="chip ${i.severity === 'ERROR' ? 'critical' : i.severity === 'WARNING' ? 'warning' : 'info'}"><span class="dot"></span>${esc(i.severity)}</span>`, `<span class="mono">${esc(i.code)}</span>`, esc(i.host || ''), esc(i.metric || ''), esc(i.message), `<span class="mono tiny">${esc(i.source_file || '')}${i.source_line ? ':' + i.source_line : ''}</span>`])) : '<div class="empty">Sin incidencias de este tipo.</div>';
    };
    $$('#issFilter button').forEach((b) => b.addEventListener('click', () => drawIss(b.dataset.s)));
    drawIss('');
  }

  // ------------------------------------------------------------------ Dimensionamiento
  async function pageSizing() {
    const [caps, runs, cat] = await Promise.all([api('/api/captures'), api('/api/runs'), getCatalog()]);
    if (!state.scenario) state.scenario = await api('/api/scenario/default');
    const sc = state.scenario;
    if (!state.scenarioSel.source && caps.length) {
      const o = caps.find((c) => c.role === 'origen') || caps[caps.length - 1];
      state.scenarioSel.source = String(o.id);
      const dd = caps.find((c) => c.role === 'destino');
      state.scenarioSel.dest = dd ? String(dd.id) : '';
    }
    const models = cat.models.map((m) => m.model);
    const modelOpts = (sel, allowEmpty) => (allowEmpty ? `<option value="">— desconocido —</option>` : '') + models.concat(cat.approximations.map((a) => a.requested)).map((m) => `<option ${m === sel ? 'selected' : ''}>${esc(m)}</option>`).join('');
    const storageFor = (model) => { const m = cat.models.find((x) => x.model === model) || cat.models.find((x) => x.model === (cat.approximations.find((a) => a.requested === model) || {}).use_model); return m ? m.storage.map((s) => s.id) : []; };
    const racksFor = (model, st) => { const m = cat.models.find((x) => x.model === model); return m ? m.racks.filter((r) => r.storage_ids.includes(st)).map((r) => r.name) : []; };
    const capOpts = (sel, withNone) => (withNone ? '<option value="">— ninguno —</option>' : '') + caps.map((c) => `<option value="${c.id}" ${String(c.id) === String(sel) ? 'selected' : ''}>${esc(c.label)} (${ROLE[c.role] || c.role}, ${num(c.hours, 1)} h${c.import_status !== 'COMPLETE' ? ', INCOMPLETA' : ''})</option>`).join('');
    const appr = Object.entries(cat.metric_approximations).concat(cat.approximations.map((a) => [a.id, a.warning]));
    view.innerHTML = head('Dimensionamiento', 'Defina el escenario y las configuraciones candidatas. El motor separa cálculo teórico, margen y aprobación operativa.', null,
      `<button class="btn ghost" id="loadEx">Cargar ejemplo</button><button class="btn ghost" id="reset">Restablecer</button>`)
      + (caps.length ? '' : `<div class="callout warn">Primero importe al menos una captura en <a href="#/capturas">Capturas</a>.</div>`)
      + `<div class="grid g2"><div class="card"><h2>1 · Capturas</h2><div class="form-grid">
          <label class="f">Origen (carga a migrar)<select id="sSrc">${capOpts(state.scenarioSel.source)}</select></label>
          <label class="f">Destino actual (carga ya existente)<select id="sDst">${capOpts(state.scenarioSel.dest, true)}</select></label>
          <label class="f">Modelo del origen<select data-k="source_system.model">${modelOpts((sc.source_system || {}).model, true)}</select></label>
          <label class="f">Tipo de celda del origen<select data-k="source_system.storage_type" id="srcType"></select></label></div>
          <p class="muted small" style="margin-bottom:0">Si el origen es X8M o posterior, sus lecturas servidas desde PMEM/XRMEM no se observan en los discos flash: el motor lo exige reconocer.</p></div>
        <div class="card"><h2>2 · Parámetros</h2><div class="form-grid">
          <label class="f">Nombre del escenario<input data-k="scenario_name" value="${esc(sc.scenario_name || '')}"></label>
          <label class="f">Crecimiento %<input data-k="growth_pct" type="number" min="0" value="${sc.growth_pct}"></label>
          <label class="f">Reserva %<input data-k="reserve_pct" type="number" min="0" max="95" value="${sc.reserve_pct}"></label>
          <label class="f">Estadística de demanda<select data-k="statistic">${[['max', 'Máximo'], ['p99', 'Percentil 99'], ['p95', 'Percentil 95']].map(([v, l]) => `<option value="${v}" ${sc.statistic === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
          <label class="f">Combinación origen+destino<select data-k="combination"><option value="envelope" ${sc.combination === 'envelope' ? 'selected' : ''}>Envolvente (suma de máximos)</option><option value="coincident" ${sc.combination === 'coincident' ? 'selected' : ''}>Suma coincidente</option></select></label>
          <label class="f">Alineación temporal<select data-k="align"><option value="utc" ${sc.align === 'utc' ? 'selected' : ''}>Mismo instante UTC</option><option value="time_of_day" ${sc.align === 'time_of_day' ? 'selected' : ''}>Hora del día (aprox.)</option></select></label>
          <label class="f">Latencia objetivo (µs)<input data-k="latency_target_us" type="number" min="0" value="${sc.latency_target_us ?? ''}" placeholder="sin definir"></label>
          <label class="f">Cobertura mínima %<input data-k="min_coverage_pct" type="number" min="0" max="100" value="${sc.min_coverage_pct}"></label>
          <label class="f">Recuperación tras fallo %<input data-k="n_minus_1.recovery_overhead_pct" type="number" min="0" value="${sc.n_minus_1.recovery_overhead_pct}"></label></div>
          <div style="display:flex;flex-direction:column;gap:6px;margin-top:12px">
          <label class="check"><input type="checkbox" data-k="n_minus_1.enabled" ${sc.n_minus_1.enabled ? 'checked' : ''}>Calcular escenario de fallo de una celda (N-1)</label>
          <label class="check"><input type="checkbox" data-k="critical_periods_confirmed" ${sc.critical_periods_confirmed ? 'checked' : ''}>El cliente confirma que la captura incluye los periodos críticos (picos, cierres, backups)</label>
          <label class="check"><input type="checkbox" data-k="accept_incomplete_capture" ${sc.accept_incomplete_capture ? 'checked' : ''}>Aceptar capturas incompletas (el dictamen dependerá de la cobertura)</label>
          <label class="check"><input type="checkbox" data-k="allow_multirack" ${sc.allow_multirack ? 'checked' : ''}>Permitir configuraciones multirack</label></div></div></div>
      <div class="card section"><h2>3 · Configuraciones candidatas</h2><div id="cands"></div><button class="btn ghost small" id="addCand">＋ Añadir candidato</button></div>
      <div class="card section"><h2>4 · Aproximaciones autorizadas</h2><p class="muted small">Ninguna se aplica sin marcarla. Cada una añade un aviso visible al resultado.</p><div style="display:flex;flex-direction:column;gap:8px">${appr.map(([k, w]) => `<label class="check"><input type="checkbox" data-appr="${esc(k)}" ${(sc.approximations || []).includes(k) ? 'checked' : ''}><span><span class="mono">${esc(k)}</span><br><span class="muted small">${esc(w)}</span></span></label>`).join('')}</div></div>
      <div class="card section"><details><summary><b>5 · Capacidades probadas</b> <span class="muted small">(opcional: único modo de obtener aprobación operativa)</span></summary><div id="tested" style="margin-top:10px"></div><button class="btn ghost small" id="addTested">＋ Añadir capacidad probada</button></details></div>
      <div class="card section"><div class="btn-row"><button class="btn" id="runBtn" ${caps.length ? '' : 'disabled'}>▶ Ejecutar dimensionamiento</button><span class="muted small">Se generan además informes HTML, Markdown, CSV y JSON descargables.</span></div><div id="runStatus" style="margin-top:10px"></div>
        <details class="box"><summary>Ver escenario en JSON</summary><pre class="code" id="scJson" style="margin-top:8px"></pre></details></div>
      <div class="card section"><h2>Historial</h2>${table(['Ejecución', 'Escenario', 'Origen', 'Destino', 'Dictámenes'], runs.map((r) => [`<a href="#/resultados/${r.run_id}">${esc(r.generated_utc)}</a>`, esc(r.scenario_name), esc(r.source), esc(r.dest || '—'), r.verdicts.map((v) => `<span class="chip ${(VERDICT[v.verdict] || ['warning'])[0]}"><span class="dot"></span>${esc(v.id)}</span>`).join(' ')]))}</div>`;

    const setPath = (obj, path, val) => { const ks = path.split('.'); let o = obj; ks.slice(0, -1).forEach((k) => { o[k] = o[k] || {}; o = o[k]; }); o[ks[ks.length - 1]] = val; };
    const showJson = () => { $('#scJson').textContent = JSON.stringify(sc, null, 1); };
    $$('[data-k]').forEach((i) => i.addEventListener('input', () => {
      let v = i.type === 'checkbox' ? i.checked : i.value;
      if (i.type === 'number') v = i.value === '' ? null : +i.value;
      setPath(sc, i.dataset.k, v); showJson();
    }));
    $$('[data-appr]').forEach((i) => i.addEventListener('change', () => {
      const s = new Set(sc.approximations || []); i.checked ? s.add(i.dataset.appr) : s.delete(i.dataset.appr); sc.approximations = [...s]; showJson();
    }));
    const fillSrcType = () => {
      sc.source_system = sc.source_system || {};
      const opts = storageFor(sc.source_system.model || '');
      if (!opts.includes(sc.source_system.storage_type)) sc.source_system.storage_type = opts[0] || '';
      $('#srcType').innerHTML = opts.length ? opts.map((o) => `<option ${o === sc.source_system.storage_type ? 'selected' : ''}>${o}</option>`).join('') : '<option value="">—</option>';
      $('#srcType').disabled = !opts.length;
      showJson();
    };
    $('[data-k="source_system.model"]').addEventListener('change', fillSrcType);
    fillSrcType();
    $('#sSrc').addEventListener('change', (e) => (state.scenarioSel.source = e.target.value));
    $('#sDst').addEventListener('change', (e) => (state.scenarioSel.dest = e.target.value));

    function drawCands() {
      $('#cands').innerHTML = sc.candidates.map((c, i) => {
        const sts = storageFor(c.model);
        const racks = racksFor(c.model, c.storage_type);
        return `<div class="cand" data-i="${i}"><label class="f">Identificador<input data-c="id" value="${esc(c.id)}"></label>
          <label class="f">Modelo<select data-c="model">${modelOpts(c.model)}</select></label>
          <label class="f">Tipo de celda<select data-c="storage_type">${sts.map((s) => `<option ${s === c.storage_type ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
          <label class="f">Configuración publicada<select data-c="rack_config"><option value="">— elástica / libre —</option>${racks.map((r) => `<option ${r === c.rack_config ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select></label>
          <label class="f">Celdas fijas<input data-c="cells" type="number" min="1" value="${c.cells ?? ''}" placeholder="calcular"></label>
          <label class="f">Servidores BD<input data-c="db_servers" type="number" min="1" value="${c.db_servers ?? ''}" placeholder="—"></label>
          <button class="btn ghost small" data-del="${i}" title="Quitar" aria-label="Quitar candidato">✕</button></div>`;
      }).join('') || '<div class="empty">Añada al menos un candidato.</div>';
      $$('#cands [data-c]').forEach((inp) => inp.addEventListener('change', () => {
        const i = +inp.closest('.cand').dataset.i, k = inp.dataset.c;
        let v = inp.value;
        if (inp.type === 'number') v = v === '' ? undefined : +v;
        if (v === '' || v === undefined) delete sc.candidates[i][k]; else sc.candidates[i][k] = v;
        if (k === 'model') { sc.candidates[i].storage_type = storageFor(v)[0]; delete sc.candidates[i].rack_config; }
        if (k === 'storage_type') delete sc.candidates[i].rack_config;
        drawCands(); showJson();
      }));
      $$('#cands [data-del]').forEach((b) => b.addEventListener('click', () => { sc.candidates.splice(+b.dataset.del, 1); drawCands(); showJson(); }));
    }
    function drawTested() {
      sc.tested_capacities = sc.tested_capacities || [];
      const resources = ['flash_read_iops', 'flash_write_iops', 'flash_read_mbps', 'hdd_iops', 'hdd_mbps'];
      $('#tested').innerHTML = sc.tested_capacities.map((t, i) => `<div class="cand" data-t="${i}"><label class="f">Modelo<select data-tk="model">${modelOpts(t.model)}</select></label>
        <label class="f">Tipo de celda<select data-tk="storage_type">${storageFor(t.model).map((s) => `<option ${s === t.storage_type ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
        <label class="f">Recurso<select data-tk="resource">${resources.map((r) => `<option ${r === t.resource ? 'selected' : ''}>${r}</option>`).join('')}</select></label>
        <label class="f">Evidencia (prueba, fecha)<input data-tk="evidence" value="${esc(t.evidence || '')}"></label>
        <label class="f">Capacidad por celda<input data-tk="per_cell" type="number" value="${t.per_cell ?? ''}"></label>
        <label class="f">Latencia medida µs<input data-tk="latency_us" type="number" value="${t.latency_us ?? ''}"></label>
        <button class="btn ghost small" data-tdel="${i}" aria-label="Quitar">✕</button></div>`).join('') || '<div class="muted small">Sin capacidades probadas: la aprobación operativa quedará pendiente.</div>';
      $$('#tested [data-tk]').forEach((inp) => inp.addEventListener('change', () => {
        const i = +inp.closest('[data-t]').dataset.t, k = inp.dataset.tk;
        sc.tested_capacities[i][k] = inp.type === 'number' ? (inp.value === '' ? null : +inp.value) : inp.value;
        if (k === 'model') sc.tested_capacities[i].storage_type = storageFor(inp.value)[0];
        drawTested(); showJson();
      }));
      $$('#tested [data-tdel]').forEach((b) => b.addEventListener('click', () => { sc.tested_capacities.splice(+b.dataset.tdel, 1); drawTested(); showJson(); }));
    }
    drawCands(); drawTested(); showJson();
    $('#addCand').addEventListener('click', () => { sc.candidates.push({ id: `candidato-${sc.candidates.length + 1}`, model: 'X11M', storage_type: 'HC' }); drawCands(); showJson(); });
    $('#addTested').addEventListener('click', () => { sc.tested_capacities.push({ model: 'X11M', storage_type: 'HC', resource: 'flash_write_iops', per_cell: null, latency_us: null, evidence: '' }); drawTested(); showJson(); });
    $('#loadEx').addEventListener('click', async () => { state.scenario = await api('/api/scenario/example'); toast('Escenario de ejemplo cargado'); pageSizing(); });
    $('#reset').addEventListener('click', async () => { state.scenario = await api('/api/scenario/default'); pageSizing(); });
    $('#runBtn').addEventListener('click', async () => {
      const st = $('#runStatus'), btn = $('#runBtn');
      st.innerHTML = '<div class="small muted">Iniciando…</div>';
      btn.disabled = true;
      try {
        const clean = JSON.parse(JSON.stringify(sc));
        if (clean.source_system && !clean.source_system.model) delete clean.source_system;
        const r = await post('/api/sizing', { scenario: clean, source: +$('#sSrc').value, dest: $('#sDst').value ? +$('#sDst').value : null });
        st.innerHTML = '<div class="log"></div>';
        const logEl = st.querySelector('.log');
        const j = await pollJob(r.job, (job) => { logEl.textContent = job.log.join('\n'); });
        if (j.status === 'done') { if (document.body.contains(btn)) location.hash = `#/resultados/${j.result.run_id}`; else toast('Dimensionamiento terminado: ver Historial'); return; }
        st.innerHTML = `<div class="callout crit">${esc(j.error)}</div>`;
      } catch (e) { st.innerHTML = `<div class="callout crit">${esc(e.message)}</div>`; }
      btn.disabled = false;
    });
  }

  // ------------------------------------------------------------------ Resultados
  async function pageRun(runId) {
    const r = await api(`/api/runs/${runId}`);
    const sc = r.scenario || {};
    const reserve = +(sc.reserve_pct ?? 20);
    const dl = (f, l) => r.files.includes(f) ? `<a class="btn ghost small" href="/runs/${runId}/${f}">${l}</a>` : '';
    view.innerHTML = head(esc(sc.scenario_name || 'Resultado'), `${esc(r.generated_utc)} · origen <b>${esc(r.captures.origen.label)}</b>${r.captures.destino ? ' + destino actual <b>' + esc(r.captures.destino.label) + '</b>' : ''} · crecimiento ${num(sc.growth_pct, 0)} % · reserva ${num(reserve, 0)} % · estadística ${esc(sc.statistic || 'max')}`,
      `<a href="#/dimensionamiento">Dimensionamiento</a> / resultado`,
      `<a class="btn" href="/runs/${runId}/informe.html?view=1" target="_blank">Abrir informe HTML</a>${dl('informe.md', 'Markdown')}${dl('informe.json', 'JSON')}${dl('dimensionamiento_recursos.csv', 'CSV recursos')}${dl('series.csv', 'CSV series')}`)
      + `<div class="grid ${r.candidates.length > 2 ? 'g3' : 'g2'}">${r.candidates.map((c) => {
        const lim = c.limiting_resource ? (c.resources.find((x) => x.resource === c.limiting_resource) || {}).label : null;
        return `<div class="card vcard"><div class="top"><div><div class="name">${esc(c.candidate.id)}</div><div class="muted small">${esc(c.model || '?')} ${esc(c.storage_type || '')}${c.candidate.rack_config ? ' · ' + esc(c.candidate.rack_config) : ''}</div></div>${c.approximation ? '<span class="badge appr">aproximación</span>' : ''}</div>
          ${verdictHtml(c.verdict)}
          <div class="mini-kpis"><div><b>${c.n_theoretical ?? '—'}</b><span>Mínimo teórico${c.partial ? ' (parcial)' : ''}</span></div><div><b>${c.n_recommended ?? '—'}</b><span>Recomendado${c.partial ? ' (parcial)' : ''}</span></div><div><b>${c.n_failure ?? '—'}</b><span>Con N-1</span></div><div><b>${c.cells_evaluated ?? '—'}</b><span>Evaluadas</span></div></div>
          <div class="small"><b>Recurso limitante:</b> ${esc(lim || '—')}${c.limiting_margin ? ` · margen <b>${pct(c.limiting_margin)}</b>` : ''}</div>
          <div class="small"><b>Aprobación operativa:</b> <span class="chip ${/aprobada/.test(c.operational_approval) ? 'good' : 'warning'}"><span class="dot"></span>${esc(c.operational_approval)}</span></div>
          <ul class="small" style="margin:0;padding-left:18px">${c.verdict_reasons.slice(0, 2).map((x) => `<li>${esc(x.length > 190 ? x.slice(0, 189) + '…' : x)}</li>`).join('')}${c.verdict_reasons.length > 2 ? `<li class="muted">+${c.verdict_reasons.length - 2} motivos más en el detalle</li>` : ''}</ul></div>`;
      }).join('')}</div>`
      + `<div class="card section"><h2>Utilización de diseño por recurso</h2><p class="muted small">Demanda de diseño / capacidad de referencia de las celdas evaluadas. Línea punteada: utilización máxima admitida (${num(100 - reserve, 0)} %). Línea discontinua: 100 % del máximo publicado/teórico.</p><div class="seg" id="uTabs">${r.candidates.map((c, i) => `<button data-i="${i}">${esc(c.candidate.id)}</button>`).join('')}</div><div id="uChart" style="margin-top:10px"></div></div>`
      + `<div class="card section"><h2>Demanda frente a referencia</h2><p class="muted small">Demanda medida (sin crecimiento) del origen, destino actual y su suma coincidente. Líneas discontinuas: máximo publicado/teórico del número de celdas evaluado de cada candidato — <b>no es capacidad garantizada</b>.</p><div class="seg" id="dTabs">${r.charts.map((ch, i) => `<button data-i="${i}">${esc(ch.title)}</button>`).join('')}</div><div id="dChart" style="margin-top:10px"></div></div>`
      + `<div class="card section"><h2>Carga combinada estimada</h2>${table(['Medida', 'Unidad', 'Máx. origen', 'Máx. destino actual', 'Suma coincidente', 'Envolvente', 'Notas'], r.combined.filter((c) => !/latency|util|cpu/.test(c.measure)).map((c) => [esc(c.label), esc(c.unit), num(c.source_max), num(c.dest_max), num(c.coincident_max), num(c.envelope), `<span class="small muted">${esc(c.notes.join('; '))}</span>`]), [2, 3, 4, 5])}</div>`
      + r.candidates.map((c, i) => `<div class="card section"><details ${i === 0 ? 'open' : ''}><summary><b>Detalle: ${esc(c.candidate.id)}</b> — ${esc(c.model || '?')} ${esc(c.storage_type || '')} ${verdictHtml(c.verdict)}</summary>
        <div style="margin-top:12px">${table(['Recurso', 'Estado', 'Demanda', 'Demanda diseño', 'Capacidad/celda', 'Evidencia', 'Fuente', 'Teóricas', 'Recom.', 'N-1', 'Utilización', 'Margen'],
          c.resources.map((x) => { const ref = x.reference || {}; const src = ref.source_file ? `${ref.source_file} p.${ref.page}` : (x.references || []).map((y) => `${y.source_file} p.${y.page}`).join('; '); return [`<b>${esc(x.label)}</b><div class="muted tiny">${esc(x.unit)}</div>`, x.status === 'evaluado' ? 'evaluado' : `<span title="${esc(x.reason)}">${esc(x.status)}</span>`, num(x.demand), num(x.design_demand), num(x.capacity_per_cell), evidenceBadge(x.capacity_evidence), `<span class="tiny">${esc(src)}</span>`, x.n_theoretical ?? '—', x.n_recommended ?? '—', x.n_failure ?? '—', pct(x.utilization), pct(x.margin)]; }), [2, 3, 4, 7, 8, 9, 10, 11])}</div>
        <details class="box"><summary>Fórmulas, derivaciones y motivos</summary><ul class="small">${c.resources.map((x) => `<li><b>${esc(x.label)}:</b> ${esc(x.margin_formula || x.reason)}${x.demand_method ? `<br><span class="muted">Demanda: ${esc(x.demand_method)}</span>` : ''}${(x.reference || {}).derivation ? `<br><span class="muted">Derivación: ${esc(x.reference.derivation)}</span>` : ''}${(x.warnings || []).map((w) => `<br>⚠ ${esc(w)}`).join('')}</li>`).join('')}</ul></details>
        ${c.db_check ? `<div class="callout ${c.db_check.status === 'excedido' ? 'crit' : 'info'} small"><b>Servidores de BD:</b> ${esc(c.db_check.status)}. ${esc(c.db_check.reason || '')}${(c.db_check.checks || []).map((k) => ` · ${esc(k.resource)}: ${esc(k.utilization || k.reason)}`).join('')}</div>` : ''}
        ${c.approval_pending.length ? `<div class="callout warn small"><b>Pendiente para la aprobación operativa:</b><ul>${c.approval_pending.map((p) => `<li>${esc(p)}</li>`).join('')}</ul></div>` : ''}
        ${c.warnings.length ? `<details class="box"><summary>Advertencias (${c.warnings.length})</summary><ul class="small">${c.warnings.map((w) => `<li>${esc(w)}</li>`).join('')}</ul></details>` : ''}
        <details class="box"><summary>Supuestos</summary><ul class="small">${c.assumptions.map((a) => `<li>${esc(a)}</li>`).join('')}</ul></details></details></div>`).join('')
      + (r.current_vs_reference ? `<div class="card section"><h2>Carga actual frente a su referencia publicada</h2>${Object.entries(r.current_vs_reference).map(([role, rows]) => `<h3>${esc(ROLE[role] || role)}</h3>${table(['Recurso', 'Estado', 'Máx. observado', 'Referencia total', 'Evidencia', 'Utilización'], rows.map((x) => [esc(x.label || x.resource), esc(x.status), num(x.observed_max), num(x.reference_total), x.evidence ? evidenceBadge(x.evidence) : `<span class="small muted">${esc(x.reason || '')}</span>`, esc(x.utilization || '—')]), [2, 3])}`).join('')}</div>` : '');
    const drawU = (i) => {
      $$('#uTabs button').forEach((b) => b.classList.toggle('active', +b.dataset.i === i));
      const c = r.candidates[i];
      const bars = c.resources.filter((x) => x.utilization !== null && x.utilization !== undefined && x.utilization !== 'None').map((x) => ({ label: x.label.replace(/ \(.*\)$/, ''), value: +x.utilization * 100, note: +x.utilization > (100 - reserve) / 100 ? '✖ supera lo admitido' : '' }));
      if (!bars.length) { $('#uChart').innerHTML = `<div class="empty">${c.partial ? 'Datos insuficientes: no se muestran márgenes sin la información esencial.' : 'Sin recursos evaluados.'}</div>`; return; }
      const mx = Math.max(110, ...bars.map((b) => b.value));
      const niceMx = [120, 150, 200, 250, 300, 400, 500, 750, 1000, 2000, 5000].find((v) => v >= mx) || Math.ceil(mx / 1000) * 1000;
      barChart($('#uChart'), { bars, unit: '%', max: niceMx, labelWidth: 260, fmtValue: (v) => fmt(v, 1) + ' %', axisFmt: (v) => fmt(v, 0) + ' %', refs: [{ label: `admitida ${fmt(100 - reserve, 0)} %`, value: 100 - reserve, kind: 'limit' }, { label: '100 % máx. publicado/teórico', value: 100 }] });
      if (c.partial) $('#uChart').insertAdjacentHTML('afterbegin', '<div class="callout warn small">Resultado parcial (datos insuficientes): utilizaciones orientativas, sin margen.</div>');
    };
    const drawD = (i) => {
      $$('#dTabs button').forEach((b) => b.classList.toggle('active', +b.dataset.i === i));
      const ch = r.charts[i];
      if (!ch) { $('#dChart').innerHTML = '<div class="empty">Sin series.</div>'; return; }
      lineChart($('#dChart'), { series: ch.series, refs: ch.refs.map((x) => ({ ...x, short: `${x.candidate} (${x.label.match(/\((\d+) celdas\)/)?.[1] || '?'} celdas)` })), unit: ch.unit, title: ch.title, height: 320 });
    };
    $$('#uTabs button').forEach((b) => b.addEventListener('click', () => drawU(+b.dataset.i)));
    $$('#dTabs button').forEach((b) => b.addEventListener('click', () => drawD(+b.dataset.i)));
    drawU(0); drawD(0);
  }

  // ------------------------------------------------------------------ Catálogo
  async function pageCatalog() {
    const cat = await getCatalog();
    let sel = state.catModel || cat.models[cat.models.length - 1].model;
    view.innerHTML = head('Catálogo Exadata', `Versión ${esc(cat.version)} · cifras de datasheets Oracle con fuente y página; las derivadas indican su fórmula.`)
      + `<div class="callout warn small">${cat.notes.map(esc).join('<br>')}</div><div class="tabs" id="mt">${cat.models.map((m) => `<button data-m="${esc(m.model)}">${esc(m.model)}</button>`).join('')}</div><div id="mv"></div>
      <div class="card section"><h2>Aproximaciones disponibles (requieren autorización explícita)</h2>${table(['Identificador', 'Efecto'], Object.entries(cat.metric_approximations).map(([k, v]) => [`<span class="mono">${esc(k)}</span>`, esc(v)]).concat(cat.approximations.map((a) => [`<span class="mono">${esc(a.id)}</span>`, esc(a.warning)])))}</div>`;
    const draw = () => {
      state.catModel = sel;
      $$('#mt button').forEach((b) => b.classList.toggle('active', b.dataset.m === sel));
      const m = cat.models.find((x) => x.model === sel);
      $('#mv').innerHTML = `<div class="grid g3"><div class="card"><h2>Fuente</h2><div class="small"><b>${esc(m.source.title)}</b><br>${esc(m.source.file)}<br>${esc(m.source.edition)}<br><span class="mono tiny">SHA-256 ${esc(m.source.sha256.slice(0, 24))}…</span></div></div>
        <div class="card"><h2>Lecturas 8 KB publicadas</h2><div class="kpi"><span class="value" style="font-size:22px">${esc(m.read_iops_tier.toUpperCase())}</span><span class="sub">${esc(m.read_iops_tier_basis)}</span></div>${m.read_iops_tier !== 'flash' ? '<div class="callout warn small">No equiparable a lectura flash sin la aproximación <span class="mono">read_iops_memory_tier_as_flash</span>.</div>' : ''}</div>
        <div class="card"><h2>Límites y red</h2><div class="small">Celdas por rack: mín. ${m.limits.min_storage_servers}, máx. ${m.limits.max_storage_servers_per_rack}${m.limits.max_hc_storage_servers_per_rack ? ` (HC ${m.limits.max_hc_storage_servers_per_rack})` : ''} · BD máx. ${m.limits.max_db_servers_per_rack} (p.${m.limits.page})<br>Red: ${m.network.ports_per_server}× ${m.network.gbps_per_port} Gb/s ${esc(m.network.fabric)} (p.${m.network.page})<br>${m.latency_claims.map((l) => `Latencia (marketing): «${esc(l.text)}» p.${l.page}`).join('<br>') || 'Sin afirmaciones de latencia'}</div></div></div>
        ${m.storage.map((s) => `<div class="card section"><h2>${esc(s.id)} · ${esc(s.label)}</h2><div class="muted small" style="margin-bottom:8px">${Object.entries(s.hardware || {}).filter(([k]) => k !== 'page').map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(' · ')}</div>
          ${table(['Recurso', 'Capacidad por celda', 'Unidad', 'Evidencia', 'Fuente', 'Condiciones / derivación'], s.refs.map((x) => [esc(x.resource_label), num(x.value, 1), esc(x.unit), x.needs_approval ? '<span class="badge appr">requiere aproximación</span>' : evidenceBadge(x.evidence), `<span class="tiny">${esc(x.source_file)} p.${esc(x.page)}</span>`, `<span class="tiny">${esc(x.derivation || x.conditions)}</span>`]).concat(s.unavailable.filter((u) => !s.refs.some((x) => x.resource === u.resource)).map((u) => [esc(u.resource_label), '—', '', '<span class="badge">no disponible</span>', '', `<span class="tiny">${esc(u.reason)}</span>`])), [1])}</div>`).join('')}
        <div class="card section"><h2>Configuraciones de rack publicadas</h2>${table(['Configuración', 'Servidores BD', 'Celdas', 'Tipos', 'Flash (por tipo)', 'HDD', 'Página'], m.racks.map((rk) => [esc(rk.name), rk.db_servers, rk.storage_servers, esc(rk.storage_ids.join(', ')), `<span class="tiny">${Object.entries(rk.flash).map(([t, f]) => `${esc(t)}: ${Object.entries(f).map(([k, v]) => `${esc(k.replace('sql_', ''))}=${num(v, 1)}`).join(', ')}`).join('<br>')}</span>`, `<span class="tiny">${Object.entries(rk.disk).map(([k, v]) => `${esc(k.replace('sql_', ''))}=${num(v, 1)}`).join(', ')}</span>`, rk.page]), [1, 2, 6])}<p class="muted small">Las cifras de rack pueden estar limitadas por los servidores de BD: no se dividen entre celdas salvo en las derivaciones declaradas.</p></div>`;
    };
    $$('#mt button').forEach((b) => b.addEventListener('click', () => { sel = b.dataset.m; draw(); }));
    draw();
  }

  // ------------------------------------------------------------------ Guía
  async function pageGuide(doc) {
    doc = doc || 'guia';
    const tabs = [['guia', 'Interpretación de informes'], ['metodologia', 'Metodología'], ['limitaciones', 'Limitaciones'], ['recolector', 'Recolector']];
    let html = '<div class="empty">Documento no disponible en esta instalación.</div>';
    try { html = (await api(`/api/docs/${doc}`)).html; } catch { }
    view.innerHTML = head('Guía', 'Cómo interpretar los resultados y qué decisiones permiten.') + `<div class="tabs">${tabs.map(([k, l]) => `<button data-d="${k}" class="${k === doc ? 'active' : ''}">${l}</button>`).join('')}</div><div class="card doc">${html}</div>`;
    $$('.tabs button').forEach((b) => b.addEventListener('click', () => (location.hash = `#/guia/${b.dataset.d}`)));
  }

  // ------------------------------------------------------------------ arranque
  function initTheme() {
    const order = ['auto', 'light', 'dark'], names = { auto: 'automático', light: 'claro', dark: 'oscuro' };
    let t = 'auto'; try { t = localStorage.getItem('exacell-theme') || 'auto'; } catch { }
    const apply = () => { if (t === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t); $('#themeToggle').textContent = 'Tema: ' + names[t]; };
    $('#themeToggle').addEventListener('click', () => { t = order[(order.indexOf(t) + 1) % 3]; try { localStorage.setItem('exacell-theme', t); } catch { } apply(); });
    apply();
  }
  $('#menuToggle').addEventListener('click', () => $('#sidebar').classList.toggle('open'));
  initTheme();
  api('/api/info').then((i) => { state.info = i; $('#versionInfo').innerHTML = `v${esc(i.version)} · recolector ${esc(i.collector_version)} · catálogo ${esc(i.catalog_version)}<br><span title="${esc(i.workspace)}">Datos: ${esc(i.workspace.split(/[\\/]/).slice(-1)[0])}</span>`; }).catch(() => { });
  window.addEventListener('hashchange', router);
  router();
})();
