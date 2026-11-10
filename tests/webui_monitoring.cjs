'use strict';
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const code = fs.readFileSync('radioinspector/web/static/monitoring.js', 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const project = id => ({id, name: id, site: 'site', state: 'draft'});
function harness(storage = {getItem: () => null, setItem: () => {}}) {
  const projects = {value: '', options: [], replaceChildren(...options) {
    this.options = options; this.value = options[0]?.value || '';
  }};
  const workspace = {addEventListener(type, fn) {this[type] = fn;}};
  const info = {}, refresh = {}, preflight = {}, form = {};
  const controls = {'[name=project]': projects, pre: info, '[name=workspace]': workspace,
    '[data-action=refresh]': refresh, '[data-action=preflight]': preflight, form};
  const root = {querySelector: name => controls[name]}, body = {dataset: {}};
  const calls = [];
  let responder = () => ({items: [], next_cursor: null});
  const context = {document: {body, getElementById: id => id === 'monitoring-workspace' ? root : {value: 'session'}},
    window: {}, localStorage: storage, Option: function(label, value) {this.label = label; this.value = value;},
    FormData: class {get(name) {return name;}}, fetch: async (path, options) => {
      calls.push({path, options});
      const result = await responder(path, options);
      return {ok: true, text: async () => JSON.stringify({data: result})};
    }};
  vm.runInNewContext(code, context);
  return {projects, workspace, info, refresh, preflight, form, body, calls,
    setResponder(fn) {responder = fn;}, request: (...args) => context.window.oriMonitoring.request(...args)};
}
(async () => {
  for (const storage of [
    {getItem() {throw Error('SecurityError');}, setItem() {throw Error('QuotaExceededError');}},
    {getItem: () => 'unknown-mode', setItem() {throw Error('SecurityError');}}
  ]) {
    const h = harness(storage); await flush();
    assert.equal(h.body.dataset.workspaceMode, 'research');
    assert.equal(h.workspace.value, 'research');
    assert.equal(h.calls.length, 1);
    assert.equal(typeof h.refresh.onclick, 'function');
    assert.equal(typeof h.preflight.onclick, 'function');
    assert.equal(typeof h.form.onsubmit, 'function');
    h.workspace.change({target: {value: 'monitoring'}});
    assert.equal(h.body.dataset.workspaceMode, 'monitoring');
    let prevented = false;
    h.setResponder((path, options) => options.method === 'POST' ? project('new') : {items: [project('new')], next_cursor: null});
    await h.form.onsubmit({preventDefault() {prevented = true;}, target: {}});
    assert(prevented);
    assert.equal(h.calls.at(-2).options.method, 'POST');
    assert.equal(h.projects.value, 'new');
  }
  const h = harness(); await flush();
  const first = Array.from({length: 200}, (_, i) => project(`p${String(i).padStart(3, '0')}`));
  const cursor = 'p199 &/?';
  h.setResponder(path => path.endsWith(encodeURIComponent(cursor)) ?
    {items: [project('z-created')], next_cursor: 'z-created'} :
    path.endsWith('z-created') ? {items: [], next_cursor: null} : {items: first, next_cursor: cursor});
  await h.request();
  assert.equal(h.projects.options.length, 201);
  assert(h.calls.some(c => c.path === '/api/v1/monitoring-projects?after=' + encodeURIComponent(cursor)));
  h.projects.value = 'z-created'; await h.refresh.onclick();
  assert.equal(h.projects.value, 'z-created');
  const original = h.projects.options;
  h.setResponder(path => {
    if (path.includes('?')) throw Error('later page failed');
    return {items: [project('a')], next_cursor: 'a'};
  });
  await h.request();
  assert.equal(h.projects.options, original);
  assert.equal(h.projects.value, 'z-created');
  assert.equal(h.info.textContent, 'later page failed');
  h.setResponder(() => ({items: [project('a')], next_cursor: 'a'}));
  const before = h.calls.length; await h.request();
  assert.equal(h.calls.length - before, 2);
  assert.equal(h.projects.options, original);
  assert.match(h.info.textContent, /курсор/);
  h.setResponder(() => ({items: [], next_cursor: null}));
  await h.request(); assert.equal(h.projects.options.length, 0);
  console.log('Monitoring WebUI: pagination, atomic refresh, cursor guard, and storage failures passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
