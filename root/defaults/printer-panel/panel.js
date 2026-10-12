'use strict';
const panel = document.getElementById('printer-panel');
const toggle = document.getElementById('printer-toggle');
let frame = document.getElementById('printer-frame');
const resize = document.getElementById('resize-handle');

function bindDeviceFullscreen(deviceFrame) {
  let deviceDocument;
  try { deviceDocument = deviceFrame.contentDocument; } catch { return; }
  if (!deviceDocument || deviceDocument.documentElement.dataset.orcaFullscreenBound) return;
  deviceDocument.documentElement.dataset.orcaFullscreenBound = 'true';
  const cameraStyle = deviceDocument.createElement('style');
  cameraStyle.textContent = '.camera-container:fullscreen .camera-image { display: block; width: 100%; height: 100%; max-width: 100%; max-height: 100%; object-fit: contain; }';
  deviceDocument.head.append(cameraStyle);
  deviceDocument.addEventListener('keydown', event => {
    if (event.key === 'Escape' && deviceDocument.fullscreenElement?.matches('.camera-container')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      deviceDocument.exitFullscreen().catch(() => {});
    }
  }, true);
  deviceDocument.addEventListener('click', event => {
    const control = event.target instanceof deviceFrame.contentWindow.Element
      ? event.target.closest('.camera-fullscreen') : null;
    const camera = control?.closest('.camera-container');
    if (!camera || !camera.requestFullscreen) return;
    // QIDI's camera button navigates to the site root, which hosts our desktop.
    // Handle the embedded camera locally with the browser Fullscreen API.
    event.preventDefault();
    event.stopImmediatePropagation();
    const action = deviceDocument.fullscreenElement === camera
      ? deviceDocument.exitFullscreen() : camera.requestFullscreen();
    action.catch(() => { control.title = 'Kamera-Vollbild ist im Browser nicht verfügbar.'; });
  }, true);
}
function watchDeviceFrame(deviceFrame) {
  deviceFrame.addEventListener('load', () => bindDeviceFullscreen(deviceFrame));
}
watchDeviceFrame(frame);
let width = 620;
let pinned = false;
try { width = Number(localStorage.getItem('orca-panel-width')) || 620; pinned = localStorage.getItem('orca-panel-pinned') === 'true'; } catch {}
function setWidth(value) {
  width = Math.min(Math.max(Math.min(360, window.innerWidth - 44), value), window.innerWidth - 44);
  document.documentElement.style.setProperty('--panel-width', `${width}px`);
  resize.setAttribute('aria-valuenow', String(Math.round(width)));
  resize.setAttribute('aria-valuemax', String(window.innerWidth - 44));
}
function save() { try { localStorage.setItem('orca-panel-width', String(width)); localStorage.setItem('orca-panel-pinned', String(pinned)); } catch {} }
function updatePin() {
  document.body.classList.toggle('pinned', pinned);
  document.getElementById('pin').setAttribute('aria-pressed', String(pinned));
  document.getElementById('mode-label').textContent = pinned ? 'Neben dem Slicer angeheftet' : 'Über dem Slicer';
}
function openPanel(open) {
  panel.hidden = !open;
  toggle.setAttribute('aria-expanded', String(open));
  toggle.title = open ? 'Device-Ansicht schließen' : 'Device-Ansicht öffnen';
  document.getElementById('tab-arrow').textContent = open ? '›' : '‹';
  document.body.classList.toggle('panel-open', open);
  if (open && printerReady && frame.getAttribute('src') === 'about:blank') frame.src = 'device/';
  if (!open) toggle.focus();
}
toggle.addEventListener('click', () => openPanel(panel.hidden));
document.getElementById('close').addEventListener('click', () => openPanel(false));
document.getElementById('pin').addEventListener('click', () => { pinned = !pinned; updatePin(); save(); });
document.getElementById('expand').addEventListener('click', () => { setWidth(width > window.innerWidth * .6 ? 620 : window.innerWidth * .8); save(); });
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  if (document.fullscreenElement === frame) frame.contentDocument.exitFullscreen().catch(() => {});
  else if (!panel.hidden) openPanel(false);
});
resize.addEventListener('pointerdown', event => {
  if (event.button !== 0) return;
  event.preventDefault(); resize.setPointerCapture(event.pointerId); document.body.classList.add('resizing');
});
resize.addEventListener('pointermove', event => { if (resize.hasPointerCapture(event.pointerId)) setWidth(window.innerWidth - event.clientX); });
function endResize() { document.body.classList.remove('resizing'); save(); }
resize.addEventListener('pointerup', endResize);
resize.addEventListener('pointercancel', endResize);
resize.addEventListener('lostpointercapture', endResize);
resize.addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); setWidth(width + (event.key === 'ArrowLeft' ? 24 : -24)); save(); }
});
window.addEventListener('resize', () => setWidth(width));
let printerReady = false;
let printerRevision = null;
async function updatePrinter() {
  try {
    const response = await fetch('printer.json', {cache: 'no-store'});
    if (!response.ok) throw new Error('profile');
    const printer = await response.json();
    document.getElementById('printer-name').textContent = printer.name || 'Device';
    const status = document.getElementById('printer-status');
    status.textContent = printer.message || '';
    status.hidden = printer.ready;
    printerReady = printer.ready;
    if (printerRevision !== printer.revision) {
      // Destroy the old iframe so its sockets and camera streams are disconnected.
      const replacement = frame.cloneNode(false);
      watchDeviceFrame(replacement);
      replacement.src = printer.ready && !panel.hidden ? 'device/' : 'about:blank';
      frame.replaceWith(replacement);
      frame = replacement;
      printerRevision = printer.revision;
    }
    if (printer.ready && !panel.hidden && frame.getAttribute('src') === 'about:blank') frame.src = 'device/';
  } catch {
    printerReady = false;
    frame.src = 'about:blank';
    printerRevision = null;
    const status = document.getElementById('printer-status');
    status.textContent = 'Druckerprofil momentan nicht verfügbar.';
    status.hidden = false;
  }
}
updatePrinter();
setInterval(updatePrinter, 2000);
setWidth(width); updatePin();
