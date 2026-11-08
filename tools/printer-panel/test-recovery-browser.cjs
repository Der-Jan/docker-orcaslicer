// Run with Playwright installed under .test-tools/node_modules.
const {chromium} = require('./.test-tools/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({executablePath: process.env.CHROMIUM_PATH,
    headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const assets = path.resolve(__dirname, '../../root/defaults/printer-panel');
    let requests = 0, desktopLoads = 0, release;
    let result = {action: 'restored', message: 'Orca is running. Its window has been brought forward.'};
    let failure = false;
    await page.route('http://recovery.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/restore')) {
        requests++;
        assert.equal(route.request().headers()['x-orca-action'], 'restore');
        assert.equal(route.request().postData(), '{}');
        await new Promise(resolve => { release = resolve; });
        return route.fulfill({status: failure ? 503 : 200, json: result});
      }
      if (url.pathname.includes('/nextcloud/')) return route.fulfill({json: {
        connected: false, sync: {enabled: true, running: true, phase: 'checking', editor_locked: true}
      }});
      if (url.pathname === '/desktop.html') { desktopLoads++; return route.fulfill({body: ''}); }
      if (url.pathname.endsWith('/panel.js')) return route.fulfill({body: '', contentType: 'text/javascript'});
      const file = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
      return route.fulfill({body: fs.readFileSync(path.join(assets, file)), contentType:
        file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.goto('http://recovery.test/');
    await page.waitForFunction(() => !document.getElementById('nextcloud-editor-lock').hidden);
    const button = page.locator('#orca-restore');
    await button.click();
    await page.waitForFunction(() => document.getElementById('orca-restore').disabled);
    assert.equal(requests, 1);
    await page.evaluate(() => document.getElementById('orca-restore').click());
    assert.equal(requests, 1);
    release();
    await page.waitForFunction(() => !document.getElementById('orca-restore').disabled);
    assert.match(await page.locator('#orca-restore-status').textContent(), /brought forward/);
    assert.equal(await page.locator('#workspace').evaluate(e => e.inert), true);
    result = {action: 'started', message: 'Orca was not running and has been started again.'};
    await button.click();
    await page.waitForFunction(() => document.getElementById('orca-restore').disabled);
    release();
    await page.waitForFunction(() => !document.getElementById('orca-restore').disabled);
    assert.match(await page.locator('#orca-restore-status').textContent(), /started again/);
    failure = true; result = {message: 'Orca exited during startup.'};
    await button.click();
    await page.waitForFunction(() => document.getElementById('orca-restore').disabled);
    release();
    await page.waitForFunction(() => !document.getElementById('orca-restore').disabled);
    assert.equal(await page.locator('#orca-restore-status').getAttribute('data-state'), 'error');
    assert.equal(desktopLoads, 1);
    console.log('Recovery UI: accessible outside locked desktop, duplicate click blocked, restore/start/error feedback and no stream reload passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
