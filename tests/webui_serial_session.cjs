const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fields = Object.fromEntries(['form', 'name', 'tinysa', 'receiver', 'submit', 'status'].map(name => [
  `serial-session-${name}`, {value: '', disabled: false, textContent: '', listeners: {},
    addEventListener(type, fn) {this.listeners[type] = fn;}},
]));
let calls = [], response, resolveRequest, refreshed = 0, expire;
const context = {
  document: {getElementById: id => fields[id] || {click() {refreshed++;}}},
  fetch: async (url, options) => {calls.push({url, options}); return response();},
  AbortController, setTimeout: callback => {expire = callback; return 1;}, clearTimeout() {},
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
  for (const port of ['COM12', 'com12', '\\\\.\\COM12', 'sim://ardv1', '',
                      'socket://localhost:1234', 'rfc2217://localhost:1234', '/dev/tty\x00USB0']) {
    fields['serial-session-receiver'].value = port;
    await submit(); assert.equal(calls.length, 1);
  }
  fields['serial-session-receiver'].value = 'COM13';
  response = () => {throw new Error('network disconnected');};
  await submit();
  assert.match(fields['serial-session-status'].textContent, /Обновите список/);
  response = () => new Promise((resolve, reject) => {
    calls.at(-1).options.signal.addEventListener('abort', () => reject(new Error('request aborted')));
  });
  const timedOut = submit();
  expire();
  await timedOut;
  assert.match(fields['serial-session-status'].textContent, /request aborted.*Обновите список/);
  assert.equal(fields['serial-session-submit'].disabled, false);
  response = () => ({ok: false, status: 400, json: async () => ({error: {message: 'bad port'}})});
  await submit();
  assert.match(fields['serial-session-status'].textContent, /bad port/);
  for (const body of [{}, null, {contract: 'wrong'}, {
    contract: 'openradioinspector-platform', contract_version: 1,
    data: {id: 'unexpected', state: 'running'},
  }]) {
    response = () => ({ok: true, json: async () => body});
    await submit();
    assert.match(fields['serial-session-status'].textContent, /Обновите список/);
    assert.equal(fields['serial-session-submit'].disabled, false);
    assert.equal(refreshed, 1);
  }
  response = () => ({ok: false, status: 502, json: async () => {throw new Error('invalid JSON');}});
  await submit();
  assert.match(fields['serial-session-status'].textContent, /Обновите список/);
  const count = calls.length;
  fields['serial-session-name'].value = 'x'.repeat(201);
  await submit();
  assert.equal(calls.length, count);
  fields['serial-session-name'].value = 'Приборы 日本語';
  fields['serial-session-tinysa'].value = '/dev/Прибор';
  fields['serial-session-receiver'].value = '/dev/прибор';
  response = success;
  await submit();
  assert.equal(JSON.parse(calls.at(-1).options.body).name, 'Приборы 日本語');
  assert.equal(refreshed, 2);
  console.log('serial session behavior checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
