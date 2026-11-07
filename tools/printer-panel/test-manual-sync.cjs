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
    let sync = {enabled: true, running: false, phase: 'waiting', editor_locked: false};
    let offline = false;
    const assets = path.resolve(__dirname, '../../root/defaults/printer-panel');
    await page.route('http://manual-sync.test/**', async route => {
      const name = new URL(route.request().url()).pathname;
      if (name.includes('/nextcloud/')) {
        if (offline) return route.abort();
        if (name.endsWith('/sync-now')) sync = {enabled: true, running: true, phase: 'checking', editor_locked: true};
        if (name.endsWith('/sync-stop')) sync = {enabled: false, running: true, phase: 'stopping', editor_locked: true};
        return route.fulfill({json: {connected: true, server: 'https://cloud.example.com', user: 'fixture',
          folder: '/Slicer', sync, folders: [], path: '/Slicer'}});
      }
      if (name === '/desktop.html') return route.fulfill({contentType: 'text/html', body:
        '<button id="input">Desktop input</button><script>window.inputs=0; document.addEventListener("keydown",()=>inputs++); document.addEventListener("click",()=>inputs++);</script>'});
      if (name.endsWith('/panel.js')) return route.fulfill({contentType: 'text/javascript', body: ''});
      const file = name === '/' ? 'index.html' : path.basename(name);
      return route.fulfill({contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html',
        body: fs.readFileSync(path.join(assets, file))});
    });
    await page.goto('http://manual-sync.test/');
    await page.waitForFunction(() => !document.getElementById('workspace').inert);
    await page.locator('#nextcloud-toggle').click();
    await page.locator('#nextcloud-sync-now').click();
    await page.waitForFunction(() => document.getElementById('workspace').inert);
    assert.equal(await page.locator('#nextcloud-editor-lock').isVisible(), true);
    await page.locator('#nextcloud-close').click();
    assert.equal(await page.locator('#nextcloud-editor-lock').isVisible(), true);
    const desktop = page.frames().find(f => f.url().endsWith('/desktop.html'));
    await page.mouse.click(100, 100);
    await page.keyboard.press('a');
    assert.equal(await desktop.evaluate(() => window.inputs), 0);
    assert.equal(await page.evaluate(() => {
      let drops = 0;
      document.body.addEventListener('drop', () => drops++);
      const accepted = document.body.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true}));
      return drops === 0 && !accepted;
    }), true);
    // Reloading and failed status polls must not expose desktop input mid-run.
    await page.reload();
    await page.waitForFunction(() => !document.getElementById('nextcloud-editor-lock').hidden);
    offline = true;
    await page.waitForTimeout(2300);
    assert.equal(await page.locator('#workspace').evaluate(e => e.inert), true);
    offline = false;
    await page.locator('#nextcloud-editor-lock-stop').click();
    assert.equal(await page.locator('#workspace').evaluate(e => e.inert), true);
    sync = {enabled: false, running: false, phase: 'disabled', editor_locked: false};
    await page.waitForFunction(() => !document.getElementById('workspace').inert);
    sync = {enabled: true, running: false, phase: 'idle', editor_locked: false};
    await page.locator('#nextcloud-toggle').click();
    await page.locator('#nextcloud-sync-now').click();
    sync = {enabled: true, running: false, phase: 'error', editor_locked: false, message: 'Cloud unavailable.'};
    await page.waitForFunction(() => !document.getElementById('workspace').inert);
    assert.equal(await page.locator('#nextcloud-editor-lock').isVisible(), false);
    console.log('Manual sync: input/drop overlay, keyboard isolation, panel close, reload, offline polling, stop and failure release passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
