'use strict';
(() => {
  const panel = document.getElementById('nextcloud-panel');
  const toggle = document.getElementById('nextcloud-toggle');
  const form = document.getElementById('nextcloud-form');
  const address = document.getElementById('nextcloud-server');
  const connect = document.getElementById('nextcloud-connect');
  const status = document.getElementById('nextcloud-status');
  const login = document.getElementById('nextcloud-login');
  const cancel = document.getElementById('nextcloud-cancel');
  const disconnect = document.getElementById('nextcloud-disconnect');
  const picker = document.getElementById('nextcloud-folder-picker');
  const folderStatus = document.getElementById('nextcloud-folder-status');
  const folderList = document.getElementById('nextcloud-folders');
  const folderSelect = document.getElementById('nextcloud-folder-select');
  const folderUp = document.getElementById('nextcloud-folder-up');
  const syncStart = document.getElementById('nextcloud-sync-start');
  const syncStop = document.getElementById('nextcloud-sync-stop');
  const syncNow = document.getElementById('nextcloud-sync-now');
  const syncActivity = document.getElementById('nextcloud-sync-activity');
  const syncProgress = document.getElementById('nextcloud-sync-progress');
  let syncing = false;
  let folderAccount = null;
  let folderPath = '/';
  let folderGeneration = 0;
  let pending = false;
  let busy = false;
  let refreshing = false;
  let connected = false;
  function tabStatus(state, detail) {
    toggle.dataset.status = state;
    toggle.title = `Nextcloud: ${detail}`;
    toggle.setAttribute('aria-label', `Open Nextcloud panel. ${detail}`);
  }
  function open(value) {
    if (value) {
      const device = document.getElementById('printer-panel');
      if (!device.hidden) document.getElementById('close').click();
    }
    panel.hidden = !value;
    toggle.setAttribute('aria-expanded', String(value));
    document.body.classList.toggle('nextcloud-open', value);
    if (value) refresh();
  }
  async function api(action, data) {
    const response = await fetch(`nextcloud/${action}`, data === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Orca-Nextcloud': '1'},
      body: JSON.stringify(data), cache: 'no-store'
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Nextcloud connection is unavailable.');
    return result;
  }
  function formatBytes(value) {
    let size = Math.max(0, value || 0);
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
    return `${size.toLocaleString('en-GB', {maximumFractionDigits: unit ? 1 : 0})} ${units[unit]}`;
  }
  function renderSync(sync) {
    const phase = sync.phase || (sync.enabled ? 'checking' : 'disabled');
    if (!connected) tabStatus('error', 'Not connected');
    else if (phase === 'error') tabStatus('error', sync.message || 'Sync error');
    else if (['requested', 'preparing', 'checking', 'transferring', 'stopping'].includes(phase)) tabStatus('syncing', sync.message || 'Syncing');
    else tabStatus('ready', phase === 'waiting' ? sync.message || 'Waiting' : sync.enabled ? 'Up to date' : 'Connected. Sync is off');
    const labels = {disabled: 'Off', requested: 'Requested', preparing: 'Preparing first sync', checking: 'Checking for changes', transferring: 'Transferring files', waiting: 'Waiting', idle: 'Up to date', error: 'Error', stopping: 'Stopping'};
    syncActivity.dataset.phase = phase;
    document.getElementById('nextcloud-sync-phase').textContent = labels[phase] || 'Checking for changes';
    document.getElementById('nextcloud-sync-status').textContent = (sync.message || 'Sync is off.') +
      (sync.last_success ? ` Last successful sync: ${new Date(sync.last_success * 1000).toLocaleString('en-GB')}.` : '');
    const stats = sync.progress || {};
    syncProgress.hidden = !['requested', 'preparing', 'checking', 'transferring'].includes(phase);
    if (phase === 'transferring' && stats.totalBytes > 0) {
      syncProgress.value = Math.min(100, 100 * (stats.bytes || 0) / stats.totalBytes);
    } else {
      syncProgress.removeAttribute('value');
    }
    const details = [];
    if (!['requested', 'preparing', 'disabled'].includes(phase)) {
      if (stats.totalChecks) details.push(`${stats.checks || 0} / ${stats.totalChecks} files checked`);
      if (stats.totalTransfers) details.push(`${stats.transfers || 0} / ${stats.totalTransfers} files transferred`);
      if (stats.totalBytes) details.push(`${formatBytes(stats.bytes)} / ${formatBytes(stats.totalBytes)}`);
      if (stats.speed > 0 && sync.running) details.push(`${formatBytes(stats.speed)}/s`);
      if (stats.deletes) details.push(`${stats.deletes} deleted`);
      if (sync.running && sync.started_at) details.push(`${Math.max(0, Math.floor(Date.now() / 1000 - sync.started_at))}s elapsed`);
    }
    document.getElementById('nextcloud-sync-details').textContent = details.join(' · ');
    document.getElementById('nextcloud-sync').setAttribute('aria-busy', String(Boolean(sync.running || phase === 'requested')));
  }
  function render(data) {
    connected = data.connected;
    pending = data.pending;
    address.disabled = data.connected || data.pending || busy;
    connect.disabled = data.connected || data.pending || busy;
    disconnect.hidden = !data.connected;
    picker.hidden = !data.connected;
    const sync = data.sync || {};
    syncing = sync.enabled || sync.running;
    document.getElementById('nextcloud-sync').hidden = !data.connected || !data.folder;
    syncStart.hidden = sync.enabled;
    syncStart.disabled = !data.folder || data.folder === '/' || sync.running || busy;
    syncStop.hidden = !sync.enabled;
    syncNow.hidden = !sync.enabled;
    syncNow.disabled = sync.running || busy;
    renderSync(sync);
    if (data.folder === '/') document.getElementById('nextcloud-sync-status').textContent = 'Choose a dedicated Nextcloud subfolder for your projects.';
    folderSelect.disabled = syncing || folderSelect.dataset.loading === 'true';
    document.getElementById('nextcloud-selected-folder').textContent = data.folder ? `Selected folder: ${data.folder}` : 'Choose a folder for your projects.';
    const account = data.connected ? `${data.server}\n${data.user}` : null;
    if (account !== folderAccount) {
      folderAccount = account;
      folderGeneration++;
      folderList.replaceChildren();
      if (account) loadFolders(data.folder || '/');
    }
    cancel.hidden = !data.pending;
    if (!data.pending) { login.hidden = true; login.removeAttribute('href'); }
    if (data.server && document.activeElement !== address) address.value = data.server;
    status.textContent = data.connected ? `Connected to ${data.server} as ${data.user}.` : data.message || 'Not connected.';
    if (data.connected && data.message && data.message !== 'Connected to Nextcloud.') status.textContent += ` ${data.message}`;
  }
  async function loadFolders(path) {
    const generation = ++folderGeneration;
    folderSelect.disabled = true;
    folderSelect.dataset.loading = 'true';
    folderUp.disabled = true;
    folderList.replaceChildren();
    folderStatus.textContent = 'Loading folders…';
    try {
      const result = await api('folders', {path});
      if (generation !== folderGeneration || !folderAccount) return;
      folderPath = result.path;
      document.getElementById('nextcloud-folder-path').textContent = folderPath;
      folderUp.disabled = folderPath === '/';
      folderStatus.textContent = result.folders.length ? '' : 'No subfolders.';
      for (const folder of result.folders) {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.textContent = `▸ ${folder.name}`;
        button.addEventListener('click', () => loadFolders(folder.path));
        item.append(button);
        folderList.append(item);
      }
      folderSelect.dataset.loading = 'false';
      folderSelect.disabled = syncing;
    } catch (error) {
      folderSelect.dataset.loading = 'true';
      if (generation === folderGeneration) {
        folderStatus.textContent = `${error.message} Open the parent folder to try again.`;
        folderUp.disabled = false;
      }
    }
  }
  folderUp.addEventListener('click', () => loadFolders(folderPath.slice(0, folderPath.lastIndexOf('/')) || '/'));
  folderSelect.addEventListener('click', async () => {
    folderSelect.disabled = true;
    try {
      render(await api('folder', {path: folderPath}));
      folderStatus.textContent = 'Project folder saved.';
    } catch (error) { folderStatus.textContent = error.message; }
    finally { folderSelect.disabled = syncing; }
  });
  async function refresh() {
    if (refreshing || busy) return;
    refreshing = true;
    try { render(await api('status')); }
    catch {
      status.textContent = 'Connection status is currently unavailable.';
      tabStatus('error', 'Connection status is unavailable');
    }
    finally { refreshing = false; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || pending) return;
    // Open on the user gesture so browser popup blocking does not interrupt login.
    const popup = window.open('about:blank', '_blank');
    if (popup) popup.opener = null;
    busy = true;
    connect.disabled = true;
    address.disabled = true;
    status.textContent = 'Preparing sign-in…';
    try {
      const result = await api('start', {server: address.value.trim()});
      pending = true;
      login.href = result.login;
      login.hidden = false;
      if (popup) popup.location.replace(result.login);
    } catch (error) {
      if (popup) popup.close();
      status.textContent = error.message;
      connect.disabled = false;
      address.disabled = false;
      return;
    } finally { busy = false; }
    await refresh();
  });
  async function perform(action) {
    if (busy) return;
    busy = true;
    cancel.disabled = true;
    disconnect.disabled = true;
    syncStart.disabled = syncStop.disabled = syncNow.disabled = true;
    if (action === 'sync-now') {
      syncNow.textContent = 'Requesting…';
      renderSync({enabled: true, phase: 'requested', message: 'Requesting an immediate sync…'});
    }
    try { render(await api(action, {})); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; cancel.disabled = false; disconnect.disabled = false; syncStop.disabled = false; syncNow.textContent = 'Sync now'; }
    await refresh();
  }
  cancel.addEventListener('click', () => perform('cancel'));
  disconnect.addEventListener('click', () => perform('disconnect'));
  syncStart.addEventListener('click', () => perform('sync-start'));
  syncStop.addEventListener('click', () => perform('sync-stop'));
  syncNow.addEventListener('click', () => perform('sync-now'));
  toggle.addEventListener('click', () => open(panel.hidden));
  document.getElementById('nextcloud-close').addEventListener('click', () => { open(false); toggle.focus(); });
  document.addEventListener('orca-device-open', () => open(false));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !panel.hidden) { open(false); toggle.focus(); } });
  tabStatus('error', 'Connection status is unknown');
  refresh();
  setInterval(refresh, 2000);
})();
