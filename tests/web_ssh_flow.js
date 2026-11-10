// Flujo de la página «Recogida SSH» en navegador real contra un dbnode SIMULADO.
//   node tests/web_ssh_flow.js <url_base> <dir_dbnode> <cell_group> <dir_capturas_pantalla>
const { chromium } = require('playwright');
const [base, rdir, group, shots] = process.argv.slice(2);
const fail = (m) => { console.error('FALLO: ' + m); process.exit(1); };
(async () => {
  const b = await chromium.launch();
  const pg = await b.newPage({ viewport: { width: 1360, height: 900 } });
  const errs = [];
  pg.on('pageerror', (e) => errs.push(e.message));
  pg.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  pg.on('dialog', (d) => d.accept());
  await pg.goto(base + '/#/ssh');
  await pg.waitForSelector('#sTest');
  await pg.fill('#sHost', 'dbnode01'); await pg.fill('#sUser', 'oracle');
  await pg.fill('#sDir', rdir); await pg.fill('#sGroup', group);
  await pg.click('#sTest');
  await pg.waitForSelector('#sTestOut .callout', { timeout: 60000 });
  if (!/Listo para recoger/.test(await pg.textContent('#sTestOut'))) fail('prueba de conexión: ' + await pg.textContent('#sTestOut'));
  await pg.selectOption('#sDur', '2');
  await pg.fill('#sLabel', 'ssh_origen');
  await pg.click('#sDry');
  await pg.waitForSelector('#sGoOut pre', { timeout: 60000 });
  if (!/NOT </.test(await pg.textContent('#sGoOut'))) fail('simulación -n sin sintaxis esperada');
  await pg.click('#sGo');
  await pg.waitForSelector('#sGoOut .callout.ok', { timeout: 60000 });
  // esperar a que termine pulsando «Actualizar»
  for (let i = 0; i < 60; i++) {
    await pg.click('#sRuns [data-a="status"]');
    await pg.waitForTimeout(1500);
    if (await pg.$('#sRuns [data-a="import"]')) break;
  }
  if (shots) await pg.screenshot({ path: shots + '/ssh_finished.png', fullPage: true });
  const chip = await pg.textContent('#sRuns .chip');
  if (!/COMPLETE/.test(chip)) fail('estado final inesperado: ' + chip);
  await pg.click('#sRuns [data-a="import"]');
  await pg.waitForSelector('#sRuns a:has-text("Ver captura importada")', { timeout: 120000 });
  // traer captura existente con otra etiqueta
  await pg.fill('#sLabel', 'ssh_traida');
  await pg.click('#sList');
  await pg.waitForSelector('#sCaps [data-fetch]', { timeout: 60000 });
  await pg.click('#sCaps [data-fetch]');
  await pg.waitForSelector('#sFetchOut .callout', { timeout: 120000 });
  if (!/Importada con estado <b>COMPLETE|Importada con estado COMPLETE/.test(await pg.innerHTML('#sFetchOut'))) fail('captura traída: ' + await pg.textContent('#sFetchOut'));
  if (shots) await pg.screenshot({ path: shots + '/ssh_page.png', fullPage: true });
  await pg.goto(base + '/#/capturas'); await pg.waitForSelector('text=ssh_traida');
  if (errs.length) fail('errores de JavaScript: ' + JSON.stringify(errs));
  console.log('OK');
  await b.close();
})().catch((e) => fail(e.stack || String(e)));
