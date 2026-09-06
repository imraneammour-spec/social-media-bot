// Small DOM contract tests: no browser, network requests, or application server.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function script(name) {
  const html = fs.readFileSync(path.join(__dirname, '../dashboard/templates', name), 'utf8');
  return [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].at(-1)[1]
    .replace(/{{\s*t\('([^']*)'\)\|tojson\s*}}/g, (_, text) => JSON.stringify(text));
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

test('mode disclosure supports Prompt, Message, and Link without submitting irrelevant fields', () => {
  const document = {}, mode = element(document), delay = element(document);
  const privateMessage = element(document), destination = element(document);
  privateMessage.dataset.defaultDirectMessage = "Hey 👋 Thanks for commenting! I’ve got something for you.";
  privateMessage.dataset.defaultLink = "Hey 👋 Thanks for your interest! Your link is ready.";
  const panels = ['direct_message link', 'prompt_request', 'link'].map(value => ({ dataset: { deliveryMode: value } }));
  mode.value = 'prompt_request';
  document.getElementById = id => ({
    delivery_mode: mode,
    followup_delay_minutes: delay,
    private_message_text: privateMessage,
    destination_url: destination,
  })[id];
  document.querySelectorAll = () => panels;
  vm.runInNewContext(script('app_automations.html'), { document });
  assert.deepEqual(panels.map(panel => panel.hidden), [true, false, true]);
  assert.equal(delay.max, '10080');
  mode.value = 'direct_message'; mode.listeners.change();
  assert.deepEqual(panels.map(panel => panel.hidden), [false, true, true]);
  assert.equal(delay.max, '1439');
  assert.equal(privateMessage.value, privateMessage.dataset.defaultDirectMessage);
  assert.equal(destination.required, false);
  mode.value = 'prompt_request'; mode.listeners.change();
  privateMessage.value = '';
  mode.value = 'link'; mode.listeners.change();
  assert.deepEqual(panels.map(panel => panel.hidden), [false, true, false]);
  assert.equal(delay.max, '1439');
  assert.equal(privateMessage.value, privateMessage.dataset.defaultLink);
  assert.equal(destination.required, true);
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
