// Flujo de usuario completo en un navegador real (Playwright). Uso:
//   node tests/web_ui_flow.js <url_base> <archivo.tar.gz> <dir_capturas_pantalla>
// Sale con código 0 si todo el flujo funciona sin errores de JavaScript.
const { chromium } = require('playwright');
const [base, upload, shots] = process.argv.slice(2);
const fail = (m) => { console.error('FALLO: ' + m); process.exit(1); };

(async () => {
  const b = await chromium.launch();
  const pg = await b.newPage({ viewport: { width: 1360, height: 900 } });
  const errs = [];
  pg.on('pageerror', (e) => errs.push(e.message));
  pg.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });

  // 1) Panel vacío
  await pg.goto(base + '/#/');
  await pg.waitForSelector('text=Cómo empezar');

  // 2) Capturas simuladas (origen y destino) desde la interfaz
  await pg.goto(base + '/#/capturas');
  await pg.waitForSelector('#smBtn');
  for (const [label, role] of [['ui_origen', 'origen'], ['ui_destino', 'destino']]) {
    await pg.fill('#smLabel', label);
    await pg.selectOption('#smRole', role);
    await pg.fill('#smCells', '2'); await pg.fill('#smHours', '24');
    await pg.fill('#smHdd', '4'); await pg.fill('#smFlash', '2');
    await pg.click('#smBtn');
    await pg.waitForSelector('#smStatus .callout', { timeout: 120000 });
    const t = await pg.textContent('#smStatus');
    if (!/COMPLETE/.test(t)) fail('simulación no completada: ' + t);
  }

  // 3) Subida de un .tar.gz (como el que entrega el cliente)
  await pg.setInputFiles('#file', upload);
  await pg.fill('#upLabel', 'ui_subida');
  await pg.click('#upBtn');
  await pg.waitForSelector('#upStatus .callout', { timeout: 120000 });
  const up = await pg.textContent('#upStatus');
  if (!/Importada con estado/.test(up)) fail('subida fallida: ' + up);

  // 4) Detalle de captura: gráfico, cobertura, incidencias
  await pg.click('#upStatus a');
  await pg.waitForSelector('#mChart svg', { timeout: 60000 });
  await pg.waitForSelector('#heat .c');
  await pg.click('#mTabs button[data-m="hdd_mbps"]');
  await pg.waitForTimeout(400);
  if (shots) await pg.screenshot({ path: shots + '/flow_capture.png' });

  // 5) Dimensionamiento con el escenario de ejemplo
  await pg.goto(base + '/#/dimensionamiento');
  await pg.waitForSelector('#loadEx');
  await pg.click('#loadEx');
  await pg.waitForSelector('#cands .cand');
  const opts = await pg.$$eval('#sSrc option', (os) => os.map((o) => [o.value, o.textContent]));
  const src = opts.find(([, t]) => t.startsWith('ui_origen'))[0];
  const dst = opts.find(([, t]) => t.startsWith('ui_destino'))[0];
  await pg.selectOption('#sSrc', src);
  await pg.selectOption('#sDst', dst);
  await pg.click('#addCand');
  const n = await pg.$$eval('#cands .cand', (c) => c.length);
  if (n < 2) fail('no se añadió el candidato');
  if (shots) await pg.screenshot({ path: shots + '/flow_sizing.png', fullPage: true });
  await pg.click('#runBtn');
  await pg.waitForURL(/#\/resultados\//, { timeout: 120000 });
  await pg.waitForSelector('.vcard .verdict');
  const verdicts = await pg.$$eval('.vcard .verdict', (v) => v.map((x) => x.textContent.trim()));
  if (!verdicts.length) fail('sin dictámenes');
  if (!verdicts.every((v) => /Compatible bajo las referencias|No compatible con los límites|Datos insuficientes para concluir/.test(v))) fail('dictamen inesperado ' + verdicts);
  await pg.waitForSelector('#dChart svg');
  await pg.waitForSelector('#uChart');
  if (shots) await pg.screenshot({ path: shots + '/flow_run.png', fullPage: true });

  // 6) Descarga del informe HTML autocontenido
  const href = await pg.getAttribute('a:has-text("Abrir informe HTML")', 'href');
  const resp = await pg.request.get(base + href);
  if (resp.status() !== 200 || !(await resp.text()).includes('Informe ExaCell Sizer')) fail('informe HTML no disponible');

  // 7) Catálogo y guía
  await pg.goto(base + '/#/catalogo'); await pg.waitForSelector('#mv table');
  await pg.goto(base + '/#/guia'); await pg.waitForSelector('.doc h1');
  await pg.goto(base + '/#/recolector'); await pg.waitForSelector('#cmdRun');
  const cmd = await pg.textContent('#cmdRun');
  if (!/exacell_collect\.sh -d .* -u celladmin -s .*\+01:00 -e .*\+01:00/.test(cmd)) fail('comando del recolector mal formado: ' + cmd);

  if (errs.length) fail('errores de JavaScript: ' + JSON.stringify(errs));
  console.log('OK', JSON.stringify(verdicts));
  await b.close();
})().catch((e) => fail(e.stack || String(e)));
