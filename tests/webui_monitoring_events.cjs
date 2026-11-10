'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const projects = {
  value: '', options: [],
  replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; }
};
const info = {textContent: ''};
const elements = new Map([['[name=project]', projects], ['pre', info]]);
const root = {querySelector(selector) {
  if (!elements.has(selector)) elements.set(selector, {value: '', addEventListener() {}});
  return elements.get(selector);
}};
const rows = [
  {id: 'p1', name: 'First', site: 'Site', state: 'ready'},
  {id: 'p2', name: 'Second', site: 'Site', state: 'ready'}
];
let reply = {items: rows};
const calls = [];
const context = {
  document: {getElementById: () => root, body: {dataset: {}}},
  window: {}, localStorage: {getItem: () => null, setItem() {}},
  Option: function(text, value) { this.text = text; this.value = value; },
  fetch: async (url, options) => {
    calls.push({url, options});
    return {ok: true, text: async () => JSON.stringify({data: reply})};
  }
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
(async () => {
  await new Promise(resolve => setImmediate(resolve)); // Initial automatic project refresh.
  assert.equal(projects.value, 'p1');
  projects.value = 'p2';
  const initialOptions = projects.options;
  for (const items of [[{id: 'event-1', kind: 'new_signal'}], []]) {
    reply = {items};
    await root.querySelector('[data-action=events]').onclick();
    assert.equal(calls.at(-1).url, '/api/v1/monitoring-projects/p2/events');
    assert.equal(projects.options, initialOptions);
    assert.equal(context.window.oriMonitoring.selected(), 'p2');
    assert.deepEqual(JSON.parse(info.textContent), reply);
    reply = {ready: true};
    await root.querySelector('[data-action=preflight]').onclick();
    assert.equal(calls.at(-1).url, '/api/v1/monitoring-projects/p2/preflight');
    assert.deepEqual(JSON.parse(info.textContent), reply);
  }
  reply = {items: []};
  await context.window.oriMonitoring.request('', {});
  assert.equal(calls.at(-1).options.method, 'POST');
  assert.equal(projects.options, initialOptions);
  assert.equal(projects.value, 'p2');
  reply = {items: [...rows].reverse()};
  await root.querySelector('[data-action=refresh]').onclick();
  assert.equal(projects.value, 'p2');
  assert.deepEqual(Array.from(projects.options, p => p.value), ['p2', 'p1']);
  reply = {items: [rows[0]]};
  await context.window.oriMonitoring.request();
  assert.equal(projects.value, 'p1');
  reply = {items: []};
  await context.window.oriMonitoring.request();
  assert.equal(projects.value, '');
  assert.equal(projects.options.length, 0);
  console.log('monitoring event checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
