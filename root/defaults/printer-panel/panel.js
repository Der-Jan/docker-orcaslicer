'use strict';
const panel = document.getElementById('printer-panel');
const toggle = document.getElementById('printer-toggle');
let frame = document.getElementById('printer-frame');
const resize = document.getElementById('resize-handle');

const cameraViews = new WeakMap();
function bindDeviceCamera(deviceFrame) {
  let deviceDocument;
  try { deviceDocument = deviceFrame.contentDocument; } catch { return; }
  if (!deviceDocument || deviceDocument.documentElement.dataset.orcaCameraBound) return;
  deviceDocument.documentElement.dataset.orcaCameraBound = 'true';
  const cameraStyle = deviceDocument.createElement('style');
  cameraStyle.textContent = `
    html.orca-camera-view-open, html.orca-camera-view-open body { overflow: hidden !important; }
    .camera-container.orca-camera-view { position: fixed !important; inset: 0 !important; width: 100% !important; height: 100% !important; max-height: none !important; z-index: 2147483647; background: #000 !important; }
    .orca-camera-view .camera-image { display: block; width: 100%; height: 100%; max-width: 100%; max-height: 100%; object-fit: contain; }
    .orca-camera-return { position: absolute; top: 10px; left: 10px; z-index: 2; padding: 8px 12px; border: 1px solid #637476; border-radius: 6px; background: #252e32; color: #eef3f3; font: 13px system-ui, sans-serif; cursor: pointer; }
    .orca-camera-return:focus-visible { outline: 2px solid #19c5ae; }
  `;
  deviceDocument.head.append(cameraStyle);
  let activeCamera = null;
  let returnButton = null;
  let previousFocus = null;
  function closeCameraView() {
    if (!activeCamera) return false;
    activeCamera.classList.remove('orca-camera-view');
    deviceDocument.documentElement.classList.remove('orca-camera-view-open');
    returnButton.remove();
    activeCamera = null;
    previousFocus?.focus();
    return true;
  }
  cameraViews.set(deviceFrame, closeCameraView);
  deviceDocument.addEventListener('keydown', event => {
    if (event.key === 'Escape' && activeCamera) {
      event.preventDefault();
      event.stopImmediatePropagation();
      closeCameraView();
    }
  }, true);
  deviceDocument.addEventListener('click', event => {
    const control = event.target.closest?.('.camera-fullscreen, .orca-camera-return');
    const camera = control?.closest('.camera-container');
    if (!camera) return;
    // Keep QIDI's site-root navigation inside our embedded camera view.
    event.preventDefault();
    event.stopImmediatePropagation();
    if (closeCameraView()) return;
    previousFocus = deviceDocument.activeElement;
    activeCamera = camera;
    camera.classList.add('orca-camera-view');
    deviceDocument.documentElement.classList.add('orca-camera-view-open');
    returnButton = deviceDocument.createElement('button');
    returnButton.type = 'button';
    returnButton.className = 'orca-camera-return';
    returnButton.textContent = 'Back to Device';
    camera.append(returnButton);
    returnButton.focus({preventScroll: true});
  }, true);
}
function watchDeviceFrame(deviceFrame) {
  deviceFrame.addEventListener('load', () => bindDeviceCamera(deviceFrame));
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
  document.getElementById('mode-label').textContent = pinned ? 'Pinned beside the slicer' : 'Overlay on the slicer';
}
function openPanel(open) {
  if (open) document.dispatchEvent(new Event('orca-device-open'));
  panel.hidden = !open;
  toggle.setAttribute('aria-expanded', String(open));
  toggle.title = open ? 'Close Device panel' : 'Open Device panel';
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
  if (!cameraViews.get(frame)?.() && !panel.hidden) openPanel(false);
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
    status.textContent = 'Printer profile is currently unavailable.';
    status.hidden = false;
  }
}
updatePrinter();
setInterval(updatePrinter, 2000);
setWidth(width); updatePin();
