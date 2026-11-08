/* Execute production DVB-T handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

const SENT = {
  parameters: { mer_db: 20, ber_after_viterbi: 1e-6, ber_after_rs: 0 },
  thresholds: { min_mer_db: 15, max_ber_after_viterbi: 1e-3, max_ber_after_rs: 1e-9 },
  guard_interval: '1/4', code_rate: '2/3',
};
function result(code = 'norm') {
  const verdicts = code === 'norm'
    ? { mer_db: true, ber_after_viterbi: true, ber_after_rs: true }
    : { mer_db: false, ber_after_viterbi: true, ber_after_rs: true };
  return {
    session_id: 'session/1', methodology: 'dvb_t_parameters', result_code: code,
    guard_interval: SENT.guard_interval, code_rate: SENT.code_rate,
    parameters: SENT.parameters, thresholds: SENT.thresholds, verdicts,
    values: { ...SENT.parameters, guard_interval_ratio: 0.25, code_rate_ratio: 2 / 3 },
  };
}

function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map();
  let timerId = 0;
  function element(id = '') {
    const result = {
      id, value: '', textContent: '', hidden: false, disabled: false, children: [], listeners: {},
      options: [],
      addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...items) { this.children = items; this.options = items; },
      setAttribute(name, value) { this[name] = value; }, click() {}, remove() {},
    };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'mer', 'bertv', 'berrs', 'minmer', 'maxtv', 'maxrs',
                      'guard', 'code', 'source', 'status', 'summary', 'items']) {
    dom[name] = element(`dvb-${name}`);
  }
  dom.mer.value = '20'; dom.bertv.value = '0.000001'; dom.berrs.value = '0';
  dom.minmer.value = '15'; dom.maxtv.value = '0.001'; dom.maxrs.value = '1e-9';
  dom.guard.value = '1/4'; dom.code.value = '2/3';
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('dvb-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(sessionId = 'session/1') {
    window.OpenRadioDvbEvaluation.update({ sessionId });
  }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const body = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope }));
    requests[index].resolve({ status, ok: status >= 200 && status < 300,
                              headers: { get: () => String(body.length) }, text: async () => body.toString() });
  }
  return { dom, events, requests, timers, window, context, update, click, respond };
}

async function happyPath() {
  const app = setup();
  app.update();
  assert.equal(app.dom.run.disabled, false);
  const promise = app.click('run'); app.window.OpenRadioDvbEvaluation.setConnected(true); app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.length, 1, 'single flight');
  assert.match(app.requests[0].url, /\/api\/v1\/sessions\/session%2F1\/dvb-evaluation$/);
  const sent = JSON.parse(app.requests[0].options.body);
  assert.deepEqual(Object.keys(sent).sort(), ['code_rate', 'guard_interval', 'parameters', 'thresholds']);
  assert.deepEqual(sent.parameters, SENT.parameters);
  assert.deepEqual(sent.thresholds, SENT.thresholds);
  app.respond(0, result());
  await promise;
  assert.match(app.dom.summary.textContent, /Код результата: norm/);
  assert.match(app.dom.summary.textContent, /не нормативное заключение/);
  assert.match(app.dom.summary.textContent, /dvb_t_parameters/);
  assert.equal(app.dom.items.children.length, 3);
  assert.match(app.dom.items.children[0].textContent, /mer_db.*в норме/);
  assert.match(app.dom.status.textContent, /демодуляция IQ/i);
  // not_norm is rendered per parameter.
  const bad = setup();
  bad.update();
  const promiseBad = bad.click('run');
  await new Promise(resolve => setImmediate(resolve));
  bad.respond(0, result('not_norm'));
  await promiseBad;
  assert.match(bad.dom.summary.textContent, /Код результата: not_norm/);
  assert.match(bad.dom.items.children[0].textContent, /вне нормы/);
}

async function guards() {
  // Bad numbers are refused before any request.
  for (const change of [{ mer: 'abc' }, { bertv: '' }, { minmer: 'NaN' }]) {
    const app = setup();
    app.update();
    for (const [key, value] of Object.entries(change)) app.dom[key].value = value;
    await app.click('run');
    assert.equal(app.requests.length, 0, JSON.stringify(change));
  }
  // Forged responses never render.
  const forged = setup();
  forged.update();
  const pending = forged.click('run');
  await new Promise(resolve => setImmediate(resolve));
  forged.respond(0, result({ ...result(), parameters: { mer_db: 99 } }));
  await pending;
  assert.equal(forged.dom.items.children.length, 0);
  assert.match(forged.dom.status.textContent, /Не удалось/);
  const wrongValues = setup();
  wrongValues.update();
  const pendingWrong = wrongValues.click('run');
  await new Promise(resolve => setImmediate(resolve));
  wrongValues.respond(0, { ...result(), values: { mer_db: 1 } });
  await pendingWrong;
  assert.equal(wrongValues.dom.items.children.length, 0);
  for (const status of [400, 404, 409, 500]) {
    const app = setup();
    app.update();
    const promise = app.click('run');
    await new Promise(resolve => setImmediate(resolve));
    app.respond(0, {}, status);
    await promise;
    assert.equal(app.dom.items.children.length, 0);
  }
  // Connection loss disables the panel without dropping the inputs.
  const lost = setup();
  lost.update();
  lost.window.OpenRadioDvbEvaluation.setConnected(false);
  assert.equal(lost.dom.run.disabled, true);
  assert.equal(lost.dom.mer.value, '20');
}

(async () => {
  await happyPath();
  await guards();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioDvbEvaluation\?\.update/);
  for (const name of ['window.OpenRadioDvbEvaluation?.setConnected(true)',
                      'window.OpenRadioDvbEvaluation?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("dvb-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('dvb-evaluation behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
