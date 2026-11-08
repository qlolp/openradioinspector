const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = Object.fromEntries(['form', 'name', 'tinysa', 'receiver', 'submit', 'status'].map(name => [
  `serial-session-${name}`, {value: '', disabled: false, textContent: '', listeners: {},
    addEventListener(type, fn) {this.listeners[type] = fn;}},
]));
let calls = [], response, resolveRequest, refreshed = 0;
const context = {
  document: {getElementById: id => fields[id] || {click() {refreshed++;}}},
  fetch: async (url, options) => {calls.push({url, options}); return response();},
  AbortController, setTimeout, clearTimeout,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const submit = () => fields['serial-session-form'].listeners.submit({preventDefault() {}});
const success = () => ({ok: true, status: 201, json: async () => ({
  contract: 'openradioinspector-platform', contract_version: 1, data: {id: 'com-session', state: 'draft'},
})});
(async () => {
  await submit();
  assert.equal(calls.length, 0);
  fields['serial-session-name'].value = 'Windows core';
  fields['serial-session-tinysa'].value = 'COM12';
  fields['serial-session-receiver'].value = 'COM13';
  response = () => new Promise(resolve => {resolveRequest = resolve;});
  const pending = submit();
  assert.equal(fields['serial-session-submit'].disabled, true);
  await submit();
  assert.equal(calls.length, 1);
  resolveRequest(success()); await pending;
  assert.equal(fields['serial-session-submit'].disabled, false);
  assert.equal(refreshed, 1);
  const request = calls[0];
  assert.equal(request.url, '/api/v1/sessions');
  const payload = JSON.parse(request.options.body);
  assert.deepEqual(payload.streams.map(s => s.settings.adapter), ['tinysa-serial', 'ardv1-serial']);
  assert.deepEqual(payload.streams.map(s => s.level_unit), ['dBm', 's_meter_count']);
  assert.equal(payload.streams[1].capabilities.includes('hardware_decode'), false);
  assert.equal(calls.length, 1); // Creation never starts a session or worker.
  for (const port of ['COM12', 'sim://ardv1', '']) {
    fields['serial-session-receiver'].value = port;
    await submit(); assert.equal(calls.length, 1);
  }
  fields['serial-session-receiver'].value = 'COM13';
  response = () => {throw new Error('network disconnected');};
  await submit();
  assert.match(fields['serial-session-status'].textContent, /Обновите список/);
  assert.equal(fields['serial-session-submit'].disabled, false);
  response = () => ({ok: false, status: 400, json: async () => ({error: {message: 'bad port'}})});
  await submit();
  assert.match(fields['serial-session-status'].textContent, /bad port/);
  console.log('serial session behavior checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
