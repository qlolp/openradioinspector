/* Execute production handlers with synthetic data and adversarial transport timing. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');
const copy = structuredClone;
function sweep(id = 'source-1', count = 7, stream = 'tiny/1') {
  return { id, stream_id: stream, level_unit: 'dBm',
    interval: { started_at: '2026-11-08T12:30:00.000001+00:00', ended_at: '2026-11-08T12:30:01.000002+00:00' },
    frequencies_hz: Array.from({ length: count }, (_, i) => 100 + i * 100),
    levels: count === 7 ? [-90, -70, -70, -90, -80, -90, -60] : Array(count).fill(-90),
    settings: { rbw_hz: 1000, active: true, nested: { array: [1, 'known'] }, sequence: 1 } };
}
function payload(source = sweep(), levels = [-90, -80, -70], session = 'session/1') {
  const d = { session_id: session, stream_id: source.stream_id, sweep: copy(source),
    frequencies_hz: [...source.frequencies_hz], points: source.levels.length,
    method: 'three_horizontal', comparison: 'strictly_greater', level_unit: 'dBm', delta_unit: 'dB',
    lines: levels.map((level, i) => ({ id: `line-${i + 1}`, level_dbm: level,
      excess_levels: source.levels.map(raw => raw - level), regions: [], region_count: 0, exceedance_points: 0 })) };
  // Test-only oracle for independently generated synthetic core responses.
  d.lines.forEach(line => {
    for (let i = 0; i < d.points; i++) {
      if (line.excess_levels[i] <= 0) continue;
      const start = i; let peak = i;
      while (i + 1 < d.points && line.excess_levels[i + 1] > 0) { i++; if (line.excess_levels[i] > line.excess_levels[peak]) peak = i; }
      line.regions.push({ start_index: start, end_index: i, peak_index: peak, start_frequency_hz: d.frequencies_hz[start],
        end_frequency_hz: d.frequencies_hz[i], peak_frequency_hz: d.frequencies_hz[peak], peak_excess_db: line.excess_levels[peak], points: i - start + 1 });
      line.exceedance_points += i - start + 1;
    }
    line.region_count = line.regions.length;
  });
  return d;
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map(), drawing = [];
  let timerId = 0;
  const context2d = new Proxy({}, { get(target, key) { return target[key] || ((...args) => drawing.push([key, ...args])); },
    set(target, key, value) { target[key] = value; drawing.push([key, value]); return true; } });
  function element() {
    const result = { value: '', textContent: '', hidden: false, disabled: false, width: 960, height: 280, listeners: {}, children: [],
      addEventListener(event, fn) { this.listeners[event] = fn; }, replaceChildren(...items) { this.children = items; },
      setAttribute(name, value) { this[name] = value; }, getContext() { return context2d; } };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'level-1', 'level-2', 'level-3', 'source', 'status', 'summary', 'canvas',
    'line-1-summary', 'line-2-summary', 'line-3-summary', 'line-1-regions', 'line-2-regions', 'line-3-regions']) dom[name] = element();
  [-90, -80, -70].forEach((value, i) => { dom[`level-${i + 1}`].value = String(value); });
  const window = { location: { origin: 'http://server-a' }, addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) { return dom[id.replace('three-thresholds-', '')]; }, createElement: element },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  function update(sweeps, sessionId = 'session/1', streamId = 'tiny/1') { window.OpenRadioSweepThreeThresholds.update({ sweeps, sessionId, streamId }); }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const json = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope },
      (_key, value) => typeof value === 'number' && !Number.isFinite(value) ? `__NONFINITE_${value}__` : value)
      .replace(/"__NONFINITE_Infinity__"/g, '1e999').replace(/"__NONFINITE_-Infinity__"/g, '-1e999')
      .replace(/"__NONFINITE_NaN__"/g, 'NaN');
    const body = Buffer.from(json);
    requests[index].resolve(streamResponse([body], String(body.length), status));
  }
  return { dom, events, requests, timers, drawing, window, context, update, click, respond };
}
function streamResponse(chunks, length = null, status = 200) {
  const state = { reads: 0, cancelled: 0, released: 0 };
  return { status, state, headers: { get: () => length }, body: { getReader: () => ({
    async read() { const value = chunks[state.reads++]; return value === undefined ? { done: true } : { done: false, value }; },
    cancel() { state.cancelled++; return Promise.resolve(); }, releaseLock() { state.released++; },
  }) } };
}
async function happyAndFrozen() {
  const app = setup(), source = sweep('<img onerror=alert(1)> /?&'), frozen = copy(source);
  app.update([sweep('old'), source, sweep('other-stream', 3, 'other'), { ...sweep('receiver'), level_unit: 's_meter_count' }]);
  assert.equal(app.requests.length, 0, 'updates never analyze');
  const promise = app.click('run'); app.click('run');
  assert.equal(app.requests.length, 1, 'single flight'); assert.equal(app.dom.run.disabled, true);
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-three-thresholds'); assert.equal(url.search, '');
  assert.deepEqual(JSON.parse(app.requests[0].options.body), { sweep_id: frozen.id, levels_dbm: [-90, -80, -70] });
  assert.equal(app.requests[0].options.method, 'POST'); assert.equal(app.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(app.requests[0].options.cache, 'no-store');
  source.levels[0] = -100; source.settings.sequence = 99; source.settings.nested.array[0] = 999; source.interval.started_at = 'bad';
  app.respond(0, payload(frozen)); await promise;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent); assert.equal(app.timers.size, 0);
  assert.match(app.dom.summary.textContent, /Все 7 точек/); assert.match(app.dom.summary.textContent, /00\.000001\+00:00/);
  assert.match(app.dom.summary.textContent, /"sequence":1/); assert(app.dom.summary.textContent.includes(frozen.id));
  for (const [id, count, regions] of [[1, 4, 3], [2, 3, 2], [3, 1, 1]]) {
    assert.match(app.dom[`line-${id}-summary`].textContent, new RegExp(`Превышений \\(> линии\\): ${count}`));
    assert.equal(app.dom[`line-${id}-regions`].children.length, regions);
    assert.match(app.dom[`line-${id}-summary`].textContent, /превышение Δ.*dB/);
  }
  assert.match(app.dom['line-1-regions'].children[0].textContent, /20 dB @ 200 Гц \(первый индекс 1\)/);
  assert.match(app.dom.status.textContent, /не нормативные/);
  const summary = app.dom.summary.textContent, perLine = app.dom['line-1-summary'].textContent;
  app.update(Array.from({ length: 40 }, (_, i) => sweep(`next-${i}`)));
  assert.equal(app.requests.length, 1); assert.equal(app.dom.summary.textContent, summary); assert.equal(app.dom['line-1-summary'].textContent, perLine);
  const again = app.click('run'); app.respond(1, payload(sweep('next-39'))); await again;
  assert.match(app.dom.summary.textContent, /next-39/);
  app.dom['level-2'].value = '-100'; app.dom['level-2'].listeners.input();
  assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.canvas.hidden, true);
  for (let i = 1; i <= 3; i++) assert.equal(app.dom[`line-${i}-regions`].children.length, 0);
  app.click('clear'); assert.equal(app.requests.length, 2);
}
async function inputBoundaries() {
  for (const value of ['', ' ', 'NaN', 'Infinity', '-Infinity', '1e999', '0x10', 'false', '1,2']) for (const i of [1, 2, 3]) {
    const app = setup(); app.update([sweep()]); app.dom[`level-${i}`].value = value; await app.click('run');
    assert.equal(app.requests.length, 0, `${i}: ${value}`); assert.equal(app.dom.canvas.hidden, true);
    assert.match(app.dom.status.textContent, /конечное число/);
  }
  for (const levels of [[-70, -90, -80], [-80, -80, -80], [0, -0, 0], [1e300, -1e300, 0], [1e-300, -1e-300, 3.5]]) {
    const app = setup(), source = sweep(); app.update([source]); levels.forEach((v, i) => { app.dom[`level-${i + 1}`].value = String(v); });
    const p = app.click('run'); assert.deepEqual(JSON.parse(app.requests[0].options.body).levels_dbm, levels.map(v => v || 0));
    app.respond(0, payload(source, levels)); await p; assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  }
  const badSources = [s => s.id = '', s => s.id = ' ', s => s.id = '😀'.repeat(129), s => s.id = '\ud800', s => s.id = '\udc00',
    s => s.settings = {}, s => s.settings = { sequence: 1 }, s => s.settings.rbw_hz = null,
    s => s.settings.nested.array[0] = null, s => s.settings.sequence = Infinity,
    s => s.frequencies_hz[0] = true, s => s.frequencies_hz[0] = -1, s => s.frequencies_hz[0] = 1.5,
    s => s.frequencies_hz[0] = Number.MAX_SAFE_INTEGER + 1, s => s.frequencies_hz[1] = 0,
    s => s.levels[0] = Infinity, s => s.levels[0] = true, s => s.frequencies_hz.pop(),
    s => s.interval.started_at = '2026-02-30T00:00:00Z', s => s.interval.started_at = '2026-11-08T12:30:00.000001Z\n', s => s.interval.started_at = '0000-01-01T00:00:00Z',
    s => s.interval.started_at = '2026-11-08T12:30:00.0000010Z', s => s.interval.started_at = '2026-11-08T12:30:00+01:00',
    s => s.interval.ended_at = '2026-11-08T12:29:59Z'];
  for (const mutate of badSources) {
    const app = setup(), source = sweep(); mutate(source); app.update([source]); await app.click('run');
    assert.equal(app.requests.length, 0, String(mutate)); assert.equal(app.dom.canvas.hidden, true);
  }
  for (const count of [0, 16385]) {
    const app = setup(); app.update([sweep('invalid-points', count)]); await app.click('run'); assert.equal(app.requests.length, 0);
  }
  const app = setup(), source = sweep('😀'.repeat(128)); source.settings.sequence = { nullable: [null] };
  app.update([source]); const p = app.click('run'); app.respond(0, payload(source)); await p; assert.equal(app.dom.canvas.hidden, false);
  for (const sweeps of [[], [{ ...sweep(), level_unit: 's_meter_count' }], [sweep('other', 7, 'other')]]) {
    const absent = setup(); absent.update(sweeps); await absent.click('run'); assert.equal(absent.requests.length, 0); assert.equal(absent.dom.run.disabled, true);
  }
}
async function corruptResponses() {
  const changes = [d => d.session_id = 'wrong', d => d.stream_id = 'wrong', d => d.method = 'rolling_median',
    d => d.comparison = 'greater_or_equal', d => d.level_unit = 'dB', d => d.delta_unit = 'dBm',
    d => d.points = true, d => d.points = 0, d => d.points = 16385, d => d.frequencies_hz.pop(),
    d => d.frequencies_hz[0] = true, d => d.frequencies_hz[0] = -1, d => d.frequencies_hz[0] = 100.5,
    d => d.frequencies_hz[0] = 9007199254740992, d => d.frequencies_hz[1] = 99, d => d.frequencies_hz[0] = 101,
    d => d.sweep = null, d => d.sweep.id = 'wrong', d => d.sweep.level_unit = 's_meter_count', d => d.sweep.levels.pop(),
    d => d.sweep.levels[0] = -100, d => d.sweep.levels[0] = true, d => d.sweep.levels[0] = Infinity,
    d => d.sweep.settings.sequence = 2, d => d.sweep.settings.nested.array[0] = true, d => d.sweep.settings.extra = 1,
    d => d.sweep.settings.rbw_hz = null, d => d.sweep.settings = {}, d => d.sweep.settings = { sequence: 1 },
    d => d.sweep.interval = null, d => d.sweep.interval.started_at = '2026-11-08T12:30:00.000001Z\n', d => d.sweep.interval.started_at = '2026-11-08T12:30:00.000002Z',
    d => d.sweep.interval.ended_at = '2026-11-08T12:30:01.000003Z', d => d.sweep.interval.started_at = '2026-02-30T00:00:00Z',
    d => d.sweep.interval.started_at = '0000-11-08T12:30:00.000001Z', d => d.sweep.interval.started_at = '2026-11-08T12:30:00.0000010Z',
    d => d.sweep.interval.started_at = '2026-11-08T13:30:00.000001+01:00',
    d => d.sweep.interval.started_at = '2026-11-08T12:30:02.000002Z', d => d.lines = null, d => d.lines.pop(), d => d.lines.push(copy(d.lines[0])),
    d => d.lines.reverse(), d => d.lines[0] = null, d => d.lines[0].id = 'line-2', d => d.lines[0].level_dbm = -80,
    d => d.lines[0].level_dbm = '-90', d => d.lines[0].level_dbm = true, d => d.lines[0].level_dbm = Infinity,
    d => d.lines[0].excess_levels.pop(), d => d.lines[0].excess_levels[0] = 1, d => d.lines[0].excess_levels[0] = true,
    d => d.lines[0].excess_levels[0] = Infinity, d => d.lines[0].regions = null, d => d.lines[0].regions.pop(),
    d => d.lines[0].region_count = true, d => d.lines[0].region_count = 2, d => d.lines[0].exceedance_points = true,
    d => d.lines[0].exceedance_points = 0, d => d.lines[0].exceedance_points = 8,
    d => d.lines[0].regions[0].start_index = true, d => d.lines[0].regions[0].start_index = 0,
    d => d.lines[0].regions[0].end_index = 3, d => d.lines[0].regions[0].peak_index = 2,
    d => d.lines[0].regions[0].peak_frequency_hz = 300, d => d.lines[0].regions[0].start_frequency_hz = 101,
    d => d.lines[0].regions[0].end_frequency_hz = 101, d => d.lines[0].regions[0].peak_excess_db = 21,
    d => d.lines[0].regions[0].points = 3, d => d.lines[0].regions[0] = null];
  for (const change of changes) {
    const app = setup(); app.update([sweep()]); const p = app.click('run'), data = payload(); change(data); app.respond(0, data); await p;
    assert.equal(app.dom.canvas.hidden, true, String(change)); assert.match(app.dom.status.textContent, /Три порога недоступны/, String(change));
    assert.equal(app.dom.summary.textContent, '');
  }
  const app = setup(); app.update([sweep()]); const p = app.click('run'), data = payload();
  data.sweep.settings = Object.fromEntries(Object.entries(data.sweep.settings).reverse());
  data.sweep.interval.started_at = '2026-11-08T12:30:00.000001Z';
  app.respond(0, data); await p; assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
}
async function staleAndLifecycle() {
  for (const change of ['clear', 'level-1', 'level-2', 'level-3', 'scope', 'stream', 'server', 'offline', 'connection', 'pagehide', 'timeout']) {
    for (const outcome of ['success', 'error']) {
      const app = setup(); app.update([sweep()]); const p = app.click('run');
      if (change === 'clear') app.click('clear');
      if (change.startsWith('level-')) { app.dom[change].value = '-100'; app.dom[change].listeners.input(); }
      if (change === 'scope') app.update([sweep()], 'other-session');
      if (change === 'stream') app.update([sweep()], 'session/1', 'other-stream');
      if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update([sweep()]); }
      if (change === 'offline') app.events.offline();
      if (change === 'connection') app.window.OpenRadioSweepThreeThresholds.setConnected(false);
      if (change === 'pagehide') app.events.pagehide();
      if (change === 'timeout') { const timer = [...app.timers.values()][0]; assert.equal(timer.ms, 8000); timer.fn(); }
      assert.equal(app.requests[0].options.signal.aborted, true, change);
      const before = app.dom.status.textContent;
      if (outcome === 'success') app.respond(0, payload()); else app.requests[0].reject(new Error('late failure'));
      await p; assert.equal(app.dom.status.textContent, before, change); assert.equal(app.dom.canvas.hidden, true);
      if (['offline', 'connection'].includes(change)) app.window.OpenRadioSweepThreeThresholds.setConnected(true);
      if (change === 'pagehide') app.events.pageshow();
      assert.equal(app.requests.length, 1, 'recovery cannot autorun');
    }
  }
  const app = setup(); app.update([sweep()]); const first = app.click('run'); app.click('clear'); const second = app.click('run');
  app.respond(1, payload()); await second; const text = app.dom.summary.textContent, status = app.dom.status.textContent;
  app.requests[0].reject(new Error('late previous error')); await first;
  assert.equal(app.dom.summary.textContent, text); assert.equal(app.dom.status.textContent, status);
  for (let i = 1; i <= 3; i++) {
    app.dom[`level-${i}`].listeners.input(); assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.canvas.hidden, true);
    const pending = app.click('run'); app.respond(app.requests.length - 1, payload()); await pending;
  }
}
async function bodiesAndStatuses() {
  for (const status of [201, 204, 206, 400, 404, 409, 413, 500]) {
    const app = setup(); app.update([sweep()]); const p = app.click('run'); app.respond(0, payload(), status); await p;
    assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /Три порога недоступны/); assert.equal(app.dom.run.disabled, false);
  }
  for (const status of [200, 201, 400, 404, 409, 413, 500]) for (const length of ['2000001', null, '1']) {
    const app = setup(); app.update([sweep()]); const p = app.click('run');
    const response = streamResponse([Buffer.alloc(1000000, ' '), Buffer.alloc(1000001, ' ')], length, status);
    app.requests[0].resolve(response); await p;
    assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /2 000 000 байт/);
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(response.state.cancelled, 1);
    assert.equal(response.state.reads, length === '2000001' ? 0 : 2); assert.equal(response.state.released, 1);
  }
  const app = setup(); app.update([sweep()]); let p = app.click('run');
  const json = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: payload() }));
  app.requests[0].resolve(streamResponse([Buffer.alloc(2000000 - json.length, ' '), json], '2000000')); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  for (const value of ['{', Buffer.from([0xff]), '\ufeff{}']) {
    p = app.click('run'); app.requests.at(-1).resolve(streamResponse([Buffer.from(value)])); await p; assert.equal(app.dom.canvas.hidden, true);
  }
  for (const envelope of [{ contract: 'wrong' }, { contract_version: true }, { contract_version: 2 }, { data: null }]) {
    p = app.click('run'); app.respond(app.requests.length - 1, payload(), 200, envelope); await p; assert.equal(app.dom.canvas.hidden, true);
  }
  p = app.click('run'); app.requests.at(-1).reject(new Error('network failed')); await p; assert.match(app.dom.status.textContent, /network failed/);
  p = app.click('run'); app.requests.at(-1).resolve({ status: 200, body: null }); await p; assert.match(app.dom.status.textContent, /потоковый/);
  const late = setup(); late.update([sweep()]); const running = late.click('run'); let finish, cancelled = 0;
  late.requests[0].resolve({ status: 200, body: { getReader: () => ({
    read: () => new Promise(resolve => finish = resolve), cancel() { cancelled++; return Promise.resolve(); }, releaseLock() {},
  }) } });
  await new Promise(resolve => setImmediate(resolve)); late.click('clear'); assert.equal(cancelled, 1);
  const before = late.dom.status.textContent; finish({ done: false, value: Buffer.alloc(2000001) }); await running;
  assert.equal(late.dom.status.textContent, before); assert.equal(late.dom.canvas.hidden, true);
  const stale = setup(); stale.update([sweep()]); const staleRequest = stale.click('run'); stale.click('clear'); let bodyCancelled = 0;
  stale.requests[0].resolve({ status: 200, body: { cancel() { bodyCancelled++; return Promise.resolve(); } } });
  await staleRequest; assert.equal(bodyCancelled, 1);
}
async function completeDrawing() {
  for (const count of [1, 16384]) for (const repeated of [true, false]) {
    const app = setup(), source = sweep('large', count); source.levels.fill(-1e300); if (repeated) source.frequencies_hz.fill(100);
    const levels = [-1e300, 1e300, 0]; levels.forEach((v, i) => { app.dom[`level-${i + 1}`].value = String(v); });
    app.update([source]); const p = app.click('run'); app.respond(0, payload(source, levels)); await p;
    assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
    assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, count + 6, 'every raw bin and 3 complete horizontal lines');
    assert.equal(app.drawing.filter(row => row[0] === 'strokeStyle').length, 4);
    assert.equal(app.drawing.filter(row => row[0] === 'arc').length, 3);
    const lines = app.drawing.filter(row => row[0] === 'lineTo').slice(-3);
    assert(lines.every(row => row[1] === 936), 'all thresholds span plot width, including one-bin plots');
    for (const row of app.drawing.filter(row => ['moveTo', 'lineTo', 'arc', 'strokeRect'].includes(row[0]))) assert(row.slice(1).every(Number.isFinite));
  }
  for (const count of [1, 7]) {
    const app = setup(), source = sweep('coincident', count); source.frequencies_hz.fill(100); source.levels.fill(-90);
    for (let i = 1; i <= 3; i++) app.dom[`level-${i}`].value = '-90';
    app.update([source]); const p = app.click('run'); app.respond(0, payload(source, [-90, -90, -90])); await p;
    assert.equal(app.dom.canvas.hidden, false); assert.match(app.dom.summary.textContent, /Совпадающие линии: line-1, line-2, line-3/);
    const rings = app.drawing.filter(row => row[0] === 'arc'), square = app.drawing.find(row => row[0] === 'strokeRect');
    assert.deepEqual(rings.map(row => row[3]), [11, 8, 5]);
    assert(rings.every(row => row[1] === square[1] + 2 && row[2] === square[2] + 2));
    assert(rings.at(-1)[3] > square[3] / 2, 'inner ring cannot hide source square');
    for (let i = 1; i <= 3; i++) { assert.equal(app.dom[`line-${i}-regions`].children.length, 0); assert.match(app.dom[`line-${i}-summary`].textContent, /Превышений \(> линии\): 0/); }
  }
  const app = setup(), source = sweep('many-regions', 405); source.levels = source.levels.map((_, i) => i % 2 ? -100 : -50);
  app.update([source]); const p = app.click('run'); app.respond(0, payload(source)); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  for (let i = 1; i <= 3; i++) { assert.equal(app.dom[`line-${i}-regions`].children.length, 200); assert.match(app.dom[`line-${i}-summary`].textContent, /первые 200 из 203/); }
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 411);
}
async function platformIntegration() {
  const app = setup(); app.update([sweep()]); const pending = app.click('run'), c = app.context;
  c.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: [sweep()], refreshRunning: false };
  c.dom = { sessionSelect: { value: 'different' }, spectrumStream: { value: 'other' }, health: {} };
  c.storeSession = () => {}; c.closeHandoff = () => {}; c.refresh = () => assert(app.requests[0].options.signal.aborted);
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) {
    const start = platformCode.indexOf(`  function ${name}(`), end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0); vm.runInContext(platformCode.slice(start, end), c);
  }
  c.changeSession(); app.respond(0, payload()); await pending; assert.equal(app.dom.canvas.hidden, true);
  c.changeSpectrumStream(); assert.equal(app.requests.length, 1);
  const start = platformCode.indexOf('  async function refresh('), end = platformCode.indexOf('\n  }', start) + 4;
  c.renderSession = () => {}; c.renderAll = () => {}; c.reportError = () => {}; c.scheduleRefresh = () => {};
  c.performRefresh = async () => { throw new Error('disconnected'); }; vm.runInContext(platformCode.slice(start, end), c);
  app.update([sweep()]); const second = app.click('run'); await c.refresh(); assert.equal(app.requests[1].options.signal.aborted, true);
  app.respond(1, payload()); await second; assert.equal(app.dom.run.disabled, true);
  c.performRefresh = async () => {}; await c.refresh(); assert.equal(app.dom.run.disabled, false); assert.equal(app.requests.length, 2);
  const handlers = {}; c.API_ROOT = '/api/v1'; c.encode = encodeURIComponent; c.EVENT_TYPES = [];
  c.setSseStatus = () => {}; c.EventSource = function () { this.addEventListener = (name, fn) => handlers[name] = fn; this.close = () => {}; };
  c.window.EventSource = c.EventSource;
  const eventStart = platformCode.indexOf('  function connectEvents('), eventEnd = platformCode.indexOf('\n  }', eventStart) + 4;
  vm.runInContext(platformCode.slice(eventStart, eventEnd), c); c.connectEvents();
  const third = app.click('run'); handlers.error(); assert.equal(app.requests[2].options.signal.aborted, true);
  app.respond(2, payload()); await third; assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.run.disabled, true);
  await c.refresh(); assert.equal(app.dom.run.disabled, false); assert.equal(app.requests.length, 3);
}
async function httpSmoke(base) {
  const sourceResponse = await fetch(`${base}/api/v1/sessions/three-one/sweeps?stream_id=spectrum-one&limit=40`);
  assert.equal(sourceResponse.status, 200); const sources = (await sourceResponse.json()).data;
  const app = setup(), calls = []; app.window.location.origin = base;
  app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
  app.update(sources, 'three-one', 'spectrum-one'); assert.equal(calls.length, 0);
  await app.click('run'); assert.equal(calls.length, 1); assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /one-sweep-2/); assert.match(app.dom.summary.textContent, /20\.000001/);
  for (const [id, count, regions, peak] of [[1, 4, 2, 25], [2, 2, 2, 15], [3, 1, 1, 5]]) {
    assert.match(app.dom[`line-${id}-summary`].textContent, new RegExp(`Превышений \\(> линии\\): ${count}`));
    assert.equal(app.dom[`line-${id}-regions`].children.length, regions);
    assert.match(app.dom[`line-${id}-regions`].children[0].textContent, new RegExp(`${peak} dB @ 103000000 Гц`));
  }
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 107);
  const frozen = app.dom.summary.textContent; app.update([], 'three-one', 'spectrum-one'); assert.equal(app.dom.summary.textContent, frozen);
  app.update(sources, 'three-one', 'spectrum-one'); await app.click('run'); assert.equal(calls.length, 2);
  app.dom['level-2'].value = '-90'; app.dom['level-2'].listeners.input(); assert.equal(app.dom.canvas.hidden, true);
  await app.click('run'); assert.equal(calls.length, 3); assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /Совпадающие линии: line-1, line-2/);
  app.update([], 'three-two', 'spectrum-two'); assert.equal(app.dom.run.disabled, true); assert.equal(app.dom.summary.textContent, '');
  console.log('Web HTTP three-threshold smoke passed: 101 raw bins, counts 4/2/1, peaks 25/15/5 dB, repeated analysis, edit, equal lines, scope reset');
}
(async () => {
  await happyAndFrozen(); await inputBoundaries(); await corruptResponses(); await staleAndLifecycle(); await bodiesAndStatuses();
  await completeDrawing(); await platformIntegration(); if (process.argv[4]) await httpSmoke(process.argv[4]);
  console.log('three-threshold behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
