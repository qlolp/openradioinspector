/* Real production catalog and comparison handlers, controlled transport/lifecycle. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const time = '2026-11-08T12:30:00Z';
function sweep(id, stream = 'tiny/1') {
  return { id, stream_id: stream, level_unit: 'dBm', interval: { started_at: time, ended_at: time },
    frequencies_hz: [100, 200], levels: [-50, -40], settings: { rbw: 1000 } };
}
function record(name = 'Фон', sweepId = 'old-outside-window', stream = 'tiny/1') {
  return { id: `ref-${name}`, name, session_id: 'session/1', stream_id: stream,
    sweep_id: sweepId, created_at: time, interval: { started_at: time, ended_at: time }, level_unit: 'dBm', points: 2 };
}
function catalog(items = [], persistent = true, stream = 'tiny/1') {
  return { session_id: 'session/1', stream_id: stream, storage_mode: persistent ? 'sqlite' : 'memory', persistent, limit: 100, items };
}
function comparison(ref, target) {
  return { session_id: 'session/1', stream_id: 'tiny/1', reference: sweep(ref), target: sweep(target),
    frequencies_hz: [100, 200], delta_levels: [0, 0], delta_unit: 'dB', points: 2,
    max_increase: { frequency_hz: 100, delta_db: 0 }, min_change: { frequency_hz: 100, delta_db: 0 } };
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map(); let timerId = 0;
  const context2d = new Proxy({}, { get(target, key) { return target[key] || (() => {}); } });
  for (const id of ['reference-select', 'pin', 'run', 'clear', 'reference', 'status', 'summary', 'canvas',
    'name', 'save', 'catalog', 'refresh', 'use', 'storage', 'catalog-status', 'catalog-notice']) {
    dom[id] = { textContent: '', value: '', hidden: false, disabled: false, width: 960, height: 260,
      listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; },
      setAttribute(name, value) { this[name] = value; }, getContext() { return context2d; } };
    Object.defineProperty(dom[id], 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
  }
  const window = { location: { origin: 'http://server-a' }, addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) { return dom[id.replace('comparison-', '')]; } },
    Option: function(text, value) { this.textContent = text; this.value = value; }, URLSearchParams, AbortController,
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  const update = (sweeps = [sweep('current')], sessionId = 'session/1', streamId = 'tiny/1') =>
    window.OpenRadioSweepComparison.update({ sessionId, streamId, sweeps });
  const click = id => dom[id].listeners.click();
  const respond = (index, data, status = 200, envelope = true) => requests[index].resolve({ ok: status >= 200 && status < 300, status,
    json: async () => envelope ? { contract: 'openradioinspector-platform', contract_version: 1, data } : data });
  async function loaded(items = [], persistent = true) { update(); respond(0, catalog(items, persistent)); await flush(); }
  return { dom, window, events, requests, timers, update, click, respond, loaded };
}
async function catalogAndComparison() {
  const app = setup(), hostile = '<img onerror=alert(1)> & "фон"';
  await app.loaded([record(hostile)]);
  assert.equal(app.requests[0].options.method, 'GET');
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-references');
  assert.equal(url.searchParams.get('stream_id'), 'tiny/1');
  assert(app.dom.catalog.options[0].textContent.includes(hostile));
  assert.match(app.dom.storage.textContent, /SQLite.*перезапуска/);
  for (let n = 0; n < 25; n++) app.update();
  assert.equal(app.requests.length, 1, 'polling never refreshes catalog or POSTs');
  app.click('use');
  assert(app.dom.reference.textContent.includes(hostile));
  assert.match(app.dom.reference.textContent, /вне окна/);
  const pending = app.click('run');
  const query = new URL(app.requests[1].url, app.window.location.origin).searchParams;
  assert.equal(query.get('reference_id'), 'old-outside-window', 'use sweep_id, never record id');
  assert.equal(query.get('target_id'), 'current');
  app.respond(1, comparison('old-outside-window', 'current')); await pending;
  assert.equal(app.dom.canvas.hidden, false);
  app.click('clear'); app.click('pin');
  const current = app.click('run'); app.respond(2, comparison('current', 'current')); await current;
  assert.equal(app.dom.canvas.hidden, false, 'fresh local reference remains usable');
  const refresh = app.click('refresh'); app.click('refresh');
  assert.equal(app.requests.length, 4, 'single flight catalog refresh');
  app.respond(3, catalog([], false)); await refresh;
  assert.match(app.dom.storage.textContent, /Память.*теряется/);
  assert.equal(app.dom.use.disabled, true);
  const transient = app.click('refresh');
  app.respond(4, { ...catalog([], false), storage_mode: 'sqlite' }); await transient;
  assert.match(app.dom.storage.textContent, /SQLite в памяти.*временный.*теряется/);
}
async function saveAndNames() {
  for (const [input, expected] of [['  e\u0301 фон  ', 'é фон'], ['🛰'.repeat(80), '🛰'.repeat(80)], ['\u3000Фон\u2029', 'Фон']]) {
    const app = setup(); await app.loaded(); app.click('pin'); app.dom.name.value = input;
    const save = app.click('save'); app.click('save');
    assert.equal(app.requests.length, 2, 'repeat click must not duplicate POST');
    assert.equal(app.requests[1].options.method, 'POST');
    assert(!app.requests[1].url.includes('?'), 'POST accepts no query parameters');
    assert.deepEqual(JSON.parse(app.requests[1].options.body), { name: expected, sweep_id: 'current' });
    assert.equal(app.requests[1].options.headers['Content-Type'], 'application/json');
    app.respond(1, record(expected, 'current'), 201); await save;
    assert.match(app.dom['catalog-status'].textContent, /Сохранено/);
    app.click('save'); assert.equal(app.requests.length, 2, 'known idempotent save is already represented');
    app.update([sweep('next')]); app.click('pin'); app.click('save');
    assert.equal(app.requests.length, 2, 'known name conflict never overwrites');
    assert.match(app.dom['catalog-status'].textContent, /другому свипу/);
  }
  for (const input of ['', '  ', '🛰'.repeat(81), 'x\n', '\tfoo', 'foo\u200b', 'x\uE000', 'x\uD800', 'x\u0378']) {
    const app = setup(); await app.loaded(); app.click('pin'); app.dom.name.value = input;
    app.click('save'); assert.equal(app.requests.length, 1, `invalid name: ${JSON.stringify(input)}`);
    assert.match(app.dom['catalog-status'].textContent, /Имя/);
  }
  const app = setup(); await app.loaded(); app.click('pin'); app.dom.name.value = 'Фон';
  const pending = app.click('save'); app.respond(1, record('Фон', 'current'), 200); await pending;
  assert.match(app.dom['catalog-status'].textContent, /Сохранено/, 'server replay 200 accepted');
}
async function errorsAndRecovery() {
  const app = setup(); app.update(); app.respond(0, { error: { code: 'not_found' } }, 404, false); await flush();
  assert.match(app.dom['catalog-status'].textContent, /не поддерживает каталог/);
  assert.equal(app.dom.save.disabled, true);
  app.click('pin'); const comparisonRequest = app.click('run'); app.respond(1, comparison('current', 'current')); await comparisonRequest;
  assert.equal(app.dom.canvas.hidden, false, 'old server preserves local comparison');
  const codes = ['reference_name_conflict', 'reference_limit_reached', 'comparison_incompatible'];
  for (const code of codes) {
    const a = setup(); await a.loaded(); a.click('pin'); a.dom.name.value = 'Фон';
    const pending = a.click('save'); a.respond(1, { error: { code } }, 409, false); await pending;
    assert(!a.dom['catalog-status'].textContent.includes('неизвестен'), 'definite server rejection is not unknown');
    assert.equal(a.dom.save.disabled, false); assert.equal(a.requests.length, 2);
  }
  for (const failure of ['network', 'timeout', '500', 'malformed', 'foreign', 'wrong-name', 'wrong-sweep', 'contract', '202']) {
    const a = setup(); await a.loaded(); a.click('pin'); a.dom.name.value = 'Фон'; const pending = a.click('save');
    if (failure === 'network') a.requests[1].reject(new Error('connection lost'));
    else if (failure === 'timeout') {
      assert.equal([...a.timers.values()][0].ms, 8000);
      [...a.timers.values()][0].fn();
      assert.equal(a.requests[1].options.signal.aborted, true);
      a.respond(1, record('Фон', 'current'), 201);
    } else {
      let value = record('Фон', 'current');
      if (failure === 'malformed') value.points = NaN;
      if (failure === 'foreign') value.stream_id = 'foreign';
      if (failure === 'wrong-name') value.name = 'Another';
      if (failure === 'wrong-sweep') value.sweep_id = 'Other';
      a.respond(1, value, failure === '500' ? 500 : failure === '202' ? 202 : 201, failure !== 'contract');
    }
    await pending;
    assert.match(a.dom['catalog-status'].textContent, /неизвестен/, failure);
    assert.equal(a.dom.save.disabled, true); assert.equal(a.dom.refresh.disabled, false);
    a.click('save'); assert.equal(a.requests.length, 2, 'unknown POST is never retried automatically or by save');
    const refresh = a.click('refresh'); a.respond(2, catalog([record('Фон', 'current')])); await refresh;
    a.click('save'); assert.equal(a.requests.length, 3, 'refresh recovers committed result');
    assert.match(a.dom['catalog-status'].textContent, /Уже сохранено/);
  }
}
async function malformedCatalogs() {
  for (const corrupt of [d => d.session_id = 'foreign', d => d.stream_id = 'foreign', d => d.storage_mode = 'disk',
    d => d.persistent = 'false', d => d.storage_mode = 'memory', d => d.limit = 101, d => d.items = [record(), record()],
    d => d.items = Array.from({ length: 101 }, (_, n) => record(`name-${n}`)),
    d => d.items[0].created_at = 'not-date', d => d.items[0].interval.ended_at = '2020-01-01T00:00:00Z',
    d => d.items[0].interval.started_at = '2026-11-08T12:30:00+03:00', d => d.items[0].level_unit = 's_meter_count',
    d => d.items[0].name = ' e\u0301 ', d => d.items[0].name = 'x\n', d => d.items[0].points = 0,
    d => d.items[0].points = 16385, d => d.items[0].stream_id = 'foreign', d => d.items[0].sweep_id = '',
    d => d.items[0].id = null]) {
    const app = setup(); app.update(); const value = catalog([record()]); corrupt(value); app.respond(0, value); await flush();
    assert.match(app.dom['catalog-status'].textContent, /недоступен/);
    assert.equal(app.dom.use.disabled, true); assert.equal(app.dom.save.disabled, true);
    app.click('pin'); assert.equal(app.dom.run.disabled, false);
  }
}
async function staleScopeAndClosing() {
  for (const method of ['GET', 'POST']) for (const action of ['server', 'session', 'stream', 'pagehide']) {
    const app = setup();
    let index;
    if (method === 'POST') { await app.loaded(); app.click('pin'); app.dom.name.value = 'Фон'; app.click('save'); index = 1; }
    else { app.update(); index = 0; }
    if (action === 'server') { app.window.location.origin = 'http://other'; app.update(); }
    if (action === 'session') app.update([sweep('next')], 'other');
    if (action === 'stream') app.update([sweep('next', 'tiny/2')], 'session/1', 'tiny/2');
    if (action === 'pagehide') app.events.pagehide();
    assert.equal(app.requests[index].options.signal.aborted, true);
    assert.match(app.dom.reference.textContent, /не закреплён/);
    const status = app.dom['catalog-status'].textContent;
    app.respond(index, method === 'POST' ? record('Фон', 'current') : catalog([record()]), method === 'POST' ? 201 : 200); await flush();
    assert.equal(app.dom['catalog-status'].textContent, status, `${method} ${action} late response ignored`);
    assert(!app.dom.catalog.options.some(option => option.value === 'ref-Фон'));
    if (method === 'POST') assert.match(app.dom['catalog-notice'].textContent, /могла сохраниться/);
    if (action === 'pagehide') {
      const count = app.requests.length; app.click('save'); app.click('refresh'); assert.equal(app.requests.length, count);
      app.events.pageshow(); assert.equal(app.requests.length, count + 1);
    }
  }
  // Even an HTTP error body may resolve after navigation; it must not clear a new catalog.
  const app = setup(); app.update(); let finishJSON;
  app.requests[0].resolve({ ok: false, status: 404, json: () => new Promise(resolve => { finishJSON = resolve; }) }); await flush();
  app.window.location.origin = 'http://other'; app.update(); app.respond(1, catalog([record('New')])); await flush();
  finishJSON({ error: { code: 'not_found' } }); await flush();
  assert(app.dom.catalog.options.some(option => option.value === 'ref-New'));
}
async function failedRefreshInvalidatesCatalog() {
  for (const failure of ['timeout', 'malformed', '404']) {
    const app = setup(); await app.loaded([record()]); app.click('use');
    const pending = app.click('refresh');
    if (failure === 'timeout') {
      [...app.timers.values()][0].fn();
      app.respond(1, catalog([record()]));
    } else app.respond(1, failure === 'malformed' ? {} : catalog(), failure === '404' ? 404 : 200);
    await pending;
    assert.equal(app.dom.use.disabled, true); assert.equal(app.dom.save.disabled, true);
    assert.match(app.dom.storage.textContent, /не проверено/);
    assert.match(app.dom.reference.textContent, /old-outside-window/, 'local pin survives failed refresh');
    const retry = app.click('refresh'); app.respond(2, catalog([record()])); await retry;
    assert.equal(app.dom.use.disabled, false);
  }
}
async function independentLocalActions() {
  const app = setup(); await app.loaded(); app.click('pin'); app.dom.name.value = 'Фон'; const pending = app.click('save');
  app.click('clear'); app.update([sweep('next')]); app.click('pin');
  assert.equal(app.requests.length, 2, 'local pin actions never post');
  assert.equal(app.requests[1].options.signal.aborted, false, 'local clear does not pretend to cancel a save');
  app.respond(1, record('Фон', 'current'), 201); await pending;
  assert.match(app.dom.reference.textContent, /next/, 'save completion must not replace newer pin');
  assert(app.dom.catalog.options.some(option => option.textContent.includes('current')));
  const blank = setup(); blank.update([], null, null); assert.equal(blank.requests.length, 0);
  blank.update([], 'session/1', null); assert(!blank.requests[0].url.includes('?'));
  blank.respond(0, catalog([record()], true, null)); await flush(); assert.equal(blank.dom.use.disabled, true);
}
(async () => {
  await catalogAndComparison(); await saveAndNames(); await errorsAndRecovery(); await malformedCatalogs();
  await staleScopeAndClosing(); await failedRefreshInvalidatesCatalog(); await independentLocalActions();
  console.log('named reference behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
