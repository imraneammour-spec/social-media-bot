// Small DOM contract tests: no browser, network requests, or application server.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function script(name) {
  const html = fs.readFileSync(path.join(__dirname, '../dashboard/templates', name), 'utf8');
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1]
    .replace(/{{\s*t\((['"])(.*?)\1\)(?:\|tojson)?\s*}}/g, (_, quote, text) => JSON.stringify(text));
}
function element(document) {
  return {
    hidden: false, inert: false, value: '', dataset: {}, attributes: {}, listeners: {},
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    setCustomValidity(text) { this.validityMessage = text; },
    focus() { document.activeElement = this; },
  };
}

test('mobile menu closes with backdrop/Escape, traps focus, and resets on desktop', () => {
  const handlers = {}, document = { addEventListener: (type, fn) => { handlers[type] = fn; } };
  const classes = new Set();
  document.body = { classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name) } };
  const toggle = element(document), backdrop = element(document), sidebar = element(document), main = element(document);
  const last = element(document);
  sidebar.querySelectorAll = () => [last];
  sidebar.contains = value => value === last;
  document.querySelector = selector => ({ '.mobile-nav-toggle': toggle, '.mobile-nav-backdrop': backdrop, '.app-sidebar': sidebar, '.app-main': main })[selector];
  const mobile = { matches: true, addEventListener: (_, fn) => { mobile.change = fn; } };
  const window = { matchMedia: () => mobile, confirm: () => false };
  vm.runInNewContext(script('app_base.html'), { document, window });
  assert.equal(sidebar.inert, true);
  assert.equal(backdrop.hidden, true);
  toggle.listeners.click();
  assert.equal(main.inert, true);
  assert.equal(sidebar.inert, false);
  assert.equal(backdrop.hidden, false);
  assert.equal(toggle.attributes['aria-label'], 'Close navigation');
  last.focus();
  let prevented = false;
  handlers.keydown({ key: 'Tab', preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(document.activeElement, toggle);
  backdrop.listeners.click();
  assert.equal(main.inert, false);
  assert.equal(document.activeElement, toggle);
  toggle.listeners.click();
  handlers.keydown({ key: 'Escape', preventDefault() {} });
  assert.equal(backdrop.hidden, true);
  toggle.listeners.click();
  mobile.matches = false; mobile.change();
  assert.equal(sidebar.inert, false);
  assert.equal(main.inert, false);
  assert.equal(classes.has('mobile-nav-open'), false);
  last.focus(); mobile.matches = true; mobile.change();
  assert.equal(document.activeElement, toggle);
  let cancelled = false;
  handlers.submit({ submitter: { dataset: { confirm: 'Cancel?' } }, preventDefault() { cancelled = true; } });
  assert.equal(cancelled, true);
  const css = fs.readFileSync(path.join(__dirname, '../dashboard/static/tenant_dashboard.css'), 'utf8');
  assert.match(css, /\.mobile-nav-backdrop:not\(\[hidden\]\)\{display:block\}/);
  assert.match(css, /html\[dir="rtl"\]/);
});

test('landing mobile menu closes accessibly and resets outside mobile width', () => {
  const handlers = {}, document = { addEventListener: (type, fn) => { handlers[type] = fn; } };
  const classes = new Set();
  document.body = { classList: { toggle: (name, on) => on ? classes.add(name) : classes.delete(name) } };
  const toggle = element(document), panel = element(document), link = element(document);
  toggle.attributes['aria-expanded'] = 'false';
  toggle.dataset = { openLabel: 'Close navigation', closedLabel: 'Open navigation' };
  panel.dataset.open = 'false';
  panel.querySelectorAll = () => [link];
  panel.contains = value => value === link;
  document.querySelector = selector => ({ '.nav-toggle': toggle, '.nav-panel': panel })[selector];
  const mobile = { matches: true, addEventListener: (_, fn) => { mobile.change = fn; } };
  const window = { matchMedia: () => mobile };

  vm.runInNewContext(script('landing.html'), { document, window });

  assert.equal(typeof handlers.keydown, 'function');
  assert.equal(panel.inert, true);
  toggle.listeners.click();
  assert.equal(panel.dataset.open, 'true');
  assert.equal(panel.inert, false);
  assert.equal(toggle.attributes['aria-label'], 'Close navigation');
  assert.equal(classes.has('landing-menu-open'), true);
  handlers.keydown({ key: 'Escape', preventDefault() {} });
  assert.equal(panel.dataset.open, 'false');
  assert.equal(document.activeElement, toggle);
  toggle.listeners.click();
  link.listeners.click();
  assert.equal(panel.dataset.open, 'false');
  toggle.listeners.click();
  mobile.matches = false;
  mobile.change();
  assert.equal(toggle.attributes['aria-expanded'], 'false');
  assert.equal(panel.inert, false);
  assert.equal(classes.has('landing-menu-open'), false);

  const css = fs.readFileSync(path.join(__dirname, '../dashboard/static/landing.css'), 'utf8');
  assert.match(css, /\.skip-link:focus/);
  assert.match(css, /\.landing-menu-open\{overflow:hidden\}/);
});

test('mode disclosure switches Message and Link panels without erasing either modes edits', () => {
  const document = {}, mode = element(document), destination = element(document);
  const messageOpening = element(document), linkOpening = element(document);
  messageOpening.value = 'Saved Message opening';
  linkOpening.value = 'Saved Link opening';
  const panels = [
    { dataset: { modePanel: 'direct_message' } },
    { dataset: { modePanel: 'link' } },
    { dataset: { modeDescription: 'direct_message' } },
    { dataset: { modeDescription: 'link' } },
  ];
  mode.value = 'direct_message';
  document.getElementById = id => ({
    delivery_mode: mode,
    link_destination_url: destination,
    message_private_message_text: messageOpening,
    link_private_message_text: linkOpening,
  })[id];
  document.querySelectorAll = () => panels;
  vm.runInNewContext(script('app_automations.html'), { document });
  assert.deepEqual(panels.map(panel => panel.hidden), [false, true, false, true]);
  assert.equal(destination.required, false);
  mode.value = 'link'; mode.listeners.change();
  assert.deepEqual(panels.map(panel => panel.hidden), [true, false, true, false]);
  assert.equal(destination.required, true);
  assert.equal(messageOpening.value, 'Saved Message opening');
  assert.equal(linkOpening.value, 'Saved Link opening');
});

test('preview rejects unsafe syntax and reports loading/success/failure without fetching in the test', () => {
  const document = {}, ids = Object.fromEntries(['image-source', 'image-preview', 'image-preview-empty', 'image-preview-status'].map(id => [id, element(document)]));
  document.getElementById = id => ids[id];
  let pending;
  vm.runInNewContext(script('app_content.html'), { document, URL, setTimeout: fn => { pending = fn; }, clearTimeout: () => { pending = null; } });
  const source = ids['image-source'], preview = ids['image-preview'], status = ids['image-preview-status'];
  for (const url of ['http://example.com/a.jpg', 'https://user:pass@example.com/a.jpg', 'https://127.0.0.1/a.jpg', 'https://localhost/a.jpg']) {
    source.value = url; source.listeners.input();
    assert.ok(source.validityMessage);
    assert.equal(pending, null);
  }
  source.value = 'https://images.example.com/a.jpg'; source.listeners.input();
  assert.equal(source.validityMessage, '');
  pending();
  assert.equal(preview.src, source.value);
  assert.match(status.textContent, /Loading/);
  preview.listeners.load();
  assert.equal(preview.hidden, false);
  preview.listeners.error();
  assert.equal(preview.hidden, true);
  assert.match(status.textContent, /could not load/);
});

test('narrow account layouts and long identity labels cannot force horizontal overflow', () => {
  const css = fs.readFileSync(path.join(__dirname, '../dashboard/static/tenant_dashboard.css'), 'utf8');
  assert.match(css, /overflow-wrap:anywhere/);
  assert.match(css, /@media\(max-width:620px\).*\.account-grid\{grid-template-columns:minmax\(0,1fr\)\}/s);
});
