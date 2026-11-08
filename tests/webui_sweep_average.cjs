/* Execute production handlers with synthetic data and adversarial transport timing. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');
const copy = structuredClone;
function sweep(id = 'source-1', count = 7, sequence = 1, stream = 'tiny/1') {
  return { id, stream_id: stream, level_unit: 'dBm',
    interval: { started_at: `2026-11-08T12:30:00.00000${sequence}+00:00`, ended_at: `2026-11-08T12:30:01.00000${sequence}+00:00` },
    frequencies_hz: Array.from({ length: count }, (_, i) => 100 + i * 100), levels: Array(count).fill(-60),
    settings: { rbw_hz: 1000, active: true, nested: { array: [1, 'known'] }, sequence } };
}
function group(count = 7, n = 3) { return Array.from({ length: n + 1 }, (_, i) => sweep(`source-${i + 1}`, count, i + 1)); }
function payload(sources = group().slice(0, -1), target = group().at(-1), session = 'session/1') {
  // Synthetic equal-level sources need no client-side averaging oracle.
  const levels = [...sources[0].levels], delta = target.levels.map((v, i) => v - levels[i]);
  const d = { session_id: session, stream_id: target.stream_id, points: target.levels.length, frequencies_hz: [...target.frequencies_hz],
    reference: { method: 'mean_power', count: sources.length, source_ids: sources.map(s => s.id),
      sources: sources.map(s => ({ id: s.id, interval: copy(s.interval), settings: copy(s.settings) })),
      interval: { started_at: sources.map(s => s.interval.started_at).sort()[0], ended_at: sources.map(s => s.interval.ended_at).sort().at(-1) },
      levels, level_unit: 'dBm' }, target: copy(target), delta_levels: delta, delta_unit: 'dB' };
  summarize(d); return d;
}
function summarize(d) {
  for (const [key, value] of [['max_increase', Math.max(...d.delta_levels)], ['min_change', Math.min(...d.delta_levels)]]) {
    d[key] = { frequency_hz: d.frequencies_hz[d.delta_levels.indexOf(value)], delta_db: value };
  }
}
function detection(d, minimumIncreaseDb = 6, minimumBins = 2) {
  const regions = []; let start = null;
  function append(end) {
    if (start === null || end - start + 1 < minimumBins) return;
    let peak = start;
    for (let i = start + 1; i <= end; i++) if (d.delta_levels[i] > d.delta_levels[peak]) peak = i;
    regions.push({ start_index: start, end_index: end, start_frequency_hz: d.frequencies_hz[start],
      end_frequency_hz: d.frequencies_hz[end], peak_index: peak, peak_frequency_hz: d.frequencies_hz[peak],
      peak_increase_db: d.delta_levels[peak], bin_count: end - start + 1 });
  }
  for (let i = 0; i < d.points; i++) {
    if (d.delta_levels[i] >= minimumIncreaseDb) { if (start === null) start = i; }
    else if (start !== null) { append(i - 1); start = null; }
  }
  if (start !== null) append(d.points - 1);
  return { comparison: d, detection: { minimum_increase_db: minimumIncreaseDb, minimum_bins: minimumBins,
    regions, count: regions.length } };
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map(), drawing = []; let timerId = 0;
  const context2d = new Proxy({}, { get(target, key) { return target[key] || ((...args) => drawing.push([key, ...args])); },
    set(target, key, value) { target[key] = value; drawing.push([key, value]); return true; } });
  function element() {
    const result = { value: '', textContent: '', hidden: false, disabled: false, width: 960, height: 280, listeners: {},
      addEventListener(event, fn) { this.listeners[event] = fn; }, setAttribute(name, value) { this[name] = value; }, getContext() { return context2d; } };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['count', 'threshold', 'minimumBins', 'pin', 'run', 'clear', 'source', 'reference', 'status', 'summary', 'overlay', 'delta']) dom[name] = element();
  dom.count.value = '3'; dom.threshold.value = '6'; dom.minimumBins.value = '2';
  const window = { location: { origin: 'http://server-a' }, addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) {
    if (id === 'average-detect-threshold') return dom.threshold;
    if (id === 'average-detect-bins') return dom.minimumBins;
    return dom[id.replace('average-', '')];
  } },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  function update(sweeps, sessionId = 'session/1', streamId = 'tiny/1') { window.OpenRadioSweepAverage.update({ sweeps, sessionId, streamId }); }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const result = data?.comparison ? data : detection(data);
    const json = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: result, ...envelope },
      (_key, value) => typeof value === 'number' && !Number.isFinite(value) ? `__NONFINITE_${value}__` : value)
      .replace(/"__NONFINITE_Infinity__"/g, '1e999').replace(/"__NONFINITE_-Infinity__"/g, '-1e999')
      .replace(/"__NONFINITE_NaN__"/g, 'NaN');
    const body = Buffer.from(json); requests[index].resolve(streamResponse([body], String(body.length), status));
  }
  function pin(sweeps = group(), n = 3) { dom.count.value = String(n); update(sweeps); click('pin'); }
  return { dom, events, requests, timers, drawing, window, context, update, click, respond, pin };
}
function streamResponse(chunks, length = null, status = 200) {
  const state = { reads: 0, cancelled: 0, released: 0 };
  return { status, state, headers: { get: () => length }, body: { getReader: () => ({
    async read() { const value = chunks[state.reads++]; return value === undefined ? { done: true } : { done: false, value }; },
    cancel() { state.cancelled++; return Promise.resolve(); }, releaseLock() { state.released++; },
  }) } };
}
async function happyAndFrozen() {
  const app = setup(), all = group(); all[1].id = '<img onerror=alert(1)> /?&';
  all.at(-1).levels = [-61, -55, -55, -60, -62, -60, -60];
  const sources = copy(all.slice(0, -1)), target = copy(all.at(-1));
  app.update([sweep('old'), ...all, sweep('other-stream', 3, 1, 'other'), { ...sweep('receiver'), level_unit: 's_meter_count' }]);
  assert.equal(app.requests.length, 0); app.click('pin'); assert.equal(app.requests.length, 0, 'pinning is local');
  assert.match(app.dom.reference.textContent, /N=3.*mean_power/); assert(app.dom.reference.textContent.includes(all[1].id));
  assert(app.dom.reference.textContent.includes('00.000001+00:00'), 'preserve microseconds');
  assert(!app.dom.reference.textContent.includes(target.id), 'exclude latest dBm target');
  const pinnedText = app.dom.reference.textContent;
  all[0].settings.nested.array[0] = 123; all[0].levels[0] = -500; all[0].interval.started_at = 'bad';
  const promise = app.click('run'); app.click('run'); assert.equal(app.requests.length, 1, 'single flight');
  assert.equal(app.dom.run.disabled, true); assert.equal(app.dom.pin.disabled, true);
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-average-change-detection'); assert.equal(url.search, '');
  assert.deepEqual(JSON.parse(app.requests[0].options.body), { reference_ids: sources.map(s => s.id), target_id: target.id, minimum_increase_db: 6, minimum_bins: 2 });
  assert.equal(app.requests[0].options.method, 'POST'); assert.equal(app.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(app.requests[0].options.cache, 'no-store');
  all.at(-1).settings.nested.array[0] = 999; all.at(-1).levels[0] = -500;
  app.respond(0, payload(sources, target)); await promise;
  assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent); assert.equal(app.dom.delta.hidden, false);
  assert.match(app.dom.summary.textContent, /\+5 dB @ 200/); assert.match(app.dom.summary.textContent, /-2 dB @ 500/);
  assert.match(app.dom.summary.textContent, /Все 7 точек/); assert.match(app.dom.status.textContent, /read-only/);
  assert.equal(app.dom.reference.textContent, pinnedText); assert.equal(app.timers.size, 0);
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 23, 'all three traces plus zero line');
  const frozen = app.dom.summary.textContent;
  const next = sweep('new-target'); app.update(Array.from({ length: 40 }, () => next));
  assert.equal(app.requests.length, 1); assert.equal(app.dom.summary.textContent, frozen); assert.equal(app.dom.reference.textContent, pinnedText);
  const again = app.click('run'); app.respond(1, payload(sources, next)); await again;
  assert.match(app.dom.summary.textContent, /new-target/);
  app.click('clear'); assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.run.disabled, true);
  assert.equal(app.dom.overlay.hidden, true); assert.equal(app.dom.delta.hidden, true);
}
async function pinBoundaries() {
  for (const n of [2, 3, 16]) {
    const app = setup(), all = group(1, n);
    // Sequence is metadata, not a compatibility parameter; use representable UTC fixture values.
    all.forEach((s, i) => { s.interval = copy(group()[i % 4].interval); });
    app.pin(all, n); assert.equal(app.dom.run.disabled, false, app.dom.status.textContent);
    const p = app.click('run'); app.respond(0, payload(all.slice(0, -1), all.at(-1))); await p;
    assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  }
  for (const value of ['', ' ', '1', '17', '2.5', 'NaN', 'Infinity']) {
    const app = setup(); app.update(group()); app.dom.count.value = value; app.dom.count.listeners.input(); app.click('pin');
    assert.equal(app.requests.length, 0); assert.equal(app.dom.run.disabled, true); assert.match(app.dom.status.textContent, /N:/);
  }
  for (const available of [0, 1, 2, 3]) {
    const app = setup(); app.pin(group().slice(0, available));
    assert.equal(app.requests.length, 0); assert.equal(app.dom.pin.disabled, true); assert.equal(app.dom.run.disabled, true);
    assert.match(app.dom.status.textContent, /Недостаточно/); assert.equal(app.dom.count.value, '3');
  }
  for (const mutate of [a => a[0].id = a[1].id, a => a.at(-1).id = a[0].id, a => a[0].id = '',
    a => a[0].id = ' ', a => a[0].id = 'a'.repeat(129), a => a[0].id = '\ud800', a => a[0].id = '\udc00',
    a => a[0].settings = {}, a => a[0].settings = { sequence: 1 }, a => a[0].settings.active = 1,
    a => a[0].settings.nested.array = [null], a => a[0].frequencies_hz[0] = 101,
    a => a[0].levels[0] = Infinity, a => a[0].interval.started_at = '2026-02-30T00:00:00Z']) {
    const app = setup(), all = group(); mutate(all); app.pin(all);
    assert.equal(app.dom.run.disabled, true, String(mutate)); assert.equal(app.requests.length, 0);
  }
  const app = setup(), all = group(); all[0].id = '😀'.repeat(128); app.pin(all);
  assert.equal(app.dom.run.disabled, false, app.dom.status.textContent); let p = app.click('run');
  app.respond(0, payload(all.slice(0, -1), all.at(-1))); await p; assert.equal(app.dom.overlay.hidden, false);
  // A target in the frozen source list cannot be used, even when the received window rolls backwards.
  app.update([all[0]]); assert.equal(app.dom.run.disabled, true); await app.click('run'); assert.equal(app.requests.length, 1);
  assert.match(app.dom.status.textContent, /Цель входит/);
  const big = setup(), many = group(1, 16); many.forEach((s, i) => { s.id = `${i}`.padStart(2, '0') + '😀'.repeat(126); s.interval = copy(group()[i % 4].interval); });
  big.pin(many, 16); await big.click('run'); assert.equal(big.requests.length, 0); assert.match(big.dom.status.textContent, /8192 байт/);
  const exact = setup(), boundary = group(1, 16);
  boundary.forEach((s, i) => { s.id = `${i}`; s.interval = copy(group()[i % 4].interval); });
  const bodyBytes = () => Buffer.byteLength(JSON.stringify({ reference_ids: boundary.slice(0, -1).map(s => s.id), target_id: boundary.at(-1).id, minimum_increase_db: 6, minimum_bins: 2 }));
  for (const s of boundary) while (s.id.length < 128 && bodyBytes() + 6 <= 8192) s.id += '\u0001';
  for (const s of boundary) while (s.id.length < 128 && bodyBytes() < 8192) s.id += 'x';
  assert.equal(bodyBytes(), 8192); exact.pin(boundary, 16); p = exact.click('run');
  assert.equal(Buffer.byteLength(exact.requests[0].options.body), 8192);
  exact.respond(0, payload(boundary.slice(0, -1), boundary.at(-1))); await p;
  assert.equal(exact.dom.overlay.hidden, false, exact.dom.status.textContent);

}
async function exactUnicodeIdentities() {
  // Python str.strip() treats these 29 code points as whitespace; FEFF and
  // ZERO WIDTH SPACE are exact ID characters, not candidates for normalization.
  const whitespace = [0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20,
    0x85, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
    0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000];
  assert.equal(whitespace.length, 29);
  for (const character of ['\ufeff', '\u200b']) {
    const app = setup(), all = group();
    all.forEach((source, index) => { source.id = [character, ` ${character}`, `${character} `, `\t${character}`][index]; });
    app.pin(all); assert.equal(app.dom.run.disabled, false, JSON.stringify(character) + app.dom.status.textContent);
    const pending = app.click('run'); assert.equal(app.requests.length, 1);
    assert.deepEqual(JSON.parse(app.requests[0].options.body), {
      reference_ids: all.slice(0, -1).map(source => source.id), target_id: all.at(-1).id,
      minimum_increase_db: 6, minimum_bins: 2,
    });
    app.respond(0, payload(all.slice(0, -1), all.at(-1))); await pending;
    assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
    assert(app.dom.summary.textContent.includes(all.at(-1).id), 'preserve exact target ID');
  }
  for (const blank of [...whitespace.map(codepoint => String.fromCodePoint(codepoint)),
    String.fromCodePoint(...whitespace)]) {
    for (const position of [0, 3]) {
      const app = setup(), all = group(); all[position].id = blank;
      app.pin(all); await app.click('run');
      assert.equal(app.requests.length, 0, `Reject whitespace-only ID ${JSON.stringify(blank)} at ${position}`);
      assert.equal(app.dom.overlay.hidden, true);
    }
  }
}
async function strictWireJSON() {
  const all = group(9), deltas = [0, 6, 9, 9, 1, 7, 8, 0, 6];
  all.forEach(source => { source.frequencies_hz[2] = source.frequencies_hz[1]; });
  all.at(-1).levels = deltas.map(delta => -60 + delta);
  const raw = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1,
    data: detection(payload(all.slice(0, -1), all.at(-1))) });
  const mutations = [
    text => text.replace('"contract_version":1', '"contract_version":1.0'),
    text => text.replace('"contract_version":1', '"contract_version":1e0'),
    text => text.replace('"points":9', '"points":9.0'),
    text => text.replace('"count":3', '"count":3.0'),
    text => text.replace('"count":2', '"count":2e0'),
    text => text.replace('"minimum_bins":2', '"minimum_bins":2.0'),
    text => text.replace('"minimum_bins":2', '"minimum_bins":2e0'),
    text => text.replace('"frequencies_hz":[100', '"frequencies_hz":[100.0'),
    text => text.replace('"frequencies_hz":[100', '"frequencies_hz":[1e2'),
    text => text.replace('"max_increase":{"frequency_hz":200', '"max_increase":{"frequency_hz":200.0'),
    text => text.replace('"min_change":{"frequency_hz":100', '"min_change":{"frequency_hz":1e2'),
    text => text.replace('"minimum_bins":2', '"minimum_bins":99,"minimum_bins":2'),
    text => text.replace('"minimum_bins":2', '"minimum\\u005fbins":99,"minimum_bins":2'),
    text => text.replace('"contract_version":1', '"contract_version":99,"contract_version":1'),
    text => text.replace('"count":3', '"count":99,"count":3'),
    text => text.replace('"start_index":1', '"start_index":99,"start_index":1'),
    text => text.replace('"rbw_hz":1000', '"rbw_hz":999,"rbw_hz":1000'),
    text => text.replace('"rbw_hz":1000', '"\\u0072bw_hz":999,"rbw_hz":1000'),
    text => text.replace('"array":[1,"known"]', '"array":[],"array":[1,"known"]'),
  ];
  for (const key of ['start_index', 'end_index', 'peak_index', 'start_frequency_hz',
    'end_frequency_hz', 'peak_frequency_hz', 'bin_count']) {
    mutations.push(text => text.replace(new RegExp(`("${key}":)(-?\\d+)`), '$1$2.0'));
  }
  for (const mutate of mutations) {
    const changed = mutate(raw); assert.notEqual(changed, raw, String(mutate));
    const app = setup(); app.pin(all); const pending = app.click('run');
    app.requests[0].resolve(streamResponse([Buffer.from(changed)])); await pending;
    assert.equal(app.dom.overlay.hidden, true, String(mutate)); assert.equal(app.dom.delta.hidden, true);
    assert.match(app.dom.status.textContent, /Сравнение недоступно/, String(mutate));
  }
  // dB measurements and arbitrary numeric metadata retain their fractional/exponent
  // spellings, even when metadata keys happen to match schema integer-field names.
  const fractional = group(3), app = setup();
  fractional.forEach(source => {
    source.levels = [-60.25, -50.5, -40.75];
    Object.assign(source.settings, { minimum_bins: 2.5, count: 3.5, frequency_hz: 100.25,
      frequencies_hz: [1.25, 2.5], start_index: 1.25, contract_version: 1.5,
      data: { comparison: { points: 2.25 } },
      text: '"minimum_bins":2.0,{"duplicate":1,"duplicate":2},[1e0]' });
  });
  fractional.at(-1).levels = [-60, -43.75, -33.5]; app.pin(fractional);
  app.dom.threshold.value = '6.25'; app.dom.threshold.listeners.input();
  const pending = app.click('run');
  const accepted = JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1,
    data: detection(payload(fractional.slice(0, -1), fractional.at(-1)), 6.25, 2) })
    .replace('"minimum_increase_db":6.25', '"minimum_increase_db":6.25e0')
    .replaceAll('"minimum_bins":2.5', '"minimum_bins":2.5e0')
    .replace('"levels":[-60,', '"levels":[-6e1,');
  app.requests[0].resolve(streamResponse([Buffer.from(accepted)])); await pending;
  assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /областей: 1/);
}
async function corruptResponses() {
  const changes = [d => d.session_id = 'wrong', d => d.stream_id = 'wrong', d => d.points = true,
    d => d.points = 0, d => d.points = 16385, d => d.frequencies_hz.pop(), d => d.frequencies_hz[0] = true,
    d => d.frequencies_hz[0] = -1, d => d.frequencies_hz[0] = 100.5, d => d.frequencies_hz[0] = 9007199254740992,
    d => d.frequencies_hz[2] = 99, d => d.frequencies_hz[0] = 101,
    d => d.reference = null, d => d.reference.method = 'mean_dbm', d => d.reference.count = '3',
    d => d.reference.count = true, d => d.reference.count = 2, d => d.reference.source_ids.reverse(),
    d => d.reference.source_ids[0] = d.reference.source_ids[1], d => d.reference.sources.reverse(),
    d => d.reference.sources.pop(), d => d.reference.sources[0].id = 'wrong', d => d.reference.sources[0].settings.sequence = 99,
    d => d.reference.sources[0].settings.nested.array[0] = true, d => d.reference.sources[0].settings.extra = 1,
    d => d.reference.sources[0].settings.rbw_hz = null, d => d.reference.sources[0].settings = {},
    d => d.reference.sources[0].interval.started_at = '2026-11-08T12:30:00.000002+00:00',
    d => d.reference.sources[0].interval.ended_at = '2026-11-08T12:30:01.000002+00:00',
    d => d.reference.sources[0].interval.extra = true,
    d => d.reference.interval.started_at = '2026-11-08T12:30:00.000002+00:00',
    d => d.reference.interval.ended_at = '2026-11-08T12:30:01.000004+00:00',
    d => d.reference.interval.ended_at = '2026-11-08T12:30:01.0000031+00:00',
    d => d.reference.interval = null, d => d.reference.level_unit = 'dB', d => d.reference.levels.pop(),
    d => d.reference.levels[0] = Infinity, d => { d.reference.levels[0] = -61; d.delta_levels[0] = 1; summarize(d); },
    d => { d.reference.levels[0] = -59; d.delta_levels[0] = -1; summarize(d); },
    d => d.target.id = 'wrong', d => d.target.level_unit = 's_meter_count', d => d.target.levels.pop(),
    d => d.target.levels[0] = -99, d => d.target.settings.sequence = 100,
    d => d.target.settings.nested.array[0] = true, d => d.target.settings.rbw_hz = Infinity,
    d => d.target.interval.started_at = '2026-11-08T12:30:00.000005+00:00',
    d => d.target.interval.started_at = '2026-11-08T13:30:00.000004+01:00',
    d => d.target.interval.started_at = '2026-11-08T12:30:02.000004+00:00',
    d => d.target.interval.started_at = '2026-11-08T12:30:00.0000041+00:00',
    d => d.delta_unit = 'dBm', d => d.delta_levels.pop(), d => d.delta_levels[0] = Infinity,
    d => d.delta_levels[0] = 1, d => d.max_increase.delta_db = 1, d => d.min_change.delta_db = -1,
    d => d.max_increase.frequency_hz = 200, d => d.min_change.frequency_hz = 200,
    d => d.max_increase = null];
  for (const change of changes) {
    const app = setup(); app.pin(); const p = app.click('run'), d = payload(); change(d); app.respond(0, d); await p;
    assert.equal(app.dom.overlay.hidden, true, String(change)); assert.equal(app.dom.delta.hidden, true, String(change));
    assert.match(app.dom.status.textContent, /Сравнение недоступно/, String(change));
  }
  const appBadDetection = setup(); appBadDetection.pin(); const pending = appBadDetection.click('run');
  const wrongDetection = detection(payload()); wrongDetection.detection.regions.push({
    start_index: 0, end_index: 1, start_frequency_hz: 100, end_frequency_hz: 200,
    peak_index: 0, peak_frequency_hz: 100, peak_increase_db: 0, bin_count: 2,
  });
  wrongDetection.detection.count++;
  appBadDetection.respond(0, wrongDetection); await pending;
  assert.equal(appBadDetection.dom.overlay.hidden, true); assert.match(appBadDetection.dom.status.textContent, /Области обнаружения/);
  // Known setting values are type-aware, but dictionary order and sequence differences between sources are allowed.
  const app = setup(), all = group(); all[0].settings.sequence = null; all[1].settings.sequence = { unknown: [null] };
  app.pin(all); const p = app.click('run'), d = payload(all.slice(0, -1), all.at(-1));
  d.target.settings = Object.fromEntries(Object.entries(d.target.settings).reverse());
  d.reference.sources[0].interval.started_at = '2026-11-08T12:30:00.000001Z';
  app.respond(0, d); await p; assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
}
async function staleAndLifecycle() {
  for (const change of ['clear', 'count', 'threshold', 'minimumBins', 'scope', 'stream', 'server', 'offline', 'connection', 'pagehide', 'timeout']) {
    for (const outcome of ['success', 'error']) {
      const app = setup(); app.pin(); const p = app.click('run');
      if (change === 'clear') app.click('clear');
      if (change === 'count') { app.dom.count.value = '2'; app.dom.count.listeners.input(); }
      if (['threshold', 'minimumBins'].includes(change)) { app.dom[change].value = '3'; app.dom[change].listeners.input(); }
      if (change === 'scope') app.update(group(), 'new-session');
      if (change === 'stream') app.update(group(), 'session/1', 'new-stream');
      if (change === 'server') { app.window.location.origin = 'http://new-server'; app.update(group()); }
      if (change === 'offline') app.events.offline();
      if (change === 'connection') app.window.OpenRadioSweepAverage.setConnected(false);
      if (change === 'pagehide') app.events.pagehide();
      if (change === 'timeout') { const timer = [...app.timers.values()][0]; assert.equal(timer.ms, 8000); timer.fn(); }
      assert.equal(app.requests[0].options.signal.aborted, true, change);
      const before = app.dom.status.textContent;
      if (outcome === 'success') app.respond(0, payload()); else app.requests[0].reject(new Error('late failure'));
      await p; assert.equal(app.dom.status.textContent, before); assert.equal(app.dom.overlay.hidden, true);
      if (!['timeout', 'threshold', 'minimumBins'].includes(change)) assert.match(app.dom.reference.textContent, /не закреплён/);
      else assert.match(app.dom.reference.textContent, /N=3/);
      if (['offline', 'connection'].includes(change)) app.window.OpenRadioSweepAverage.setConnected(true);
      if (change === 'pagehide') app.events.pageshow();
      assert.equal(app.requests.length, 1, 'no automatic retries or analysis');
    }
  }
  const app = setup(); app.pin(); const first = app.click('run'); app.click('clear'); app.pin(); const second = app.click('run');
  app.respond(1, payload()); await second; const summary = app.dom.summary.textContent, status = app.dom.status.textContent;
  app.requests[0].reject(new Error('late previous error')); await first;
  assert.equal(app.dom.summary.textContent, summary); assert.equal(app.dom.status.textContent, status);
  app.dom.count.value = '2'; app.dom.count.listeners.input(); assert.equal(app.dom.summary.textContent, '');
}
async function detectionRegionsAndEdits() {
  const all = group(9), deltas = [0, 6, 9, 9, 1, 7, 8, 0, 6];
  all.forEach(s => { s.frequencies_hz[2] = s.frequencies_hz[1]; });
  all.at(-1).levels = deltas.map(v => -60 + v);
  const valid = detection(payload(all.slice(0, -1), all.at(-1)));
  assert.deepEqual(valid.detection.regions.map(r => [r.start_index, r.end_index, r.peak_index]), [[1, 3, 2], [5, 6, 6]]);
  for (const change of [
    d => { d.detection.regions = d.detection.regions.slice(1); d.detection.count = 1; },
    d => { d.detection.regions = []; d.detection.count = 0; },
    d => d.detection.regions.reverse(),
    d => { const r = d.detection.regions[0]; r.start_index = 2; r.bin_count = 2; },
    d => { const r = d.detection.regions[0]; r.peak_index = 3; r.peak_frequency_hz = 400; },
    d => d.detection.regions[0].start_index = true,
    d => d.detection.regions[0].peak_increase_db = '9',
    d => d.detection.minimum_bins = true,
    d => d.detection.minimum_increase_db = '6',
    d => d.detection.count = '2',
    d => d.detection.extra = true,
    d => d.extra = true,
  ]) {
    const app = setup(); app.pin(all); const pending = app.click('run'), result = copy(valid); change(result);
    app.respond(0, result); await pending;
    assert.equal(app.dom.overlay.hidden, true, String(change)); assert.equal(app.dom.delta.hidden, true);
    assert.match(app.dom.status.textContent, /Сравнение недоступно/);
  }
  const app = setup(); app.pin(all); const frozen = app.dom.reference.textContent;
  let pending = app.click('run'); app.respond(0, valid); await pending;
  assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /областей: 2/);
  assert.equal(app.drawing.filter(row => row[0] === 'fillRect').length, 4, 'two plots and two region highlights');
  app.dom.threshold.value = '9'; app.dom.threshold.listeners.input();
  app.dom.minimumBins.value = '2'; app.dom.minimumBins.listeners.input();
  assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.overlay.hidden, true);
  assert.equal(app.dom.reference.textContent, frozen); assert.equal(app.requests.length, 1);
  pending = app.click('run'); app.click('run'); assert.equal(app.requests.length, 2);
  assert.deepEqual(JSON.parse(app.requests[1].options.body), {
    reference_ids: all.slice(0, -1).map(s => s.id), target_id: all.at(-1).id, minimum_increase_db: 9, minimum_bins: 2,
  });
  app.respond(1, detection(valid.comparison, 9, 2)); await pending;
  assert.match(app.dom.summary.textContent, /областей: 1/); assert.equal(app.dom.reference.textContent, frozen);
  for (const [name, value] of [['threshold', ''], ['threshold', '-1'], ['threshold', '121'], ['threshold', 'Infinity'],
    ['minimumBins', ''], ['minimumBins', '0'], ['minimumBins', '16385'], ['minimumBins', '1.5']]) {
    const invalid = setup(); invalid.pin(all); invalid.dom[name].value = value; invalid.dom[name].listeners.input();
    await invalid.click('run'); assert.equal(invalid.requests.length, 0); assert.match(invalid.dom.reference.textContent, /N=3/);
  }
  for (const envelope of [{ extra: true }, { error: {} }, { contract_version: true }]) {
    const invalid = setup(); invalid.pin(); const pending = invalid.click('run');
    invalid.respond(0, payload(), 200, envelope); await pending;
    assert.equal(invalid.dom.overlay.hidden, true); assert.match(invalid.dom.status.textContent, /контракт/);
  }
}
async function bodiesAndStatuses() {
  for (const status of [201, 204, 206, 400, 404, 409, 413, 500]) {
    const app = setup(); app.pin(); const p = app.click('run'); app.respond(0, payload(), status); await p;
    assert.equal(app.dom.overlay.hidden, true); assert.match(app.dom.status.textContent, /Сравнение недоступно/); assert.equal(app.dom.run.disabled, false);
  }
  for (const status of [200, 201, 400, 404, 500]) for (const length of ['2000001', null, '1']) {
    const app = setup(); app.pin(); const p = app.click('run');
    const response = streamResponse([Buffer.alloc(1000000, ' '), Buffer.alloc(1000001, ' ')], length, status);
    app.requests[0].resolve(response); await p;
    assert.equal(app.dom.overlay.hidden, true); assert.match(app.dom.status.textContent, /2 000 000 байт/);
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(response.state.cancelled, 1);
    assert.equal(response.state.reads, length === '2000001' ? 0 : 2); assert.equal(response.state.released, 1);
  }
  const app = setup(); app.pin(); let p = app.click('run');
  const json = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: detection(payload()) }));
  app.requests[0].resolve(streamResponse([Buffer.alloc(2000000 - json.length, ' '), json], '2000000')); await p;
  assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  for (const value of ['{', '\ufeff{}', Buffer.from([0xff])]) {
    p = app.click('run'); app.requests.at(-1).resolve(streamResponse([Buffer.from(value)])); await p; assert.equal(app.dom.overlay.hidden, true);
  }
  for (const envelope of [{ contract: 'wrong' }, { contract_version: true }, { contract_version: 2 }]) {
    p = app.click('run'); app.respond(app.requests.length - 1, payload(), 200, envelope); await p; assert.equal(app.dom.overlay.hidden, true);
  }
  p = app.click('run'); app.requests.at(-1).reject(new Error('network failed')); await p; assert.match(app.dom.status.textContent, /network failed/);
  p = app.click('run'); app.requests.at(-1).resolve({ status: 200, body: null }); await p; assert.match(app.dom.status.textContent, /потоковый/);
  const late = setup(); late.pin(); const running = late.click('run'); let finish, cancelled = 0;
  late.requests[0].resolve({ status: 200, body: { getReader: () => ({
    read: () => new Promise(resolve => finish = resolve), cancel() { cancelled++; return Promise.resolve(); }, releaseLock() {},
  }) } });
  await new Promise(resolve => setImmediate(resolve)); late.click('clear'); assert.equal(cancelled, 1);
  const status = late.dom.status.textContent; finish({ done: false, value: Buffer.alloc(2000001) }); await running;
  assert.equal(late.dom.status.textContent, status); assert.equal(late.dom.overlay.hidden, true);
}
async function completeDrawing() {
  for (const count of [1, 16384]) {
    for (const repeated of [true, false]) {
      const app = setup(), all = group(count);
      all.forEach(s => { s.levels.fill(-1e300); if (repeated) s.frequencies_hz.fill(100); });
      all.at(-1).levels.fill(1e300);
      app.pin(all); const p = app.click('run'); app.respond(0, payload(all.slice(0, -1), all.at(-1))); await p;
      assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
      assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 3 * count + 2);
      assert.equal(app.drawing.filter(row => row[0] === 'arc').length, count === 1 || repeated ? 1 : 0);
      assert.equal(app.drawing.filter(row => row[0] === 'strokeRect').length, count === 1 || repeated ? 2 : 0);
      for (const row of app.drawing.filter(row => ['moveTo', 'lineTo', 'arc'].includes(row[0]))) assert(row.slice(1).every(Number.isFinite));
    }
  }
  for (const count of [1, 7]) {
    const app = setup(), all = group(count); all.forEach(s => s.frequencies_hz.fill(100)); app.pin(all);
    const p = app.click('run'); app.respond(0, payload(all.slice(0, -1), all.at(-1))); await p;
    const circle = app.drawing.find(row => row[0] === 'arc'), square = app.drawing.find(row => row[0] === 'strokeRect');
    assert(circle && square, 'both coincident overlay traces must have visible markers');
    assert.equal(circle[1], square[1] + 5); assert.equal(circle[2], square[2] + 5);
    assert(circle[3] < square[3] / 2, 'smaller target disc does not hide reference square');
  }
  // Core clamps one-ULP roundoff to the exact physical source range; accept the corrected value.
  {
    const app = setup(), all = group(1, 10);
    all.forEach((s, i) => { s.interval = copy(group()[i % 4].interval); s.levels = [-2.591576022265827]; });
    all[9].levels[0] = -2.5915760222658264; app.pin(all, 10);
    const p = app.click('run'), d = payload(all.slice(0, -1), all.at(-1));
    app.respond(0, d); await p; assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  }
  const app = setup(), all = group(); all.at(-1).levels.fill(-65); app.pin(all);
  const p = app.click('run'); app.respond(0, payload(all.slice(0, -1), all.at(-1))); await p;
  assert.match(app.dom.summary.textContent, /Максимальное изменение: -5 dB/);
}
async function platformIntegration() {
  const app = setup(); app.pin(); const pending = app.click('run'), c = app.context;
  c.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: group(), refreshRunning: false };
  c.dom = { sessionSelect: { value: 'different' }, spectrumStream: { value: 'other' }, health: {} };
  c.storeSession = () => {}; c.closeHandoff = () => {}; c.refresh = () => assert(app.requests[0].options.signal.aborted);
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) {
    const start = platformCode.indexOf(`  function ${name}(`), end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0); vm.runInContext(platformCode.slice(start, end), c);
  }
  c.changeSession(); app.respond(0, payload()); await pending; assert.equal(app.dom.overlay.hidden, true);
  c.changeSpectrumStream(); assert.equal(app.requests.length, 1);
  const start = platformCode.indexOf('  async function refresh('), end = platformCode.indexOf('\n  }', start) + 4;
  c.renderSession = () => {}; c.renderAll = () => {}; c.reportError = () => {}; c.scheduleRefresh = () => {};
  c.performRefresh = async () => { throw new Error('disconnected'); };
  vm.runInContext(platformCode.slice(start, end), c);
  app.pin(); const second = app.click('run'); await c.refresh(); assert.equal(app.requests[1].options.signal.aborted, true);
  app.respond(1, payload()); await second; assert.equal(app.dom.run.disabled, true);
  c.performRefresh = async () => {}; await c.refresh(); assert.equal(app.dom.run.disabled, true, 'recovery requires re-pinning');
  assert.equal(app.requests.length, 2);
  // SSE transport loss also invalidates the selection; normal refresh can only restore controls.
  const handlers = {}; c.API_ROOT = '/api/v1'; c.encode = encodeURIComponent; c.EVENT_TYPES = [];
  c.setSseStatus = () => {}; c.EventSource = function () { this.addEventListener = (name, fn) => handlers[name] = fn; this.close = () => {}; };
  c.window.EventSource = c.EventSource;
  const eventStart = platformCode.indexOf('  function connectEvents('), eventEnd = platformCode.indexOf('\n  }', eventStart) + 4;
  vm.runInContext(platformCode.slice(eventStart, eventEnd), c); c.connectEvents();
  app.pin(); const third = app.click('run'); handlers.error(); assert.equal(app.requests[2].options.signal.aborted, true);
  app.respond(2, payload()); await third; assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.run.disabled, true);
  await c.refresh(); assert.equal(app.dom.run.disabled, true); assert.equal(app.requests.length, 3);
}
async function httpSmoke(base) {
  const sourceResponse = await fetch(`${base}/api/v1/sessions/average-one/sweeps?stream_id=spectrum-one&limit=40`);
  assert.equal(sourceResponse.status, 200); const sources = (await sourceResponse.json()).data;
  const app = setup(), calls = []; app.window.location.origin = base;
  app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
  app.update(sources, 'average-one', 'spectrum-one'); app.click('pin'); assert.equal(calls.length, 0);
  await app.click('run'); assert.equal(calls.length, 1); assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /one-sweep-4/); assert.match(app.dom.summary.textContent, /\+20 dB @ 103000000/);
  assert.match(app.dom.summary.textContent, /-10 dB @ 107000000/);
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 305);
  const frozen = app.dom.summary.textContent; app.update([], 'average-one', 'spectrum-one'); assert.equal(app.dom.summary.textContent, frozen);
  app.update(sources, 'average-one', 'spectrum-one'); await app.click('run'); assert.equal(calls.length, 2);
  app.dom.count.value = '2'; app.dom.count.listeners.input(); assert.equal(app.dom.overlay.hidden, true);
  app.click('pin'); await app.click('run'); assert.equal(calls.length, 3); assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  app.update([], 'average-two', 'spectrum-two'); assert.equal(app.dom.run.disabled, true); assert.equal(app.dom.summary.textContent, '');
  console.log('Web HTTP average smoke passed: local pin, 101 bins, +20/-10 dB extrema, repeated compare, N edit, scope invalidation');
}
async function detectionHttpSmoke(base) {
  const sourceResponse = await fetch(`${base}/api/v1/sessions/changes-one/sweeps?stream_id=spectrum-one&limit=40`);
  assert.equal(sourceResponse.status, 200); const sources = (await sourceResponse.json()).data;
  const app = setup(), calls = []; app.window.location.origin = base;
  app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
  app.update(sources, 'changes-one', 'spectrum-one'); app.click('pin'); const frozen = app.dom.reference.textContent;
  assert.equal(calls.length, 0); await app.click('run');
  assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /областей: 2/);
  assert.match(app.dom.summary.textContent, /103000000–103300000 Гц · пик \+12 dB @ 103100000 Гц · 4 точек/);
  assert.match(app.dom.summary.textContent, /106900000–107000000 Гц · пик \+9 dB @ 107000000 Гц · 2 точек/);
  assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, 305);
  app.dom.threshold.value = '12'; app.dom.threshold.listeners.input();
  assert.equal(app.dom.overlay.hidden, true); assert.equal(app.dom.reference.textContent, frozen); assert.equal(calls.length, 1);
  await app.click('run'); assert.equal(calls.length, 2); assert.equal(app.dom.overlay.hidden, false, app.dom.status.textContent);
  assert.match(app.dom.summary.textContent, /областей: 1/);
  assert.match(app.dom.summary.textContent, /103100000–103100000 Гц · пик \+12 dB @ 103100000 Гц · 2 точек/);
  app.dom.minimumBins.value = '1'; app.dom.minimumBins.listeners.input(); await app.click('run');
  assert.match(app.dom.summary.textContent, /областей: 2/); assert.equal(app.dom.reference.textContent, frozen);
  app.update([], 'changes-two', 'spectrum-two'); assert.equal(app.dom.run.disabled, true); assert.equal(app.dom.summary.textContent, '');
  console.log('Web live HTTP detection passed: all 101 bins, two maximal regions, duplicate-bin first peak, parameter edits, preserved sources');
}
(async () => {
  await happyAndFrozen(); await pinBoundaries(); await exactUnicodeIdentities(); await strictWireJSON();
  await corruptResponses(); await staleAndLifecycle(); await bodiesAndStatuses();
  await detectionRegionsAndEdits(); await completeDrawing(); await platformIntegration();
  if (process.argv[4]) await httpSmoke(process.argv[4]);
  if (process.argv[5]) await detectionHttpSmoke(process.argv[5]);
  console.log('average behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
