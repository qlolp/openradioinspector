/* Production handlers, synthetic archive responses and adversarial transport timing. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');
const copy = structuredClone;
function sweep(number = 4, count = 7, stream = 'tiny/1') {
  return { id: `source-${number}`, stream_id: stream, level_unit: 'dBm',
    interval: { started_at: `2026-11-08T12:30:${String(number).padStart(2, '0')}.000001+00:00`,
      ended_at: `2026-11-08T12:31:${String(number).padStart(2, '0')}.000002+00:00` },
    frequencies_hz: Array.from({ length: count }, (_, i) => 100 + i * 100), levels: Array(count).fill(-90 + number),
    settings: { rbw_hz: 1000, active: true, nested: { array: [1, 'known'] }, sequence: number } };
}
function sources(count = 7) { return [7, 2, 5, 1, 6, 3, 4].map(i => sweep(i, count)); }
function payload(all = sources(), anchor = 'source-4', before = 2, after = 2, session = 'session/1') {
  // Test oracle only: core owns the actual selection operation.
  const sorted = copy(all).sort((a, b) => a.interval.started_at === b.interval.started_at ?
    Buffer.compare(Buffer.from(a.id), Buffer.from(b.id)) : a.interval.started_at < b.interval.started_at ? -1 : 1);
  const pivot = sorted.findIndex(item => item.id === anchor), source = sorted[pivot];
  const available = { before: Math.min(before, pivot), after: Math.min(after, sorted.length - pivot - 1) };
  const missing = { before: before - available.before, after: after - available.after };
  return { session_id: session, stream_id: source.stream_id, anchor_id: anchor, order: 'started_at_then_id',
    requested: { before, after }, available, missing, complete: missing.before === 0 && missing.after === 0,
    points: source.levels.length, frequencies_hz: [...source.frequencies_hz], level_unit: 'dBm',
    items: sorted.slice(pivot - available.before, pivot + available.after + 1).map((item, i) => {
      const { id, interval, levels, level_unit, settings } = item;
      return { offset: i - available.before, id, interval, levels, level_unit, settings };
    }) };
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
  for (const name of ['anchor', 'before', 'after', 'run', 'clear', 'source', 'pinned', 'status', 'summary', 'items', 'selected', 'canvas', 'threshold', 'occupancy-run', 'occupancy']) dom[name] = element();
  dom.before.value = dom.after.value = '2'; dom.threshold.value = '-60';
  const window = { location: { origin: 'http://server-a' }, addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) { return dom[id.replace('context-', '')]; }, createElement: element },
    AbortController, TextDecoder, TextEncoder, fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  function update(sweeps = sources(), sessionId = 'session/1', streamId = 'tiny/1') { window.OpenRadioSweepContext.update({ sweeps, sessionId, streamId }); }
  function click(name) { return dom[name].listeners.click(); }
  function choose(id) { dom.anchor.value = id; dom.anchor.listeners.change(); }
  function respond(index, data = payload(), status = 200, envelope = {}) {
    const json = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope },
      (_key, value) => typeof value === 'number' && !Number.isFinite(value) ? `__NONFINITE_${value}__` : value)
      .replace(/"__NONFINITE_Infinity__"/g, '1e999').replace(/"__NONFINITE_-Infinity__"/g, '-1e999').replace(/"__NONFINITE_NaN__"/g, 'NaN');
    const body = Buffer.from(json); requests[index].resolve(streamResponse([body], String(body.length), status));
  }
  return { dom, events, requests, timers, drawing, window, context, update, click, choose, respond };
}
function streamResponse(chunks, length = null, status = 200) {
  const state = { reads: 0, cancelled: 0, released: 0 };
  return { status, state, headers: { get: () => length }, body: { getReader: () => ({
    async read() { const value = chunks[state.reads++]; return value === undefined ? { done: true } : { done: false, value }; },
    cancel() { state.cancelled++; return Promise.resolve(); }, releaseLock() { state.released++; },
  }) } };
}
async function happyAndFrozen() {
  const app = setup(), all = sources(), anchor = all.at(-1); anchor.id = '<img onerror=alert(1)> /?&'; const frozen = copy(all);
  app.update([...all, sweep(8, 3, 'other'), { ...sweep(9), level_unit: 's_meter_count' }]);
  assert.equal(app.requests.length, 0); assert.equal(app.dom.anchor.children.length, 7);
  assert.equal(app.dom.anchor.value, anchor.id);
  const promise = app.click('run'); app.click('run');
  assert.equal(app.requests.length, 1, 'single flight'); assert.equal(app.dom.run.disabled, true);
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-context'); assert.equal(url.search, '');
  assert.deepEqual(JSON.parse(app.requests[0].options.body), { sweep_id: anchor.id, before: 2, after: 2 });
  assert.equal(app.requests[0].options.method, 'POST'); assert.equal(app.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(app.requests[0].options.cache, 'no-store');
  anchor.levels[0] = -100; anchor.settings.sequence = 99; anchor.settings.nested.array[0] = 999; anchor.interval.started_at = 'bad';
  app.respond(0, payload(frozen, anchor.id)); await promise;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent); assert.equal(app.timers.size, 0);
  assert.match(app.dom.pinned.textContent, /04\.000001\+00:00/); assert.match(app.dom.pinned.textContent, /"sequence":4/);
  assert.match(app.dom.summary.textContent, /Полное окно: 5 свипов × 7 точек = 35/);
  assert.match(app.dom.summary.textContent, /Доступно до: 2, после: 2/); assert.match(app.dom.summary.textContent, /Недостаёт до: 0, после: 0/);
  assert.match(app.dom.status.textContent, /Автоматический захват N1\/N2 при обнаружении не реализован/);
  assert.equal(app.dom.items.children.length, 5); assert.equal(app.dom.items.children[2].children[0]['aria-pressed'], 'true');
  assert(app.dom.items.children[2].children[1].textContent.includes(anchor.id));
  for (const [index, number] of [[0, 2], [1, 3], [3, 5], [4, 6]]) {
    app.dom.items.children[index].children[0].listeners.click(); assert.match(app.dom.selected.textContent, new RegExp(`source-${number}`));
    assert.match(app.dom.selected.textContent, /контекст, не якорь/); assert.equal(app.dom.items.children[index].children[0]['aria-pressed'], 'true');
  }
  const summary = app.dom.summary.textContent, pinned = app.dom.pinned.textContent, selected = app.dom.selected.textContent;
  app.update([sweep(10), sweep(11)]);
  assert.equal(app.requests.length, 1); assert.equal(app.dom.summary.textContent, summary); assert.equal(app.dom.pinned.textContent, pinned);
  assert.equal(app.dom.selected.textContent, selected);
  app.update(sources()); app.choose('source-2'); const second = app.click('run'); app.respond(1, payload(sources(), 'source-2')); await second;
  assert.match(app.dom.summary.textContent, /Неполное окно архива: 4/); assert.match(app.dom.summary.textContent, /Недостаёт до: 1, после: 0/);
  app.update([...sources().reverse(), sweep(9)]); assert.equal(app.dom.anchor.value, 'source-2', 'poll preserves explicit older anchor');
  app.dom.before.value = '0'; app.dom.before.listeners.input(); assert.equal(app.dom.summary.textContent, '');
  assert.equal(app.dom.pinned.textContent, 'Якорь не закреплён.'); assert.equal(app.dom.items.children.length, 0);
  app.click('clear'); assert.equal(app.requests.length, 2);
}
async function inputBoundaries() {
  for (const value of ['', ' ', 'NaN', 'Infinity', '1e1', '1.0', '-1', '17', '0x10', 'false', '1,2']) for (const name of ['before', 'after']) {
    const app = setup(); app.update(); app.dom[name].value = value; await app.click('run');
    assert.equal(app.requests.length, 0, `${name}: ${value}`); assert.match(app.dom.status.textContent, /целые числа/);
  }
  for (const [before, after] of [[0, 0], [0, 16], [16, 0], [16, 16]]) {
    const app = setup(); app.update(); app.dom.before.value = String(before); app.dom.after.value = String(after);
    const p = app.click('run'); app.respond(0, payload(sources(), 'source-4', before, after)); await p;
    assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  }
  const badSources = [s => s.id = '', s => s.id = ' ', s => s.id = '😀'.repeat(129), s => s.id = '\ud800', s => s.id = '\udc00',
    s => s.settings = {}, s => s.settings = { sequence: 1 }, s => s.settings.rbw_hz = null, s => s.settings.nested.array[0] = null,
    s => s.settings.sequence = Infinity, s => s.frequencies_hz[0] = true, s => s.frequencies_hz[0] = -1, s => s.frequencies_hz[0] = 1.5,
    s => s.frequencies_hz[0] = Number.MAX_SAFE_INTEGER + 1, s => s.frequencies_hz[1] = 0, s => s.frequencies_hz.pop(),
    s => s.levels[0] = Infinity, s => s.levels[0] = true, s => s.interval = null,
    s => s.interval.started_at = '2026-02-30T00:00:00Z', s => s.interval.started_at += '\n', s => s.interval.started_at = '0000-01-01T00:00:00Z',
    s => s.interval.started_at = '2026-11-08T12:30:00.0000010Z', s => s.interval.started_at = '2026-11-08T12:30:00+01:00',
    s => s.interval.ended_at = '2026-11-08T12:29:59Z'];
  for (const mutate of badSources) {
    const app = setup(), source = sweep(); mutate(source); app.update([source]); await app.click('run');
    assert.equal(app.requests.length, 0, String(mutate)); assert.equal(app.dom.canvas.hidden, true);
  }
  for (const count of [0, 16385]) { const app = setup(); app.update([sweep(4, count)]); await app.click('run'); assert.equal(app.requests.length, 0); }
  for (const all of [[], [{ ...sweep(), level_unit: 's_meter_count' }], [sweep(4, 7, 'other')], [sweep(), sweep()],
    [{ ...sweep(), session_id: 'foreign' }], [{ ...sweep(), session_id: null }]]) {
    const app = setup(); app.update(all); await app.click('run'); assert.equal(app.requests.length, 0);
  }
  const app = setup(), source = sweep(); source.id = '😀'.repeat(128); source.settings.sequence = { nullable: [null] };
  app.update([source]); const p = app.click('run'); app.respond(0, payload([source], source.id)); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  const long = setup(), session = 'session'.repeat(100), stream = 'stream'.repeat(100), scoped = { ...sweep(4, 7, stream), session_id: session };
  long.update([scoped], session, stream); const q = long.click('run'); long.respond(0, payload([scoped], scoped.id, 2, 2, session)); await q;
  assert.equal(long.dom.canvas.hidden, false, long.dom.status.textContent);
  const changed = setup(), original = { ...sweep(), session_id: 'session/1' }; changed.update([original]); original.session_id = 'foreign';
  await changed.click('run'); assert.equal(changed.requests.length, 0, 'mutation after update must not bypass scope validation');
}
async function corruptResponses() {
  const changes = [d => d.session_id = 'wrong', d => d.stream_id = 'wrong', d => d.anchor_id = 'wrong', d => d.order = 'arrival',
    d => d.level_unit = 'dB', d => d.points = true, d => d.points = 0, d => d.points = 16385, d => d.frequencies_hz.pop(),
    d => d.frequencies_hz[0] = true, d => d.frequencies_hz[0] = -1, d => d.frequencies_hz[0] = 100.5,
    d => d.frequencies_hz[0] = 9007199254740992, d => d.frequencies_hz[1] = 99, d => d.frequencies_hz[0] = 101,
    d => d.requested = null, d => d.requested.before = true, d => d.requested.after = 1, d => d.available = null,
    d => d.available.before = true, d => d.available.after = -1, d => d.available.after = 3, d => d.missing = null,
    d => d.missing.before = true, d => d.missing.after = 1, d => d.complete = 1, d => d.complete = false,
    d => d.items = null, d => d.items.pop(), d => d.items.push(copy(d.items[0])), d => d.items.reverse(), d => d.items[0] = null,
    d => d.items[0].offset = true, d => d.items[0].offset = -3, d => d.items[2].offset = 1,
    d => d.items[2].id = 'wrong', d => d.items[0].id = 'source-3', d => d.items[0].id = '\ud800', d => d.items[0].id = '😀'.repeat(129),
    d => d.items[0].level_unit = 's_meter_count', d => d.items[0].levels.pop(), d => d.items[0].levels[0] = true,
    d => d.items[0].levels[0] = Infinity, d => d.items[2].levels[0] = -100, d => d.items[2].settings.sequence = 99,
    d => d.items[0].settings.nested.array[0] = true, d => d.items[0].settings.extra = 1, d => d.items[0].settings.rbw_hz = null,
    d => d.items[0].settings = {}, d => d.items[0].settings = { sequence: 1 }, d => d.items[0].settings.sequence = Infinity,
    d => d.items[0].interval = null, d => d.items[0].interval.started_at += '\n', d => d.items[0].interval.started_at = '2026-02-30T00:00:00Z',
    d => d.items[0].interval.started_at = '0000-11-08T12:30:00.000001Z', d => d.items[0].interval.started_at = '2026-11-08T12:30:00.0000010Z',
    d => d.items[0].interval.started_at = '2026-11-08T13:30:00.000001+01:00',
    d => d.items[2].interval.started_at = '2026-11-08T12:30:04.000002Z', d => d.items[2].interval.ended_at = '2026-11-08T12:31:04.000003Z',
    d => d.items[0].interval.started_at = '2026-11-08T12:32:00Z'];
  for (const change of changes) {
    const app = setup(); app.update(); const p = app.click('run'), data = payload(); change(data); app.respond(0, data); await p;
    assert.equal(app.dom.canvas.hidden, true, String(change)); assert.match(app.dom.status.textContent, /Контекст недоступен/, String(change));
    assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.items.children.length, 0);
  }
  const app = setup(); app.update(); const p = app.click('run'), data = payload();
  data.items.forEach(item => { item.settings = Object.fromEntries(Object.entries(item.settings).reverse()); });
  data.items[2].interval.started_at = '2026-11-08T12:30:04.000001Z';
  data.items[0].settings.sequence = { nullable: [null] }; app.respond(0, data); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
}
async function exactOrder() {
  for (const ids of [['a', 'aa', 'b'], ['e\u0301', 'é', '😀'], ['\ue000', '𐀀', '😀']]) {
    const all = ids.map(id => ({ ...sweep(), id })), app = setup(); app.update(all); app.choose(ids[1]);
    const p = app.click('run'); app.respond(0, payload(all, ids[1])); await p;
    assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
    const bad = payload(all, ids[1]); [bad.items[0], bad.items[1]] = [bad.items[1], bad.items[0]];
    bad.items.forEach((item, i) => item.offset = i - 1);
    const q = app.click('run'); app.respond(1, bad); await q; assert.equal(app.dom.canvas.hidden, true);
  }
  const all = ['z', 'a', 'b'].map((id, i) => ({ ...sweep(), id, interval: {
    started_at: `2026-11-08T12:30:04.00000${i + 1}Z`, ended_at: '2026-11-08T12:30:05Z' } }));
  const app = setup(); app.update(all); app.choose('a'); const p = app.click('run'); app.respond(0, payload(all, 'a')); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
}
async function staleAndLifecycle() {
  for (const change of ['clear', 'anchor', 'before', 'after', 'scope', 'stream', 'server', 'offline', 'connection', 'pagehide', 'timeout']) {
    for (const outcome of ['success', 'error']) {
      const app = setup(); app.update(); const p = app.click('run');
      if (change === 'clear') app.click('clear');
      if (change === 'anchor') app.choose('source-2');
      if (['before', 'after'].includes(change)) { app.dom[change].value = '1'; app.dom[change].listeners.input(); }
      if (change === 'scope') app.update(sources(), 'other-session');
      if (change === 'stream') app.update(sources(), 'session/1', 'other-stream');
      if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update(); }
      if (change === 'offline') app.events.offline();
      if (change === 'connection') app.window.OpenRadioSweepContext.setConnected(false);
      if (change === 'pagehide') app.events.pagehide();
      if (change === 'timeout') { const timer = [...app.timers.values()][0]; assert.equal(timer.ms, 8000); timer.fn(); }
      assert.equal(app.requests[0].options.signal.aborted, true, change);
      const before = app.dom.status.textContent;
      if (outcome === 'success') app.respond(0); else app.requests[0].reject(new Error('late failure'));
      await p; assert.equal(app.dom.status.textContent, before, change); assert.equal(app.dom.canvas.hidden, true);
      assert.equal(app.dom.pinned.textContent, 'Якорь не закреплён.');
      if (['offline', 'connection'].includes(change)) app.window.OpenRadioSweepContext.setConnected(true);
      if (change === 'pagehide') app.events.pageshow();
      assert.equal(app.requests.length, 1, 'recovery cannot autorun');
    }
  }
  const app = setup(); app.update(); const first = app.click('run'); app.click('clear'); const second = app.click('run');
  app.respond(1); await second; const text = app.dom.summary.textContent, status = app.dom.status.textContent;
  app.requests[0].reject(new Error('late previous error')); await first;
  assert.equal(app.dom.summary.textContent, text); assert.equal(app.dom.status.textContent, status);
  const staleButton = app.dom.items.children[0].children[0]; app.click('clear'); staleButton.listeners.click();
  assert.equal(app.dom.canvas.hidden, true, 'detached row cannot restore old result');
}
async function retainedAnchorReload() {
  const app = setup(), initial = sources(); app.update(initial); app.choose('source-4');
  let p = app.click('run'); app.respond(0); await p;
  const frozen = app.dom.pinned.textContent; app.update([sweep(8), sweep(9)]);
  assert.equal(app.dom.anchor.value, 'source-4'); assert.equal(app.dom.pinned.textContent, frozen);
  assert.equal(app.dom.anchor.children.length, 3); assert.match(app.dom.anchor.children.at(-1).textContent, /сохранённый выбор/);
  app.dom.after.value = '4'; app.dom.after.listeners.input(); assert.equal(app.dom.pinned.textContent, 'Якорь не закреплён.');
  p = app.click('run'); assert.deepEqual(JSON.parse(app.requests[1].options.body), { sweep_id: 'source-4', before: 2, after: 4 });
  app.respond(1, payload([...initial, sweep(8), sweep(9)], 'source-4', 2, 4)); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent); assert.equal(app.dom.items.children.length, 7);
  assert.equal(app.dom.pinned.textContent, frozen);
  app.update([]); app.click('clear'); p = app.click('run'); app.respond(2, payload(initial, 'source-4', 2, 4)); await p;
  assert.equal(app.dom.canvas.hidden, false); assert.match(app.dom.summary.textContent, /Недостаёт до: 0, после: 1/);
  app.window.OpenRadioSweepContext.setConnected(false); app.window.OpenRadioSweepContext.setConnected(true);
  assert.equal(app.dom.run.disabled, true, 'disconnect discards retained source outside current page');
  await app.click('run'); assert.equal(app.requests.length, 3);
}
async function bodiesAndStatuses() {
  for (const status of [201, 204, 206, 400, 404, 409, 413, 500]) {
    const app = setup(); app.update(); const p = app.click('run'); app.respond(0, payload(), status); await p;
    assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /Контекст недоступен/); assert.equal(app.dom.run.disabled, false);
  }
  for (const status of [200, 201, 400, 404, 409, 413, 500]) for (const length of ['2000001', null, '1']) {
    const app = setup(); app.update(); const p = app.click('run');
    const response = streamResponse([Buffer.alloc(1000000, ' '), Buffer.alloc(1000001, ' ')], length, status);
    app.requests[0].resolve(response); await p; assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /2 000 000 байт/);
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(response.state.cancelled, 1);
    assert.equal(response.state.reads, length === '2000001' ? 0 : 2); assert.equal(response.state.released, 1);
  }
  const app = setup(); app.update(); let p = app.click('run');
  const json = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: payload() }));
  app.requests[0].resolve(streamResponse([Buffer.alloc(2000000 - json.length, ' '), json], '2000000')); await p;
  assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  for (const value of ['{', Buffer.from([0xff]), '\ufeff{}']) {
    p = app.click('run'); app.requests.at(-1).resolve(streamResponse([Buffer.from(value)])); await p; assert.equal(app.dom.canvas.hidden, true);
  }
  for (const replacement of ['"points":7,"points":7', '"points":7,"poin\\u0074s":7', '"extra":1e999,"points":7']) {
    p = app.click('run'); app.requests.at(-1).resolve(streamResponse([Buffer.from(json.toString().replace('"points":7', replacement))]));
    await p; assert.equal(app.dom.canvas.hidden, true, replacement);
  }
  for (const envelope of [{ contract: 'wrong' }, { contract_version: true }, { contract_version: 2 }, { data: null }]) {
    p = app.click('run'); app.respond(app.requests.length - 1, payload(), 200, envelope); await p; assert.equal(app.dom.canvas.hidden, true);
  }
  p = app.click('run'); app.requests.at(-1).reject(new Error('network failed')); await p; assert.match(app.dom.status.textContent, /network failed/);
  p = app.click('run'); app.requests.at(-1).resolve({ status: 200, body: null }); await p; assert.match(app.dom.status.textContent, /потоковый/);
  const late = setup(); late.update(); const running = late.click('run'); let finish, cancelled = 0;
  late.requests[0].resolve({ status: 200, body: { getReader: () => ({
    read: () => new Promise(resolve => finish = resolve), cancel() { cancelled++; return Promise.resolve(); }, releaseLock() {},
  }) } });
  await new Promise(resolve => setImmediate(resolve)); late.click('clear'); assert.equal(cancelled, 1);
  const before = late.dom.status.textContent; finish({ done: false, value: json }); await running;
  assert.equal(late.dom.status.textContent, before); assert.equal(late.dom.canvas.hidden, true);
}
async function fullDrawing() {
  for (const [itemCount, points] of [[1, 1], [1, 3], [8, 16384], [33, 3971]]) {
    const all = Array.from({ length: itemCount }, (_, i) => sweep(i + 1, points));
    if (itemCount === 1) { all[0].frequencies_hz.fill(100); all[0].levels = all[0].levels.map((_, i) => i % 2 ? -1e308 : 1e308); }
    const app = setup(); app.update(all); const pivot = Math.floor(itemCount / 2); app.choose(all[pivot].id);
    app.dom.before.value = String(pivot); app.dom.after.value = String(itemCount - pivot - 1);
    const p = app.click('run'); app.respond(0, payload(all, all[pivot].id, pivot, itemCount - pivot - 1)); await p;
    assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent); assert.equal(app.dom.items.children.length, itemCount);
    app.drawing.length = 0;
    app.dom.items.children.forEach(row => row.children[0].listeners.click());
    const coordinates = app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0]));
    assert.equal(coordinates.length, itemCount * points, 'all bins for every selected spectrum, no truncation');
    assert(coordinates.every(row => row.slice(1).every(Number.isFinite)));
    assert.equal(app.drawing.filter(row => row[0] === 'fillRect' && row[3] === 4).length, itemCount, 'single/repeated-frequency marker');
  }
  const all = Array.from({ length: 9 }, (_, i) => sweep(i + 1, 16384)), app = setup(); app.update(all); app.choose('source-5');
  app.dom.before.value = app.dom.after.value = '4'; const p = app.click('run'); app.respond(0, payload(all, 'source-5', 4, 4)); await p;
  assert.equal(app.dom.canvas.hidden, true); assert.match(app.dom.status.textContent, /размер/);
}
async function platformIntegration() {
  const app = setup(); app.update(); const pending = app.click('run'), c = app.context;
  c.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: sources(), refreshRunning: false };
  c.dom = { sessionSelect: { value: 'different' }, spectrumStream: { value: 'other' }, health: {}, candidateForm: {reset() {}, dataset: {}}, candidateEditor: {} };
  c.storeSession = () => {}; c.closeHandoff = () => {}; c.refresh = () => assert(app.requests[0].options.signal.aborted);
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) {
    const start = platformCode.indexOf(`  function ${name}(`), end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0); vm.runInContext(platformCode.slice(start, end), c);
  }
  c.changeSession(); app.respond(0); await pending; assert.equal(app.dom.canvas.hidden, true);
  c.changeSpectrumStream(); assert.equal(app.requests.length, 1);
  const start = platformCode.indexOf('  async function refresh('), end = platformCode.indexOf('\n  }', start) + 4;
  c.renderSession = () => {}; c.renderAll = () => {}; c.reportError = () => {}; c.scheduleRefresh = () => {};
  c.performRefresh = async () => { throw new Error('disconnected'); }; vm.runInContext(platformCode.slice(start, end), c);
  app.update(); const second = app.click('run'); await c.refresh(); assert.equal(app.requests[1].options.signal.aborted, true);
  app.respond(1); await second; assert.equal(app.dom.run.disabled, true);
  c.performRefresh = async () => {}; await c.refresh(); assert.equal(app.dom.run.disabled, false); assert.equal(app.requests.length, 2);
  const handlers = {}; c.API_ROOT = '/api/v1'; c.encode = encodeURIComponent; c.EVENT_TYPES = [];
  c.setSseStatus = () => {}; c.EventSource = function () { this.addEventListener = (name, fn) => handlers[name] = fn; this.close = () => {}; };
  c.window.EventSource = c.EventSource;
  const eventStart = platformCode.indexOf('  function connectEvents('), eventEnd = platformCode.indexOf('\n  }', eventStart) + 4;
  vm.runInContext(platformCode.slice(eventStart, eventEnd), c); c.connectEvents();
  const third = app.click('run'); handlers.error(); assert.equal(app.requests[2].options.signal.aborted, true);
  app.respond(2); await third; assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.run.disabled, true);
  await c.refresh(); assert.equal(app.dom.run.disabled, false); assert.equal(app.requests.length, 3);
}
async function httpSmoke(base) {
  const sourceResponse = await fetch(`${base}/api/v1/sessions/context-one/sweeps?stream_id=spectrum-one&limit=40`);
  assert.equal(sourceResponse.status, 200); const all = (await sourceResponse.json()).data;
  const app = setup(), calls = []; app.window.location.origin = base;
  app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
  app.update(all, 'context-one', 'spectrum-one'); app.choose('one-sweep-4'); assert.equal(calls.length, 0);
  await app.click('run'); assert.equal(calls.length, 1); assert.equal(app.dom.canvas.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.pinned.textContent, /one-sweep-4/); assert.match(app.dom.pinned.textContent, /40\.000001/);
  assert.match(app.dom.summary.textContent, /5 свипов × 101 точек = 505/);
  assert.equal(app.dom.items.children.length, 5);
  app.drawing.length = 0;
  app.dom.items.children.forEach((row, index) => {
    assert.match(row.children[1].textContent, new RegExp(`one-sweep-${index + 2}`)); row.children[0].listeners.click();
    assert.match(app.dom.selected.textContent, new RegExp(`offset ${index - 2}`));
  });
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 505);
  const frozen = app.dom.summary.textContent, pinned = app.dom.pinned.textContent;
  app.update([], 'context-one', 'spectrum-one'); assert.equal(app.dom.summary.textContent, frozen); assert.equal(app.dom.pinned.textContent, pinned);
  app.update(all, 'context-one', 'spectrum-one'); app.choose('one-sweep-1'); await app.click('run');
  assert.match(app.dom.summary.textContent, /Недостаёт до: 2, после: 0/); assert.equal(calls.length, 2);
  app.dom.after.value = '0'; app.dom.after.listeners.input(); assert.equal(app.dom.canvas.hidden, true);
  await app.click('run'); assert.equal(calls.length, 3); assert.equal(app.dom.items.children.length, 1);
  app.dom.before.value = app.dom.after.value = '2'; app.dom.threshold.value = '-90';
  app.choose('one-sweep-4'); await app.click('occupancy-run');
  assert.equal(calls.length, 4); assert.match(calls[3].url, /sweep-occupancy$/);
  assert.match(app.dom.occupancy.textContent, /Порог: > -90 dBm · 5 свипов/);
  assert.match(app.dom.occupancy.textContent, /Разрывы: 4,/);
  assert.match(app.dom.occupancy.textContent, /5\/5 · 100.0%/);
  assert.match(app.dom.occupancy.textContent, /20 из 101/);
  console.log('Web HTTP occupancy smoke passed: exact sources, sample fractions, acquisition gaps');
  app.update([], 'context-two', 'spectrum-two'); assert.equal(app.dom.run.disabled, true); assert.equal(app.dom.summary.textContent, '');
  console.log('Web HTTP context smoke passed: 5 × 101 raw bins, offsets -2..2, anchor 4, incomplete side, repeated load, edit, scope reset');
}
async function occupancyAnalysis() {
  const app = setup(); app.update();
  const response = payload();
  response.occupancy = { method: 'sample_fraction', comparison: 'strictly_greater', threshold_dbm: -60,
    sample_count: response.items.length, exceedance_counts: Array(response.points).fill(1),
    sample_fractions: Array(response.points).fill(1 / response.items.length),
    acquisition_span_seconds: 6, acquisition_covered_seconds: 4, acquisition_gap_seconds: 2,
    acquisition_gap_count: 1, rf_time_occupancy: null };
  const first = app.click('occupancy-run');
  assert.match(app.requests[0].url, /sweep-occupancy$/);
  assert.equal(JSON.parse(app.requests[0].options.body).threshold_dbm, -60);
  app.respond(0, response); await first;
  assert.match(app.dom.occupancy.textContent, /Порог: > -60 dBm/);
  assert.match(app.dom.occupancy.textContent, /Разрывы: 1, всего 2 с/);
  const frozen = app.dom.occupancy.textContent;
  app.update([sweep(99)]); assert.equal(app.dom.occupancy.textContent, frozen);
  app.dom.threshold.value = '-70'; app.dom.threshold.listeners.input();
  assert.equal(app.dom.occupancy.textContent, '');
  const bad = app.click('occupancy-run'); app.respond(1, response); await bad;
  assert.equal(app.dom.occupancy.textContent, ''); assert.equal(app.dom.canvas.hidden, true);
  app.dom.threshold.value = '-60';
  const late = app.click('occupancy-run'); app.update([], 'new-session', 'new-stream');
  app.respond(2, response); await late; assert.equal(app.dom.occupancy.textContent, '');
  assert.equal(app.dom['occupancy-run'].disabled, true);
  const invalid = setup(); invalid.update(); invalid.dom.threshold.value = '';
  await invalid.click('occupancy-run'); assert.equal(invalid.requests.length, 0);
  for (const change of [ {sample_count: 99}, {rf_time_occupancy: 0.5}, {acquisition_gap_seconds: -1},
      {sample_fractions: Array(response.points).fill(2)}, {exceedance_counts: []} ]) {
    const broken = setup(); broken.update(); const work = broken.click('occupancy-run');
    broken.respond(0, {...response, occupancy: {...response.occupancy, ...change}}); await work;
    assert.equal(broken.dom.occupancy.textContent, ''); assert.equal(broken.dom.canvas.hidden, true);
  }
}
(async () => {
  await occupancyAnalysis(); await happyAndFrozen(); await inputBoundaries(); await corruptResponses(); await exactOrder(); await staleAndLifecycle(); await retainedAnchorReload();
  await bodiesAndStatuses(); await fullDrawing(); await platformIntegration(); if (process.argv[4]) await httpSmoke(process.argv[4]);
  console.log('sweep-context behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
