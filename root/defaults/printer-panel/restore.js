'use strict';
(() => {
  const button = document.getElementById('orca-restore');
  const status = document.getElementById('orca-restore-status');
  let timer;
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    clearTimeout(timer);
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    status.hidden = false;
    status.dataset.state = 'checking';
    status.textContent = 'Checking Orca…';
    try {
      const response = await fetch('restore', {
        method: 'POST', headers: {'Content-Type': 'application/json', 'X-Orca-Action': 'restore'},
        body: '{}', cache: 'no-store', signal: AbortSignal.timeout(35000)
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.message || 'Could not restore Orca.');
      status.dataset.state = 'ready';
      status.textContent = result.message;
    } catch (error) {
      status.dataset.state = 'error';
      status.textContent = error.name === 'TimeoutError'
        ? 'Recovery has not been confirmed yet. Wait a moment before trying again.'
        : error.message;
    } finally {
      button.disabled = false;
      button.setAttribute('aria-busy', 'false');
      timer = setTimeout(() => { status.hidden = true; }, 20000);
    }
  });
})();
