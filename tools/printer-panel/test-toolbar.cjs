// Run with Playwright installed under .test-tools/node_modules.
const {chromium} = require('./.test-tools/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_PATH,
    args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1728, height: 1000}});
    const assets = path.resolve(__dirname, '../../root/defaults/printer-panel');
    const shots = path.join(__dirname, '.test-tools/toolbar-20261113');
    fs.mkdirSync(shots, {recursive: true});
    let sync = {enabled: true, running: false, phase: 'idle', editor_locked: false};
    let desktopLoads = 0, restores = 0;
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const serve = async route => {
      const name = new URL(route.request().url()).pathname;
      if (name.endsWith('/solidcraft')) return route.fulfill({json: {request: null}});
      if (name.endsWith('/printer.json')) return route.fulfill({json: {ready: false, revision: 'test', message: 'Test printer offline'}});
      if (name.includes('/nextcloud/')) return route.fulfill({json: {connected: true, user: 'fixture',
        server: 'https://cloud.example', folder: '/Slicer', sync}});
      if (name.endsWith('/restore')) {
        restores++;
        return route.fulfill({json: {message: 'Orca is running. Its window has been brought forward.'}});
      }
      if (name === '/desktop.html') {
        desktopLoads++;
        return route.fulfill({contentType: 'text/html', body: '<body style="margin:0;background:#40464a;color:white;font:13px system-ui"><header style="height:32px;background:#22292b;display:flex;align-items:center;padding:0 16px">File<span style="position:absolute;left:45%">Print project — OrcaSlicer</span><span style="position:absolute;right:16px">−　□　×</span></header><p style="margin:30px">Slicer desktop</p></body>'});
      }
      const file = name === '/' ? 'index.html' : path.basename(name);
      return route.fulfill({body: fs.readFileSync(path.join(assets, file)), contentType:
        file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    };
    await page.route('http://toolbar.test/**', serve);
    const trigger = page.locator('#panels-toggle');
    const actions = page.locator('#toolbar-actions');
    async function expanded(value) {
      await page.waitForFunction(value => document.getElementById('toolbar-actions').hidden !== value, value);
    }
    async function reveal() { await trigger.hover(); await expanded(true); }
    await page.goto('http://toolbar.test/');
    await page.waitForFunction(() => document.getElementById('panels-toggle').dataset.status === 'ready');
    assert.equal(await actions.isVisible(), false);
    assert.equal(await page.locator('#panels-led').evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(89, 217, 139)');
    await page.screenshot({path: path.join(shots, 'collapsed.png')});
    await reveal();
    const row = await page.locator('.edge-tab').evaluateAll(nodes => nodes.map(e => {
      const r = e.getBoundingClientRect(); return {x: r.x, y: r.y, right: r.right, bottom: r.bottom};
    }));
    assert.ok(row.every(r => r.y === row[0].y && r.bottom <= 32 && r.right <= 1728 - 112));
    assert.ok(row[0].x < row[1].x && row[1].x < row[2].x);
    await page.screenshot({path: path.join(shots, 'expanded.png')});
    await page.mouse.move(20, 100); await expanded(false);
    await trigger.click(); await expanded(true);
    await page.locator('#printer-toggle').click(); await expanded(false);
    assert.equal(await page.locator('#printer-panel').isVisible(), true);
    await page.locator('#close').click();
    assert.equal(await trigger.evaluate(e => document.activeElement === e), true);
    await trigger.press('ArrowDown'); await expanded(true);
    assert.equal(await page.locator('#printer-toggle').evaluate(e => document.activeElement === e), true);
    await page.keyboard.press('Tab');
    assert.equal(await page.locator('#nextcloud-toggle').evaluate(e => document.activeElement === e), true);
    await page.keyboard.press('Escape'); await expanded(false);
    assert.equal(await trigger.evaluate(e => document.activeElement === e), true);
    await reveal(); await page.locator('#toolbar-pin').click();
    await page.mouse.move(20, 100); await page.waitForTimeout(450);
    assert.equal(await actions.isVisible(), true);
    await page.reload(); await expanded(true);
    assert.equal(await page.locator('#toolbar-pin').getAttribute('aria-pressed'), 'true');
    await page.locator('#nextcloud-toggle').click();
    assert.equal(await actions.isVisible(), true);
    assert.equal(await page.locator('#nextcloud-panel').isVisible(), true);
    await page.locator('#nextcloud-close').click();
    await page.locator('#toolbar-pin').click();
    await page.mouse.move(20, 100); await expanded(false);
    sync = {enabled: true, running: true, phase: 'checking', editor_locked: true};
    await page.waitForFunction(() => document.getElementById('panels-toggle').dataset.status === 'syncing');
    assert.equal(await page.locator('#panels-led').evaluate(e => getComputedStyle(e).animationName), 'nextcloud-blink');
    assert.equal(await page.locator('#workspace').evaluate(e => e.inert), true);
    await reveal(); await page.locator('#orca-restore').click(); await expanded(false);
    assert.equal(restores, 1);
    assert.match(await page.locator('#orca-restore-status').textContent(), /brought forward/);
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await page.locator('#panels-led').evaluate(e => getComputedStyle(e).animationName), 'none');
    sync = {enabled: true, running: false, phase: 'error', editor_locked: false, message: 'Offline'};
    await page.waitForFunction(() => document.getElementById('panels-toggle').dataset.status === 'error');
    assert.equal(await page.locator('#panels-led').evaluate(e => getComputedStyle(e).backgroundColor), 'rgb(240, 113, 113)');
    for (const width of [1024, 600, 360]) {
      await page.setViewportSize({width, height: 800});
      await page.evaluate(() => { document.getElementById('orca-restore-status').hidden = false; });
      await reveal();
      const box = await actions.boundingBox(), handle = await trigger.boundingBox();
      assert.ok(box.x >= 0 && box.x + box.width <= width - 112 && box.y >= handle.y + handle.height);
      assert.ok((await page.locator('#orca-restore-status').boundingBox()).y >= box.y + box.height);
      if (width === 360) await page.screenshot({path: path.join(shots, 'narrow.png')});
      await page.locator('#printer-toggle').click();
      assert.equal(await page.locator('#printer-panel').isVisible(), true);
      await page.locator('#close').click();
      await page.mouse.move(width - 10, 200); await expanded(false);
    }
    assert.equal(desktopLoads, 2, 'Only the explicit test reload restarts the frame');
    const touch = await browser.newPage({hasTouch: true, viewport: {width: 420, height: 800}});
    await touch.route('http://toolbar.test/**', serve);
    await touch.goto('http://toolbar.test/');
    await touch.locator('#panels-toggle').tap();
    await touch.waitForTimeout(500);
    assert.equal(await touch.locator('#toolbar-actions').isVisible(), true, 'Touch release keeps the actions open');
    await touch.locator('#nextcloud-toggle').tap();
    assert.equal(await touch.locator('#nextcloud-panel').isVisible(), true);
    assert.equal(await touch.locator('#toolbar-actions').isVisible(), false);
    await touch.close();
    assert.deepEqual(errors, []);
    console.log('Toolbar: hover/click, action collapse, keyboard focus, persistent pin, LED states, reduced motion, recovery during sync and responsive placement passed.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
