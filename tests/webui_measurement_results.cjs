/* Execute production measurement-result handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

const CATALOG = [
  { methodology: 'beta2_bandwidth', title: 'Полоса β/2', original_table: 'MEAS_B2_RESULT',
    unit: 'Hz', fields: ['bandwidth_hz', 'lower_edge_hz', 'upper_edge_hz', 'center_hz'],
    result_codes: ['norm', 'not_norm', 'not_measured'] },
  { methodology: 'control_bandwidth', title: 'Контрольная полоса', original_table: 'MEAS_N1902_RESULTCBW',
    unit: 'Hz', fields: ['control_bandwidth_hz', 'norm_hz'], result_codes: ['norm', 'not_norm', 'not_measured'] },
];
function values(code = 'norm', methodology = 'beta2_bandwidth') {
  const spec = CATALOG.find(item => item.methodology === methodology);
  if (code === 'not_measured') return {};
  return Object.fromEntries(spec.fields.map((name, index) => [name, index + 1.0]));
}
function record(overrides = {}) {
  return {
    id: 'result/1', session_id: 'session/1', stream_id: 'tiny/1',
    methodology: 'beta2_bandwidth', result_code: 'norm', frequency_hz: 101800000,
    interval: { started_at: '2026-11-08T12:00:00Z', ended_at: '2026-11-08T12:00:01Z' },
    values: values(), created_at: '2026-11-08T12:00:02Z', ...overrides,
  };
}

function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map();
  let timerId = 0;
  const blobs = [], downloads = [], revoked = [];
  function element(id = '') {
    const result = {
      id, value: '', textContent: '', hidden: false, disabled: false, children: [], listeners: {},
      options: [],
      addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...items) {
        this.children = items; this.options = items;
        // A real <select> keeps its value only while it is still offered.
        if (items.length && !items.some(option => option.value === this.value)) {
          this.value = items[0].value ?? this.value;
        }
      },
      setAttribute(name, value) { this[name] = value; }, click() { downloads.push(this); }, remove() {},
    };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'save', 'methodology', 'code', 'frequency', 'started',
                      'ended', 'values', 'source', 'status', 'summary', 'items']) {
    dom[name] = element(`meas-${name}`);
  }
  dom.frequency.value = '101800000';
  dom.started.value = '2026-11-08T12:00:00Z';
  dom.ended.value = '2026-11-08T12:00:01Z';
  dom.values.value = JSON.stringify(values());
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('meas-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL(blob) { blobs.push(blob); return `blob:${blobs.length}`; }, revokeObjectURL(url) { revoked.push(url); } },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(sessionId = 'session/1', streamId = 'tiny/1') {
    window.OpenRadioMeasurementResults.update({ sessionId, streamId });
  }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 201, envelope = {}) {
    const body = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope }));
    requests[index].resolve({ status, ok: status >= 200 && status < 300,
                              headers: { get: () => String(body.length) }, text: async () => body.toString(),
                              json: async () => JSON.parse(body.toString()) });
  }
  return { dom, events, requests, timers, window, context, update, click, respond, blobs, downloads, revoked };
}

async function boot(app, records = []) {
  // update() fetches the catalog and the current results; answer both.
  await new Promise(resolve => setImmediate(resolve));
  const catalogIndex = app.requests.findIndex(r => r.url.includes('/measurement-catalog'));
  assert.notEqual(catalogIndex, -1, 'catalog is fetched');
  app.respond(catalogIndex, CATALOG, 200);
  await new Promise(resolve => setImmediate(resolve));
  const listIndex = app.requests.findIndex(r => r.url.includes('/measurement-results?limit=100'));
  assert.notEqual(listIndex, -1, 'results are fetched');
  app.respond(listIndex, records, 200);
  await new Promise(resolve => setImmediate(resolve));
}

async function happyPath() {
  const app = setup();
  app.update();
  await boot(app);
  assert.equal(app.dom.methodology.options.length, 2);
  assert.equal(app.dom.methodology.value, 'beta2_bandwidth');
  assert.equal(app.dom.run.disabled, false);
  const promise = app.click('run'); app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.filter(r => r.options?.method === 'POST').length, 1, 'single flight');
  const post = app.requests.find(r => r.options?.method === 'POST');
  assert.match(post.url, /\/api\/v1\/sessions\/session%2F1\/measurement-results$/);
  const sent = JSON.parse(post.options.body);
  assert.deepEqual(Object.keys(sent).sort(),
    ['frequency_hz', 'interval', 'methodology', 'result_code', 'values']);
  assert.equal(sent.methodology, 'beta2_bandwidth');
  assert.equal(sent.result_code, 'norm');
  assert.equal(sent.values.bandwidth_hz, 1);
  app.respond(app.requests.indexOf(post), record());
  await promise;
  assert.match(app.dom.status.textContent, /result\/1/);
  assert.match(app.dom.status.textContent, /norm\. /);
  assert.equal(app.dom.items.children.length, 1);
  assert.match(app.dom.items.children[0].textContent, /beta2_bandwidth.*norm.*101800000/);
  assert.equal(app.dom.save.disabled, false);
}

async function notMeasuredAndCatalogGuard() {
  const app = setup();
  app.update();
  await boot(app);
  app.dom.code.value = 'not_measured';
  app.dom.values.value = '{"bandwidth_hz": 1}';
  const bad = app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.filter(r => r.options?.method === 'POST').length, 0,
    'not_measured with values is refused before the request');
  assert.match(app.dom.status.textContent, /not_measured/);
  await bad;
  app.dom.values.value = '{}';
  const promise = app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  const post = app.requests.find(r => r.options?.method === 'POST');
  assert.deepEqual(JSON.parse(post.options.body).values, {});
  app.respond(app.requests.indexOf(post),
    record({ id: 'result/2', result_code: 'not_measured', values: {} }));
  await promise;
  assert.match(app.dom.items.children[0].textContent, /not_measured/);
  assert.match(app.dom.items.children[0].textContent, /без значений/);
  // The client refuses a response whose values contradict the code or the catalog.
  const forged = setup();
  forged.update();
  await boot(forged);
  const forgedRun = forged.click('run');
  await new Promise(resolve => setImmediate(resolve));
  const forgedPost = forged.requests.find(r => r.options?.method === 'POST');
  forged.respond(forged.requests.indexOf(forgedPost),
    record({ values: { bandwidth_hz: 1, lower_edge_hz: 1, upper_edge_hz: 1, center_hz: 1, extra: 1 } }));
  await forgedRun;
  assert.equal(forged.dom.save.disabled, true);
  assert.match(forged.dom.status.textContent, /не совпадают с методикой/);
}

async function transportAndScope() {
  for (const status of [400, 404, 409, 500]) {
    const app = setup();
    app.update();
    await boot(app);
    const promise = app.click('run');
    await new Promise(resolve => setImmediate(resolve));
    const post = app.requests.find(r => r.options?.method === 'POST');
    app.respond(app.requests.indexOf(post), {}, status);
    await promise;
    assert.equal(app.dom.save.disabled, true);
    assert.equal(app.dom.items.children.length, 0);
  }
  const lost = setup();
  lost.update();
  await boot(lost);
  lost.window.OpenRadioMeasurementResults.setConnected(false);
  assert.equal(lost.dom.run.disabled, true);
  assert.equal(lost.dom.save.disabled, true);
  // A session change refetches the catalog and results.
  const moved = setup();
  moved.update();
  await boot(moved, [record()]);
  assert.equal(moved.dom.items.children.length, 1);
  moved.update('session/2');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(moved.requests.filter(r => r.url.includes('/measurement-catalog')).length, 2);
}

(async () => {
  await happyPath();
  await notMeasuredAndCatalogGuard();
  await transportAndScope();
  const wiring = setup();
  const start = platformCode.indexOf('  function renderCandidates('), end = platformCode.indexOf('\n  }', start) + 4;
  assert(start >= 0, 'renderCandidates exists');
  assert.match(platformCode.slice(start, end), /OpenRadioMeasurementResults\?\.update/);
  for (const name of ['window.OpenRadioMeasurementResults?.setConnected(true)',
                      'window.OpenRadioMeasurementResults?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("meas-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('measurement-results behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
