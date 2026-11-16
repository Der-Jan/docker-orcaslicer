'use strict';
(() => {
  const panel = document.getElementById('solidcraft-return');
  const link = document.getElementById('solidcraft-edit');
  let dismissed;
  document.getElementById('solidcraft-dismiss').addEventListener('click', () => {
    dismissed = panel.dataset.nonce;
    panel.hidden = true;
    link.removeAttribute('href');
  });
  async function poll() {
    try {
      const response = await fetch('solidcraft', {cache: 'no-store', signal: AbortSignal.timeout(5000)});
      if (!response.ok) throw new Error('Source status unavailable');
      const {request} = await response.json();
      if (!request || request.nonce === dismissed) {
        panel.hidden = true;
        link.removeAttribute('href');
        return;
      }
      const destination = new URL(request.url);
      if (!['http:', 'https:'].includes(destination.protocol) || destination.username || destination.password
          || destination.origin !== request.service || destination.pathname !== '/'
          || !destination.searchParams.has('craft-source')) throw new Error('Invalid source destination');
      link.href = destination.href;
      document.getElementById('solidcraft-model').textContent = request.object_name;
      document.getElementById('solidcraft-origin').textContent = `${request.name} · ${destination.origin}`;
      document.getElementById('solidcraft-snapshot').hidden = !request.snapshot;
      panel.dataset.nonce = request.nonce;
      panel.hidden = false;
    } catch (_) {
      panel.hidden = true;
      link.removeAttribute('href');
    } finally {
      setTimeout(poll, 1500);
    }
  }
  poll();
})();
