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
    const reference = Buffer.from(JSON.stringify({account: 'user', room: 'room', id: 'file', folder: '/Slicer'})).toString('base64url');
    let request = null, desktopLoads = 0;
    await page.route('http://source.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/solidcraft')) return route.fulfill({json: {request}});
      if (url.pathname === '/desktop.html') { desktopLoads++; return route.fulfill({body: ''}); }
      const file = url.pathname === '/' ? 'index.html' : path.basename(url.pathname);
      if (file.endsWith('.js') && file !== 'solidcraft.js') return route.fulfill({body: '', contentType: 'text/javascript'});
      return route.fulfill({body: fs.readFileSync(path.join(assets, file)), contentType:
        file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html'});
    });
    await page.context().route('https://solidcraft.example/**', route => route.fulfill({body: 'Original cloud model'}));
    await page.goto('http://source.test/');
    assert.equal(await page.locator('#solidcraft-return').isVisible(), false);
    request = {nonce: 'one', key: 'a'.repeat(64), object_id: 17,
      object_name: '<img src=x onerror=alert(1)>', name: 'Bracket.sc', service: 'https://solidcraft.example',
      url: `https://solidcraft.example/?craft-source=${reference}`, snapshot: true};
    await page.locator('#solidcraft-return').waitFor({state: 'visible'});
    assert.equal(browser.contexts()[0].pages().length, 1, 'No unsolicited browser tab');
    assert.equal(await page.locator('#solidcraft-model img').count(), 0, 'Untrusted labels remain text');
    assert.equal(await page.locator('#solidcraft-snapshot').isVisible(), true);
    assert.equal(await page.locator('#workspace').evaluate(e => e.inert), true, 'Link available outside locked desktop');
    const [opened] = await Promise.all([page.waitForEvent('popup'), page.locator('#solidcraft-edit').click()]);
    await opened.waitForLoadState();
    assert.equal(opened.url(), request.url);
    await opened.close();
    await page.locator('#solidcraft-dismiss').click();
    await page.waitForTimeout(1700);
    assert.equal(await page.locator('#solidcraft-return').isVisible(), false, 'Dismissed request stays dismissed');
    request = {...request, nonce: 'two', snapshot: false};
    await page.locator('#solidcraft-return').waitFor({state: 'visible'});
    assert.equal(await page.locator('#solidcraft-snapshot').isVisible(), false);
    request = {...request, nonce: 'bad', url: 'javascript:alert(1)'};
    await page.locator('#solidcraft-return').waitFor({state: 'hidden'});
    assert.equal(await page.locator('#solidcraft-edit').getAttribute('href'), null);
    request = null;
    assert.equal(desktopLoads, 1, 'Desktop stream is not reloaded');
    console.log('SolidCraft browser: user-initiated outer tab, snapshot warning, safe labels, dismissal, URL rejection and unchanged desktop passed.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
