/* Execute production handlers with synthetic data and adversarial transport timing. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');
const time = '2026-11-08T12:30:00+00:00';
function sweep(id = 'sweep/?&', count = 7, stream = 'tiny/1') {
  const levels = Array(count).fill(-60);
  if (count >= 3) levels[1] = -40;
  if (count >= 7) { levels[2] = -45; levels[4] = -50; }
  return { id, stream_id: stream, level_unit: 'dBm', interval: { started_at: time, ended_at: time },
    frequencies_hz: Array.from({ length: count }, (_, i) => 100 + i * 100), levels, settings: { rbw_hz: 1000, sequence: null } };
}
function mask() { return { format: 'openradioinspector-spectral-mask', format_version: 1, name: 'Synthetic mask', frequency_unit: 'Hz', level_unit: 'dBm', interpolation: 'linear_dbm', vertices: [{ frequency_hz: 0, limit_dbm: -54 }, { frequency_hz: 9007199254740991, limit_dbm: -54 }] }; }
function payload(source = sweep(), artifact = mask(), session = 'session/1') {
  const d = { session_id: session, stream_id: source.stream_id, sweep: structuredClone(source), mask: artifact,
    frequencies_hz: [...source.frequencies_hz], comparison: 'strictly_greater',
    threshold_levels: Array(source.levels.length).fill(-54), excess_levels: source.levels.map(v => v + 54),
    level_unit: 'dBm', delta_unit: 'dB', regions: [], points: source.levels.length, exceedance_points: 0, region_count: 0 };
  summarize(d); return d;
}
function summarize(d) {
  d.regions = []; d.exceedance_points = 0;
  for (let i = 0; i < d.points; i++) {
    if (d.excess_levels[i] <= 0) continue;
    const start = i; let peak = i;
    while (i + 1 < d.points && d.excess_levels[i + 1] > 0) { i++; if (d.excess_levels[i] > d.excess_levels[peak]) peak = i; }
    d.regions.push({ start_index: start, end_index: i, peak_index: peak, start_frequency_hz: d.frequencies_hz[start],
      end_frequency_hz: d.frequencies_hz[i], peak_frequency_hz: d.frequencies_hz[peak], peak_excess_db: d.excess_levels[peak], points: i - start + 1 });
    d.exceedance_points += i - start + 1;
  }
  d.region_count = d.regions.length;
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map(), drawing = [];
  let timerId = 0; const blobs = [], downloads = [], revoked = [];
  const context2d = new Proxy({}, { get(target, key) { return target[key] || ((...args) => drawing.push([key, ...args])); },
    set(target, key, value) { target[key] = value; drawing.push([key, value]); return true; } });
  function element() {
    const result = { value: '', textContent: '', hidden: false, disabled: false, width: 960, height: 280, listeners: {}, children: [],
      addEventListener(event, fn) { this.listeners[event] = fn; }, replaceChildren(...items) { this.children = items; },
      setAttribute(name, value) { this[name] = value; }, click() { downloads.push(this); }, remove() {}, getContext() { return context2d; } };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'editor', 'file', 'template', 'save', 'draft', 'source', 'status', 'summary', 'regions', 'canvas']) dom[name] = element();
  dom.editor.value = JSON.stringify(mask());
  const window = { location: { origin: 'http://server-a' }, addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) { return dom[id.replace('mask-', '')]; }, createElement: element, body: { appendChild() {} } },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob, URL: { createObjectURL(blob) { blobs.push(blob); return `blob:${blobs.length}`; }, revokeObjectURL(url) { revoked.push(url); } }, fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  function update(sweeps, sessionId = 'session/1', streamId = 'tiny/1') { window.OpenRadioSweepMask.update({ sweeps, sessionId, streamId }); }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const json = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope },
      (_key, value) => typeof value === 'number' && !Number.isFinite(value) ? `__NONFINITE_${value}__` : value)
      .replace(/"__NONFINITE_Infinity__"/g, '1e999').replace(/"__NONFINITE_-Infinity__"/g, '-1e999')
      .replace(/"__NONFINITE_NaN__"/g, 'NaN');
    const body = Buffer.from(json);
    requests[index].resolve(streamResponse([body], String(body.length), status));
  }
  return { dom, events, requests, timers, drawing, window, context, update, click, respond, blobs, downloads, revoked };
}
function streamResponse(chunks, length = null, status = 200) {
  const state = { reads: 0, cancelled: 0, released: 0 };
  return { status, state, headers: { get: () => length }, body: { getReader: () => ({
    async read() { const value = chunks[state.reads++]; return value === undefined ? { done: true } : { done: false, value }; },
    cancel() { state.cancelled++; return Promise.resolve(); }, releaseLock() { state.released++; },
  }) } };
}
async function happyAndFrozen() {
  const app = setup(), source = sweep('<img onerror=alert(1)> /?&');
  app.update([sweep('old'), source, sweep('other-stream', 3, 'other')]);
  assert.equal(app.requests.length, 0, 'snapshot updates never analyze');
  const promise = app.click('run'); app.click('run');
  assert.equal(app.requests.length, 1, 'single flight'); assert.equal(app.dom.run.disabled, true);
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-mask-analysis'); assert.equal(url.search, '');
  const sent = JSON.parse(app.requests[0].options.body);
  assert.deepEqual(Object.keys(sent).sort(), ['mask_json', 'sweep_id']); assert.equal(sent.sweep_id, source.id);
  assert.equal(sent.mask_json, app.dom.editor.value); assert.equal(app.requests[0].options.method, 'POST');
  assert.equal(app.requests[0].options.headers['Content-Type'], 'application/json'); assert.equal(app.requests[0].options.cache, 'no-store');
  app.respond(0, payload(source)); await promise;
  assert.equal(app.dom.canvas.hidden, false); assert.equal(app.dom.regions.children.length, 2);
  assert.match(app.dom.summary.textContent, /Превышений \(> маски\): 3/);
  assert.match(app.dom.summary.textContent, /2026-11-08T12:30:00.000Z/);
  assert(app.dom.summary.textContent.includes(source.id));
  assert.match(app.dom.status.textContent, /не подтверждение соответствия нормативам/);
  assert.match(app.dom.regions.children[0].textContent, /Индексы 1–2/);
  assert.match(app.dom.regions.children[0].textContent, /14 dB @ 200 Гц/);
  const traces = app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0]));
  assert.equal(traces.length, 14, 'draw every bin in both traces');
  assert.equal(app.drawing.filter(row => row[0] === 'strokeStyle').length, 2);
  assert.equal(app.drawing.filter(row => row[0] === 'setLineDash').length, 3, 'different stroke styles');
  assert.equal(app.timers.size, 0);
  const frozen = app.dom.summary.textContent;
  app.update(Array.from({ length: 40 }, (_, i) => sweep(`next-${i}`)));
  assert.equal(app.requests.length, 1); assert.equal(app.dom.summary.textContent, frozen);
  const again = app.click('run'); app.respond(1, payload(sweep('next-39'))); await again;
  assert.match(app.dom.summary.textContent, /next-39/);
  app.dom.editor.value = '{'; app.dom.editor.listeners.input();
  assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.save.disabled, true);
  app.click('clear'); assert.equal(app.dom.canvas.hidden, true); assert.equal(app.dom.regions.children.length, 0);
}
async function badInputsAndStatus() {
  for (const value of ['', '   ', 'я'.repeat(32769)]) {
    const app = setup(); app.update([sweep()]); app.dom.editor.value = value; await app.click('run');
    assert.equal(app.requests.length, 0); assert.equal(app.dom.canvas.hidden, true);
  }
  for (const value of ['{', '{"name":"x","name":"y"}', '\ufeff{}']) {
    const app = setup(); app.update([sweep()]); app.dom.editor.value = value; const pending = app.click('run');
    assert.equal(JSON.parse(app.requests[0].options.body).mask_json, value, 'raw malformed or duplicate JSON is never rewritten');
    app.respond(0, {}, 400); await pending; assert.equal(app.dom.save.disabled, true);
  }
  for (const raw of [JSON.stringify(mask()).replace('"name":', '"name":"earlier","name":'),
    JSON.stringify(mask()).replace('"frequency_hz":0', '"frequency_hz":1,"frequency_hz":0'),
    JSON.stringify(mask()).replace('"name":', '"n\\u0061me":"earlier","name":')]) {
    const app = setup(); app.update([sweep()]); app.dom.editor.value = raw; const pending = app.click('run');
    assert.equal(JSON.parse(app.requests[0].options.body).mask_json, raw); app.respond(0, payload()); await pending;
    assert.equal(app.dom.save.disabled, true, 'faulty server cannot bless duplicate members');
    assert.match(app.dom.status.textContent, /Повторный ключ/);
  }
  for (const status of [201, 204, 206, 400, 404, 409, 413, 500]) {
    const app = setup(); app.update([sweep()]); const pending = app.click('run'); app.respond(0, payload(), status); await pending;
    assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /Маска недоступна/);
    assert.equal(app.dom.run.disabled, false);
    if (status === 404) assert.match(app.dom.status.textContent, /не поддерживает анализ/);
    if (status === 409) assert.match(app.dom.status.textContent, /несовместим/);
  }
  const app = setup(); app.update([sweep()]); let pending = app.click('run');
  app.requests[0].reject(new Error('offline')); await pending; assert.match(app.dom.status.textContent, /offline/);
  pending = app.click('run'); app.requests[1].resolve(streamResponse([Buffer.from('invalid JSON')]));
  await pending; assert.match(app.dom.status.textContent, /Маска недоступна/);
  for (const envelope of [{ contract: 'wrong' }, { contract_version: 2 }]) {
    pending = app.click('run'); app.respond(app.requests.length - 1, payload(), 200, envelope); await pending;
    assert.equal(app.dom.canvas.hidden, true);
  }
}
async function corruptResponses() {
  const mutations = [d => d.session_id = 'wrong', d => d.stream_id = 'wrong', d => d.sweep.id = 'wrong',
    d => d.level_unit = 'dB', d => d.delta_unit = 'dBm', d => d.sweep.level_unit = 's_meter_count',
    d => d.mask = null, d => d.mask.name = 'wrong', d => d.mask.extra = true, d => d.mask.format = 'wrong',
    d => d.mask.format_version = true, d => d.mask.level_unit = 'dB', d => d.mask.frequency_unit = 'MHz',
    d => d.mask.interpolation = 'linear_power', d => d.mask.name = ' Synthetic mask ', d => d.mask.name = 'x\n',
    d => d.mask.name = 'x'.repeat(81), d => d.mask.vertices.pop(), d => d.mask.vertices[0].frequency_hz = true,
    d => d.mask.vertices[0].frequency_hz = -1, d => d.mask.vertices[1].frequency_hz = 0,
    d => d.mask.vertices[0].limit_dbm = Infinity, d => d.mask.vertices[0].limit_dbm = -53,
    d => d.mask.vertices[0].extra = 1, d => d.comparison = 'greater_or_equal',
    d => d.points = true, d => d.points = 0, d => d.points = 16385,
    d => d.frequencies_hz[0] = -1, d => d.frequencies_hz[1] = 99, d => d.frequencies_hz[1] = 1.5,
    d => d.frequencies_hz[1] = Infinity, d => d.frequencies_hz[6] = Number.MAX_SAFE_INTEGER + 1,
    d => d.frequencies_hz.pop(), d => d.sweep.levels.pop(), d => d.sweep.levels[0] = Infinity,
    d => d.sweep.levels[0] = -61, d => d.sweep.settings = {}, d => d.sweep.settings = { sequence: 1 },
    d => d.sweep.settings = { rbw_hz: null }, d => d.sweep.settings.sequence = NaN, d => d.sweep.settings.rbw_hz = 2000,
    d => d.sweep.interval = {}, d => d.sweep.interval.ended_at = '2025-01-01T00:00:00Z',
    d => d.sweep.interval.started_at = '2026-11-08T15:30:00+03:00', d => d.sweep.interval.ended_at = '2026-11-08T12:31:00Z',
    d => d.threshold_levels[0] = NaN, d => d.threshold_levels.pop(),
    d => d.threshold_levels[0] = -53, d => d.excess_levels[0] = Infinity, d => d.excess_levels[0] = 0,
    d => d.regions = null, d => d.region_count = true, d => d.region_count++, d => d.exceedance_points = true,
    d => d.exceedance_points++, d => d.regions[0].start_index = 0, d => d.regions[0].end_index = 3,
    d => d.regions[0].peak_index = 2, d => d.regions[0].peak_frequency_hz = 300,
    d => d.regions[0].start_frequency_hz = 100, d => d.regions[0].end_frequency_hz = 400,
    d => d.regions[0].points = 1, d => d.regions[0].peak_excess_db = NaN,
    d => { d.regions.pop(); d.region_count--; }, d => { d.regions.push(d.regions[0]); d.region_count++; }];
  for (const [i, mutate] of mutations.entries()) {
    const app = setup(); app.update([sweep()]); const pending = app.click('run'), d = payload(); mutate(d);
    app.respond(0, d); await pending;
    assert.equal(app.dom.canvas.hidden, true, `mutation ${i}: ${mutate}`);
    assert.match(app.dom.status.textContent, /Маска недоступна/);
  }
}
async function staleAndLifecycle() {
  for (const change of ['clear', 'session', 'stream', 'server', 'pagehide', 'offline', 'disconnect', 'input', 'timeout']) {
    for (const phase of ['fetch', 'body']) {
      const app = setup(); app.update([sweep()]); const pending = app.click('run'); let finishBody;
      if (phase === 'body') {
        app.requests[0].resolve({ status: 200, body: { getReader: () => ({
          read: () => new Promise(resolve => { finishBody = resolve; }), cancel() { return Promise.resolve(); }, releaseLock() {},
        }) } });
        await new Promise(resolve => setImmediate(resolve));
      }
      if (change === 'clear') app.click('clear');
      if (change === 'session') app.update([sweep('next')], 'new-session');
      if (change === 'stream') app.update([sweep('next', 7, 'other')], 'session/1', 'other');
      if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update([sweep('next')]); }
      if (change === 'pagehide' || change === 'offline') app.events[change]();
      if (change === 'disconnect') app.window.OpenRadioSweepMask.setConnected(false);
      if (change === 'input') { app.dom.editor.value = '{'; app.dom.editor.listeners.input(); }
      if (change === 'timeout') {
        assert.equal([...app.timers.values()][0].ms, 8000); app.update([sweep('new')]);
        [...app.timers.values()][0].fn(); assert.match(app.dom.status.textContent, /таймаут 8 секунд/);
      }
      assert.equal(app.requests[0].options.signal.aborted, true, `${change}/${phase}`);
      const status = app.dom.status.textContent;
      if (phase === 'fetch') app.respond(0, payload());
      else finishBody({ done: false, value: Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: payload() })) });
      await pending; assert.equal(app.dom.canvas.hidden, true); assert.equal(app.dom.status.textContent, status);
      assert.equal(app.dom.summary.textContent, ''); assert.equal(app.timers.size, 0);
      if (change === 'pagehide') { await app.click('run'); assert.equal(app.requests.length, 1); app.events.pageshow(); }
      if (['offline', 'disconnect'].includes(change)) { await app.click('run'); assert.equal(app.requests.length, 1); app.window.OpenRadioSweepMask.setConnected(true); }
      assert.equal(app.requests.length, 1, 'resume never starts work');
    }
  }
  const app = setup(); app.update([sweep()]); const old = app.click('run');
  [...app.timers.values()][0].fn(); app.update([sweep('new')]); const fresh = app.click('run');
  app.respond(1, payload(sweep('new'))); await fresh; const expected = app.dom.summary.textContent;
  app.respond(0, payload()); await old; assert.equal(app.dom.summary.textContent, expected, 'late old cannot overwrite newer result');
}
async function limitsAndEquality() {
  for (const count of [1, 3, 16384]) {
    const app = setup(), source = sweep('large', count);
    source.levels = source.levels.map((_, i) => i % 2 ? -40 : -60);
    app.update([source]); const pending = app.click('run'); app.respond(0, payload(source)); await pending;
    assert.equal(app.dom.canvas.hidden, false);
    assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 2 * count);
    assert.equal(app.dom.regions.children.length, Math.min(200, Math.floor(count / 2)));
    if (count === 16384) assert.match(app.dom.summary.textContent, /первые 200 из 8192/);
  }
  for (const grid of [[0, 1, 1000], [100, 100, 100], [100, 100, 300]]) {
    const app = setup(), source = sweep('grid', 3); source.frequencies_hz = grid; source.levels = [-1e308, 0, 1e308];
    app.update([source]); const pending = app.click('run'), d = payload(source);
    d.threshold_levels = [-54, -54, -54]; d.excess_levels = [-1e308, 54, 1e308]; summarize(d);
    app.respond(0, d); await pending; assert.equal(app.dom.canvas.hidden, false);
    const points = app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0]));
    assert(points.flatMap(row => row.slice(1)).every(Number.isFinite));
    if (grid[0] === grid[2]) assert.equal(points[0][1], points[2][1]);
    if (grid[0] === 0) assert(Math.abs((points[1][1] - points[0][1]) / (points[2][1] - points[0][1]) - .001) < 1e-8);
  }
  const source = sweep('equality', 3); source.levels = [-54, -54, -54];
  const app = setup(); app.update([source]); let pending = app.click('run'); app.respond(0, payload(source)); await pending;
  assert.equal(app.dom.regions.children.length, 0); assert.match(app.dom.summary.textContent, /Превышений \(> маски\): 0/);
  source.levels = [-40, -40, -60]; app.update([source]); pending = app.click('run'); const d = payload(source);
  d.regions[0].peak_index = 1; d.regions[0].peak_frequency_hz = source.frequencies_hz[1];
  app.respond(1, d); await pending; assert.equal(app.dom.canvas.hidden, true, 'first tied peak only');
  app.update([{ ...sweep(), level_unit: 's_meter_count' }]); assert.equal(app.dom.run.disabled, true);
  await app.click('run'); assert.equal(app.requests.length, 2);
  app.update([sweep('dBm'), { ...sweep('not-dBm'), level_unit: 's_meter_count' }]); pending = app.click('run');
  assert.equal(JSON.parse(app.requests[2].options.body).sweep_id, 'dBm');
  app.respond(2, payload(sweep('dBm'))); await pending;
}
async function platformIntegration() {
  const app = setup(); app.update([sweep()]); const pending = app.click('run');
  const c = app.context;
  c.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: [sweep()] };
  c.dom = { sessionSelect: { value: 'new' }, spectrumStream: { value: 'other' }, health: {}, candidateForm: {reset() {}, dataset: {}}, candidateEditor: {} };
  c.storeSession = () => {}; c.closeHandoff = () => {}; c.refresh = () => assert(app.requests[0].options.signal.aborted);
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) {
    const start = platformCode.indexOf(`  function ${name}(`), end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0); vm.runInContext(platformCode.slice(start, end), c);
  }
  c.changeSession(); app.respond(0, payload()); await pending; assert.equal(app.dom.canvas.hidden, true);
  c.changeSpectrumStream(); assert.equal(app.requests.length, 1);
  const start = platformCode.indexOf('  async function refresh('), end = platformCode.indexOf('\n  }', start) + 4;
  c.renderSession = () => {}; c.renderAll = () => {}; c.reportError = () => {}; c.scheduleRefresh = () => {};
  c.performRefresh = async () => { throw new Error('disconnected'); };
  vm.runInContext(platformCode.slice(start, end), c);
  app.update([sweep()]); const second = app.click('run'); await c.refresh();
  assert.equal(app.requests[1].options.signal.aborted, true); app.respond(1, payload()); await second;
  assert.equal(app.dom.run.disabled, true);
  c.performRefresh = async () => {}; await c.refresh(); assert.equal(app.dom.run.disabled, false);
  assert.equal(app.requests.length, 2, 'recovery never automatically analyzes');
}
async function boundedBodies() {
  for (const length of ['2000001', null, '1']) {
    const app = setup(); app.update([sweep()]); const pending = app.click('run');
    const response = streamResponse([Buffer.alloc(1000000, ' '), Buffer.alloc(1000001, ' ')], length);
    app.requests[0].resolve(response); await pending;
    assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /2 000 000 байт/);
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(response.state.cancelled, 1);
    assert.equal(response.state.reads, length === '2000001' ? 0 : 2); assert.equal(response.state.released, 1);
  }
  const app = setup(); app.update([sweep()]); const pending = app.click('run');
  const json = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: payload() }));
  const padding = Buffer.alloc(2000000 - json.length, ' '), response = streamResponse([padding, json], '2000000');
  app.requests[0].resolve(response); await pending;
  assert.equal(app.dom.canvas.hidden, false, 'exact byte limit accepted'); assert.equal(response.state.cancelled, 0);
  const late = setup(); late.update([sweep()]); const inflight = late.click('run'); let finishRead, cancelled = 0;
  late.requests[0].resolve({ status: 200, body: { getReader: () => ({
    read: () => new Promise(resolve => { finishRead = resolve; }), cancel() { cancelled++; return Promise.resolve(); }, releaseLock() {},
  }) } });
  await new Promise(resolve => setImmediate(resolve)); late.click('clear'); assert.equal(cancelled, 1);
  const status = late.dom.status.textContent;
  finishRead({ done: false, value: Buffer.alloc(2000001) }); await inflight;
  assert.equal(late.dom.status.textContent, status); assert.equal(late.dom.canvas.hidden, true);
}
async function filesAndExport() {
  const os = require('node:os'), path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ori-mask-web-'));
  function localFile(bytes, stated = bytes.length) {
    const file = path.join(dir, 'draft.json'); fs.writeFileSync(file, bytes);
    return { size: stated, slice(start, end) { assert.equal(start, 0); assert.equal(end, 65537);
      return { async arrayBuffer() { const b = fs.readFileSync(file).subarray(start, end); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length); } }; } };
  }
  async function load(app, file) { app.dom.file.files = file ? [file] : []; await app.dom.file.listeners.change(); }
  try {
    const app = setup(); app.update([sweep()]); await app.click('save'); assert.equal(app.blobs.length, 0);
    const raw = JSON.stringify({ ...mask(), name: '  Cafe\u0301  ' }, null, 1);
    await load(app, localFile(Buffer.from(raw))); assert.equal(app.dom.editor.value, raw); assert.equal(app.requests.length, 0);
    assert.match(app.dom.draft.textContent, /не проверен/);
    let pending = app.click('run'); app.respond(0, payload(sweep(), { ...mask(), name: 'Café' })); await pending;
    assert.equal(app.dom.save.disabled, false); assert.match(app.dom.draft.textContent, /Café/);
    const summary = app.dom.summary.textContent; await load(app, null); assert.equal(app.dom.summary.textContent, summary);
    app.click('save'); assert.equal(app.downloads.length, 1); assert.equal(app.downloads[0].download, 'spectral-mask.json');
    const exported = await app.blobs[0].text(); assert.deepEqual(JSON.parse(exported), { ...mask(), name: 'Café' });
    assert.equal(app.blobs[0].type, 'application/json;charset=utf-8');
    fs.writeFileSync(path.join(dir, 'exported.json'), exported); assert.deepEqual(JSON.parse(fs.readFileSync(path.join(dir, 'exported.json'), 'utf8')), JSON.parse(exported));
    await load(app, localFile(Buffer.from(exported))); assert.equal(app.dom.save.disabled, true);
    assert.equal(app.requests.length, 1); assert.equal(app.dom.editor.value, exported);
    for (const [bytes, size] of [[Buffer.alloc(65537), 65537], [Buffer.from([0xff]), 1], [Buffer.from('short'), 6]]) {
      await load(app, localFile(bytes, size)); assert.equal(app.dom.editor.value, exported); assert.match(app.dom.status.textContent, /Не удалось загрузить/);
    }
    await load(app, { size: 1, slice() { return { arrayBuffer: async () => { throw Error('disk read error'); } }; } });
    assert.equal(app.dom.editor.value, exported); assert.match(app.dom.status.textContent, /disk read error/);
    for (const text of ['', '{', '{"name":"x","name":"y"}', '\ufeff{}', ' '.repeat(65536)]) {
      await load(app, localFile(Buffer.from(text))); assert.equal(app.dom.editor.value, text); assert.equal(app.requests.length, 1);
    }
    app.click('template'); assert.equal(app.requests.length, 1); const example = JSON.parse(app.dom.editor.value);
    assert.match(example.name, /НЕНОРМАТИВНЫЙ/); assert.deepEqual(example.vertices, [{ frequency_hz: 100, limit_dbm: -80 }, { frequency_hz: 700, limit_dbm: -80 }]);
    app.update([{ ...sweep(), frequencies_hz: Array(7).fill(100) }]); assert.equal(app.dom.template.disabled, true);
    app.click('template'); assert.deepEqual(JSON.parse(app.dom.editor.value), example); assert.match(app.dom.status.textContent, /две различные/);
    // Every invalidation discards late local reads, including a newer Analyze action.
    for (const change of ['edit', 'scope', 'clear', 'pagehide', 'analyze', 'newfile']) {
      const a = setup(); a.update([sweep()]); let finish;
      const deferred = { size: 3, slice() { return { arrayBuffer() { return new Promise(resolve => finish = resolve); } }; } };
      a.dom.file.files = [deferred]; const reading = a.dom.file.listeners.change();
      if (change === 'edit') { a.dom.editor.value = 'new'; a.dom.editor.listeners.input(); }
      if (change === 'scope') a.update([sweep()], 'new');
      if (change === 'clear') a.click('clear');
      if (change === 'pagehide') a.events.pagehide();
      if (change === 'newfile') await load(a, localFile(Buffer.from('new file')));
      let running; if (change === 'analyze') running = a.click('run');
      const before = a.dom.editor.value; finish(new TextEncoder().encode('old').buffer); await reading;
      assert.equal(a.dom.editor.value, before, `late import ${change}`);
      if (running) { a.respond(0, payload()); await running; }
    }
    // Import itself cancels an in-flight analysis and cannot revive its export.
    const a = setup(); a.update([sweep()]); pending = a.click('run'); await load(a, localFile(Buffer.from('{}')));
    assert.equal(a.requests[0].options.signal.aborted, true); a.respond(0, payload()); await pending;
    assert.equal(a.dom.save.disabled, true); assert.equal(a.dom.canvas.hidden, true);
    const b = setup(); b.update([sweep()]); pending = b.click('run'); b.respond(0, payload()); await pending;
    b.context.URL.createObjectURL = () => { throw Error('download error'); }; b.click('save'); assert.match(b.dom.status.textContent, /download error/);
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}
async function httpSmoke(base) {
  const os = require('node:os'), path = require('node:path');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ori-mask-http-'));
  try {
    const sourceResponse = await fetch(`${base}/api/v1/sessions/mask-one/sweeps?stream_id=spectrum-one&limit=2`);
    assert.equal(sourceResponse.status, 200); const sources = (await sourceResponse.json()).data;
    const app = setup(), calls = []; app.window.location.origin = base;
    app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
    app.update(sources, 'mask-one', 'spectrum-one');
    const fixture = fs.readFileSync(path.join(__dirname, '../docs/examples/spectral-mask.json'));
    const sourceFile = path.join(dir, 'import.json'); fs.writeFileSync(sourceFile, fixture);
    function file(filename) {
      return { size: fs.statSync(filename).size, slice(start, end) { return { async arrayBuffer() {
        const b = fs.readFileSync(filename).subarray(start, end); return b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
      } }; } };
    }
    app.dom.file.files = [file(sourceFile)]; await app.dom.file.listeners.change(); assert.equal(calls.length, 0);
    assert.equal(app.dom.editor.value, fixture.toString('utf8')); await app.click('run');
    assert.equal(calls.length, 1); assert.equal(app.dom.save.disabled, false, app.dom.status.textContent);
    assert.match(app.dom.summary.textContent, /one-sweep-2/); assert.match(app.dom.summary.textContent, /Превышений \(> маски\): 4/);
    assert.equal(app.dom.regions.children.length, 2); assert.match(app.dom.regions.children[0].textContent, /Индексы 30–30/);
    assert.match(app.dom.regions.children[1].textContent, /Индексы 69–71/);
    assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 202);
    app.click('save'); const exported = await app.blobs[0].text(), output = path.join(dir, 'exported.json');
    fs.writeFileSync(output, exported); assert.deepEqual(JSON.parse(exported), JSON.parse(fixture));
    app.dom.file.files = [file(output)]; await app.dom.file.listeners.change(); assert.equal(calls.length, 1);
    await app.click('run'); assert.equal(calls.length, 2); assert.equal(app.dom.save.disabled, false, app.dom.status.textContent);
    app.update([], 'different-session', 'spectrum-one'); assert.equal(app.dom.canvas.hidden, true); assert.equal(app.dom.save.disabled, true);
    console.log('Web HTTP mask smoke passed: import, 101 bins, 2 regions / 4 exceedances, canonical export and reanalysis');
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}
(async () => {
  await happyAndFrozen(); await badInputsAndStatus(); await corruptResponses(); await staleAndLifecycle();
  await limitsAndEquality(); await platformIntegration(); await boundedBodies(); await filesAndExport(); if (process.argv[4]) await httpSmoke(process.argv[4]); console.log('mask behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
