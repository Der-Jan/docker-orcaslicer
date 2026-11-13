'use strict';
(() => {
  const toolbar = document.getElementById('panel-toolbar');
  const trigger = document.getElementById('panels-toggle');
  const actions = document.getElementById('toolbar-actions');
  const pin = document.getElementById('toolbar-pin');
  let pinned = false;
  let hovered = false;
  let keyboardFocus = false;
  let timer;
  try { pinned = localStorage.getItem('orca-toolbar-pinned') === 'true'; } catch {}

  function show() {
    clearTimeout(timer);
    actions.hidden = false;
    toolbar.classList.add('toolbar-expanded');
    trigger.setAttribute('aria-expanded', 'true');
  }
  function hide(returnFocus = false) {
    clearTimeout(timer);
    if (pinned) return;
    if (returnFocus) trigger.focus({preventScroll: true});
    actions.hidden = true;
    toolbar.classList.remove('toolbar-expanded');
    trigger.setAttribute('aria-expanded', 'false');
  }
  function updatePin() {
    pin.setAttribute('aria-pressed', String(pinned));
    pin.title = pin.ariaLabel = pinned ? 'Unpin toolbar' : 'Keep toolbar open';
    if (pinned) show();
  }
  toolbar.addEventListener('pointerenter', event => {
    hovered = true;
    if (event.pointerType !== 'touch') {
      clearTimeout(timer);
      timer = setTimeout(show, 150);
    }
  });
  toolbar.addEventListener('pointerleave', event => {
    hovered = false;
    if (event.pointerType === 'touch') return;
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!keyboardFocus || !actions.contains(document.activeElement)) hide(actions.contains(document.activeElement));
    }, 350);
  });
  toolbar.addEventListener('focusout', event => {
    if (!hovered && !toolbar.contains(event.relatedTarget)) hide();
  });
  trigger.addEventListener('click', () => {
    clearTimeout(timer);
    if (actions.hidden) show();
    else hide();
  });
  trigger.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    show();
    const buttons = actions.querySelectorAll('button');
    buttons[event.key === 'ArrowDown' ? 0 : buttons.length - 1].focus();
  });
  toolbar.addEventListener('keydown', event => {
    keyboardFocus = true;
    if (event.key === 'Escape' && !actions.hidden && !pinned) {
      event.preventDefault();
      event.stopPropagation();
      hide(true);
    }
  });
  toolbar.addEventListener('pointerdown', () => { keyboardFocus = false; });
  pin.addEventListener('click', () => {
    pinned = !pinned;
    try { localStorage.setItem('orca-toolbar-pinned', String(pinned)); } catch {}
    updatePin();
  });
  actions.addEventListener('click', event => {
    if (event.target.closest('.edge-tab')) hide(true);
  });
  document.addEventListener('pointerdown', event => {
    if (!toolbar.contains(event.target)) hide();
  });
  updatePin();
})();
