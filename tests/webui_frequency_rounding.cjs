/* Real production handlers, synthetic inputs and adversarial transport timing. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const path = require('node:path');
const code = fs.readFileSync(process.argv[2], 'utf8'), platformCode = fs.readFileSync(process.argv[3], 'utf8');
const example = JSON.parse(fs.readFileSync(path.join(__dirname, '../docs/examples/frequency-rounding-profile.json'), 'utf8'));
const copy = value => structuredClone(value);
function candidate(id = 'candidate/?&', frequency = 103025123, stream = 'tiny/1', unit = 'dBm', session = 'session/1') {
  return { id, session_id: session, source_stream_id: stream, center_frequency_hz: frequency, level_unit: unit,
    interval: { started_at: '2026-11-08T12:00:00.000001Z', ended_at: '2026-11-08T12:00:01.000002Z' },
    state: 'new', score: 10, peak_level: -60, bandwidth_hz: 10000, observation_ids: ['unrelated'] };
}
function profile(rule) { return { ...copy(example), rules: rule ? [rule] : copy(example.rules) }; }
function rule(start = 0, end = 100, step = 10, origin = 0, tolerance = 10) {
  return { start_frequency_hz: start, end_frequency_hz: end, step_hz: step, origin_frequency_hz: origin, max_offset_hz: tolerance };
}
function payload(source = candidate(), artifact = profile(), session = source.session_id || 'session/1') {
  const provenance = Object.fromEntries(['id', 'source_stream_id', 'interval', 'center_frequency_hz', 'level_unit'].map(key => [key, copy(source[key])]));
  const canonical = { ...copy(artifact), name: artifact.name.normalize('NFC').trim() }, frequency = source.center_frequency_hz;
  const index = canonical.rules.findIndex(r => r.start_frequency_hz <= frequency && frequency <= r.end_frequency_hz);
  let result = { status: 'outside_ranges', rule_index: null, nearest_frequency_hz: null, offset_hz: null, rounded_frequency_hz: null };
  if (index >= 0) {
    const r = canonical.rules[index], f = BigInt(frequency), step = BigInt(r.step_hz), origin = BigInt(r.origin_frequency_hz);
    const mod = n => (n % step + step) % step, first = BigInt(r.start_frequency_hz) + mod(origin - BigInt(r.start_frequency_hz));
    const last = BigInt(r.end_frequency_hz) - mod(BigInt(r.end_frequency_hz) - origin), lower = f - mod(f - origin);
    const clamp = n => n < first ? first : n > last ? last : n, a = clamp(lower), b = clamp(lower + step);
    const abs = n => n < 0n ? -n : n, nearest = abs(f - a) <= abs(f - b) ? a : b, offset = nearest - f;
    const accepted = abs(offset) <= BigInt(r.max_offset_hz), status = accepted ? offset ? 'rounded' : 'unchanged' : 'outside_tolerance';
    result = { status, rule_index: index, nearest_frequency_hz: Number(nearest), offset_hz: Number(offset), rounded_frequency_hz: accepted ? Number(nearest) : null };
  }
  return { session_id: session, candidate: provenance, profile: canonical, frequency_unit: 'Hz', method: 'nearest_grid', tie_break: 'lower', result };
}
function streamResponse(chunks, length = null, status = 200) {
  const state = { reads: 0, cancelled: 0, released: 0 };
  return { status, state, headers: { get: () => length }, body: { cancel() { state.cancelled++; return Promise.resolve(); }, getReader: () => ({
    async read() { const value = chunks[state.reads++]; return value === undefined ? { done: true } : { done: false, value }; },
    cancel() { state.cancelled++; return Promise.resolve(); }, releaseLock() { state.released++; },
  }) } };
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map(), downloads = [], blobs = [], revoked = [];
  let timerId = 0, appendHook = () => {}, blobHook = () => {};
  function element() {
    const e = { value: '', textContent: '', hidden: false, disabled: false, listeners: {}, children: [],
      addEventListener(name, fn) { this.listeners[name] = fn; }, replaceChildren(...items) { this.children = items; },
      setAttribute(name, value) { this[name] = value; }, remove() {}, click() { downloads.push(this); } };
    Object.defineProperty(e, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } }); return e;
  }
  for (const name of ['candidate', 'run', 'clear', 'editor', 'file', 'template', 'save', 'draft', 'source', 'status', 'summary']) dom[name] = element();
  dom.editor.value = JSON.stringify(profile());
  const window = { location: { origin: 'http://server-a' }, addEventListener(name, fn) { events[name] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; }, clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById: id => dom[id.replace('rounding-', '')], createElement: element,
    body: { appendChild() { appendHook(); } } }, AbortController, TextEncoder, TextDecoder, Blob,
    URL: { createObjectURL(blob) { blobs.push(blob); blobHook(); return `blob:${blobs.length}`; }, revokeObjectURL(url) { revoked.push(url); } },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  const api = window.OpenRadioFrequencyRounding;
  const update = (all = [candidate()], sessionId = 'session/1', fresh = true, streamId = 'tiny/1') => api.update({ candidates: all, sessionId, fresh, streamId });
  const choose = id => { dom.candidate.value = id; dom.candidate.listeners.change(); };
  const click = name => dom[name].listeners.click();
  const edit = text => { dom.editor.value = text; dom.editor.listeners.input(); };
  function respond(index, data = payload(), status = 200, envelope = {}) {
    const body = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope }));
    requests[index].resolve(streamResponse([body], String(body.length), status));
  }
  const select = (source = candidate(), p = profile(), session = source.session_id || 'session/1') => {
    update([source], session); choose(source.id); edit(JSON.stringify(p));
  };
  return { dom, events, requests, timers, downloads, blobs, revoked, window, context, api, update, choose, click, edit, respond, select,
    appendHook(fn) { appendHook = fn; }, blobHook(fn) { blobHook = fn; } };
}
async function happyAndFrozen() {
  const app = setup(), source = candidate('<img onerror=alert(1)> /?&');
  const receiver = candidate('receiver-candidate', 145506250, 'other-stream', 's_meter_count');
  app.update([source, receiver, candidate('foreign', 12, 'foreign-stream', 'dBm', 'other-session')]);
  assert.equal(app.requests.length, 0); assert.equal(app.dom.run.disabled, true, 'candidate selection must be explicit');
  assert.equal(app.dom.candidate.children.length, 3, 'placeholder plus all current session sources');
  app.choose(source.id); const p = app.click('run'); app.click('run');
  assert.equal(app.requests.length, 1); assert.equal(app.dom.run.disabled, true);
  const request = app.requests[0], url = new URL(request.url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/candidate-frequency-rounding'); assert.equal(url.search, '');
  assert.deepEqual(JSON.parse(request.options.body), { candidate_id: source.id, profile_json: app.dom.editor.value });
  assert.equal(request.options.method, 'POST'); assert.equal(request.options.cache, 'no-store'); assert.equal(request.options.headers['Content-Type'], 'application/json');
  const changed = copy(source); changed.state = 'resolved'; changed.score = 0; changed.peak_level = -2; changed.observation_ids.push('later');
  app.update([changed, receiver]); app.respond(0, payload(changed)); await p;
  assert.equal(app.dom.save.disabled, false, app.dom.status.textContent); assert.equal(app.timers.size, 0);
  assert.match(app.dom.summary.textContent, /Исходная частота: 103025123 Гц/); assert.match(app.dom.summary.textContent, /− исходная\): -25123 Гц/);
  assert.match(app.dom.summary.textContent, /±30000 Гц/); assert.match(app.dom.summary.textContent, /\.000001Z/); assert(app.dom.summary.textContent.includes(source.id));
  assert.match(app.dom.status.textContent, /не является нормативом/);
  const frozen = app.dom.summary.textContent, provenance = app.dom.source.textContent;
  source.center_frequency_hz = 999; source.interval.started_at = 'invalid';
  app.update([receiver], 'session/1', true, 'other-spectrum');
  assert.equal(app.dom.summary.textContent, frozen); assert.equal(app.dom.source.textContent, provenance); assert.equal(app.dom.candidate.value, changed.id);
  assert.match(app.dom.candidate.children.at(-1).textContent, /сохранённый выбор/);
  const again = app.click('run'); app.respond(1, payload(changed)); await again; assert.equal(app.dom.save.disabled, false);
  app.click('clear'); assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.candidate.value, changed.id);
  app.choose(receiver.id); const third = app.click('run'); app.respond(2, payload(receiver)); await third;
  assert.match(app.dom.summary.textContent, /s_meter_count/); assert.match(app.dom.summary.textContent, /-6250 Гц/); assert.match(app.dom.summary.textContent, /правило 2/);
  app.click('save'); assert.equal(app.downloads.length, 1); assert.equal(app.downloads[0].download, 'frequency-rounding-profile.json');
  assert.deepEqual(JSON.parse(await app.blobs[0].text()), profile()); assert(!('candidate' in JSON.parse(await app.blobs[0].text())));
}
async function exactArithmetic() {
  const M = Number.MAX_SAFE_INTEGER;
  const cases = [
    [103025123, profile(), 103000000, -25123, 'rounded'], [103000000, profile(), 103000000, 0, 'unchanged'],
    [103050000, profile(), 103000000, -50000, 'outside_tolerance'], [120000000, profile(), null, null, 'outside_ranges'],
    [103075000, profile(), 103100000, 25000, 'rounded'], [145506250, profile(), 145500000, -6250, 'rounded'],
    [1, profile(rule(0, 1, 10, 0, 1)), 0, -1, 'rounded'], [7, profile(rule(7, 13, 10, 0, 3)), 10, 3, 'rounded'],
    [13, profile(rule(7, 13, 10, 0, 3)), 10, -3, 'rounded'], [7, profile(rule(0, 20, 10, 100, 10)), 10, 3, 'rounded'],
    [5, profile(rule(0, 20, 10, 100, 5)), 0, -5, 'rounded'], [5, profile(rule(0, 20, 10, 100, 4)), 0, -5, 'outside_tolerance'],
    [13, profile(rule(13, 13, 5, 3, 0)), 13, 0, 'unchanged'], [M, profile(rule(0, M, 2, 0, M)), M - 1, -1, 'rounded'],
    [M - 1, profile(rule(0, M, M, M, M)), M, 1, 'rounded'], [1, profile(rule(0, M, M, M, M)), 0, -1, 'rounded'],
    [M - 2, profile(rule(M - 10, M, 3, 0, 3)), M - 1, 1, 'rounded'],
    [9007199254740989, profile(rule(0, M, 2251799813685247, 0, M)), 9007199254740988, -1, 'rounded'],
  ];
  for (const [frequency, artifact, nearest, offset, status] of cases) {
    const app = setup(), source = candidate('exact', frequency); app.select(source, artifact);
    const data = payload(source, artifact); assert.equal(data.result.nearest_frequency_hz, nearest); assert.equal(data.result.offset_hz, offset); assert.equal(data.result.status, status);
    const pending = app.click('run'); app.respond(0, data); await pending;
    assert.equal(app.dom.save.disabled, false, `${frequency}: ${app.dom.status.textContent}`); assert(app.dom.summary.textContent.includes(status));
    for (const key of ['nearest_frequency_hz', 'offset_hz', 'rounded_frequency_hz']) if (data.result[key] !== null) {
      const p = app.click('run'), bad = copy(data); bad.result[key] += 1; app.respond(app.requests.length - 1, bad); await p;
      assert.equal(app.dom.save.disabled, true, `${frequency}/${key}`);
    }
  }
  const app = setup(), artifact = profile(); artifact.name = '  Cafe\u0301  ';
  app.select(candidate(), artifact); const p = app.click('run'); app.respond(0, payload(candidate(), artifact)); await p;
  assert.equal(app.dom.save.disabled, false); app.click('save'); assert.equal(JSON.parse(await app.blobs[0].text()).name, 'Café');
  const largest = profile(); largest.rules = Array.from({ length: 128 }, (_, i) => rule(i * 10, i * 10 + 9, 1, 0, 0));
  const maxApp = setup(), source = candidate('last-rule', 1279); maxApp.select(source, largest);
  const pending = maxApp.click('run'); maxApp.respond(0, payload(source, largest)); await pending;
  assert.equal(maxApp.dom.save.disabled, false, maxApp.dom.status.textContent); assert.match(maxApp.dom.summary.textContent, /правило 128/);
}
async function identityAndSource() {
  for (const field of ['id', 'source_stream_id', 'level_unit', 'session_id']) for (const exact of ['\ufeff', ' \ufeff\t']) {
    const app = setup(), source = candidate(); source[field] = exact;
    app.select(source, profile(), source.session_id); const pending = app.click('run');
    assert.equal(app.requests.length, 1, `Python accepts exact ${field}=${JSON.stringify(exact)}`);
    assert.equal(JSON.parse(app.requests[0].options.body).candidate_id, source.id);
    assert.equal(new URL(app.requests[0].url, app.window.location.origin).pathname,
      `/api/v1/sessions/${encodeURIComponent(source.session_id)}/candidate-frequency-rounding`);
    app.respond(0, payload(source)); await pending; assert.equal(app.dom.save.disabled, false, app.dom.status.textContent);
    assert(app.dom.summary.textContent.includes(exact), `Preserve exact ${field}, including FEFF`);
  }
  const pythonWhitespace = '\u0009\u000a\u000b\u000c\u000d\u001c\u001d\u001e\u001f\u0020\u0085\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000';
  for (const field of ['id', 'source_stream_id', 'level_unit', 'session_id']) for (const blank of ['', ...pythonWhitespace, pythonWhitespace]) {
    const app = setup(), source = candidate(); source[field] = blank;
    app.select(source, profile(), source.session_id); await app.click('run');
    assert.equal(app.requests.length, 0, `Reject whitespace-only ${field}=${JSON.stringify(blank)}`);
  }
  for (const id of ['😀'.repeat(128), 'a/?&#%+ ü', 'e\u0301']) {
    const app = setup(), source = candidate(id, 103025123, 'stream/' + 'x'.repeat(150), 'dBm', 'session/' + 'я'.repeat(150));
    app.select(source); const p = app.click('run'); assert.equal(app.requests.length, 1); app.respond(0, payload(source)); await p;
    assert.equal(app.dom.save.disabled, false, app.dom.status.textContent); assert.equal(JSON.parse(app.requests[0].options.body).candidate_id, id);
  }
  for (const id of ['', ' ', '\u0085', 'x'.repeat(129), '😀'.repeat(129), '\ud800']) {
    const app = setup(), source = candidate(id); app.select(source); await app.click('run'); assert.equal(app.requests.length, 0, JSON.stringify(id));
  }
  const mutations = [s => s.center_frequency_hz = 0, s => s.center_frequency_hz = -1, s => s.center_frequency_hz = true,
    s => s.center_frequency_hz = 1.5, s => s.center_frequency_hz = Number.MAX_SAFE_INTEGER + 1, s => s.source_stream_id = '',
    s => s.level_unit = '', s => s.interval.started_at = '2026-11-08T12:00:00.000001+03:00',
    s => s.interval.started_at = '2026-02-30T12:00:00Z', s => s.interval.started_at = '0000-01-01T00:00:00Z',
    s => s.interval.ended_at = '2026-11-08T12:00:00.000000Z', s => s.interval.extra = true];
  for (const mutate of mutations) { const app = setup(), source = candidate(); mutate(source); app.select(source); await app.click('run'); assert.equal(app.requests.length, 0, String(mutate)); }
  const app = setup(), source = candidate(); app.update([source, copy(source)]); app.choose(source.id); await app.click('run'); assert.equal(app.requests.length, 0);
  app.update([candidate('foreign', 103000000, 'x', 'dBm', 'foreign')]); app.choose('foreign'); await app.click('run'); assert.equal(app.requests.length, 0);
  // Equivalent UTC encodings preserve all six fractional digits, never Date milliseconds.
  app.select(source); const p = app.click('run'), equivalent = payload(); equivalent.candidate.interval.started_at = '2026-11-08T12:00:00.000001+00:00';
  app.respond(0, equivalent); await p; assert.equal(app.dom.save.disabled, false);
  // Identifier compatibility must not weaken the profile-name C* prohibition.
  const invalidName = profile(); invalidName.name = '\ufeff'; const names = setup(); names.select(candidate(), invalidName);
  const nameRequest = names.click('run'), nameData = payload(); nameData.profile = invalidName;
  names.respond(0, nameData); await nameRequest; assert.equal(names.dom.save.disabled, true);
}
async function corruptResponses() {
  const mutations = [d => d.extra = 1, d => d.session_id = 'foreign', d => d.frequency_unit = 'MHz', d => d.method = 'floor', d => d.tie_break = 'upper',
    d => d.candidate.id = 'wrong', d => d.candidate.source_stream_id = 'wrong', d => d.candidate.center_frequency_hz++,
    d => d.candidate.level_unit = 's_meter_count', d => d.candidate.state = 'new', d => d.candidate.interval.started_at = '2026-11-08T12:00:00.000002Z',
    d => d.candidate.interval.ended_at = '2026-11-08T12:00:01.000003Z', d => d.candidate.interval.ended_at = '2026-11-08T11:00:01.000002Z',
    d => d.profile = null, d => d.profile.extra = 1, d => d.profile.format = 'other', d => d.profile.format_version = true,
    d => d.profile.frequency_unit = 'MHz', d => d.profile.method = 'floor', d => d.profile.tie_break = 'upper', d => d.profile.name = 'wrong',
    d => d.profile.name = ' ' + d.profile.name, d => d.profile.name += '\n', d => d.profile.name = 'x'.repeat(81),
    d => d.profile.rules = [], d => d.profile.rules = Array(129).fill(d.profile.rules[0]), d => d.profile.rules[0].extra = 1,
    d => d.profile.rules[0].step_hz = 0, d => d.profile.rules[0].step_hz = true, d => d.profile.rules[0].step_hz = 1.5,
    d => d.profile.rules[0].start_frequency_hz = -1, d => d.profile.rules[0].end_frequency_hz = Number.MAX_SAFE_INTEGER + 1,
    d => d.profile.rules[0].origin_frequency_hz = '0', d => d.profile.rules[0].max_offset_hz++,
    d => d.profile.rules[1].start_frequency_hz = d.profile.rules[0].end_frequency_hz,
    d => d.profile.rules[0] = rule(1, 2, 10), d => d.result = null, d => d.result.extra = 1, d => delete d.result.offset_hz,
    d => d.result.rule_index = true, d => d.result.rule_index = 1, d => d.result.status = 'unchanged', d => d.result.nearest_frequency_hz = null,
    d => d.result.nearest_frequency_hz++, d => d.result.offset_hz++, d => d.result.rounded_frequency_hz = null,
    d => d.result.rounded_frequency_hz++, d => d.result.status = 'outside_tolerance'];
  for (const mutate of mutations) {
    const app = setup(); app.select(); const p = app.click('run'), data = payload(); mutate(data); app.respond(0, data); await p;
    assert.equal(app.dom.save.disabled, true, String(mutate)); assert.equal(app.dom.summary.textContent, ''); assert.match(app.dom.status.textContent, /Округление недоступно/);
  }
  for (const envelope of [{ contract: 'wrong' }, { contract_version: true }, { contract_version: 2 }, { data: null }, { extra: 1 }]) {
    const app = setup(); app.select(); const p = app.click('run'); app.respond(0, payload(), 200, envelope); await p; assert.equal(app.dom.save.disabled, true);
  }
  for (const field of ['rule_index', 'nearest_frequency_hz', 'offset_hz', 'rounded_frequency_hz']) {
    const source = candidate('outside', 120000000), app = setup(); app.select(source); const p = app.click('run'), d = payload(source); d.result[field] = 0;
    app.respond(0, d); await p; assert.equal(app.dom.save.disabled, true, field);
  }
}
async function rawDraftsAndBodies() {
  for (const raw of ['', ' ', 'я'.repeat(32769), '\ud800']) {
    const app = setup(); app.select(); app.edit(raw); await app.click('run'); assert.equal(app.requests.length, 0);
  }
  const rawProfile = JSON.stringify(profile());
  for (const raw of ['{', '\ufeff{}', rawProfile.replace('"name":', '"name":"earlier","name":'),
    rawProfile.replace('"name":', '"n\\u0061me":"earlier","name":'),
    rawProfile.replace('"step_hz":100000', '"step_hz":1,"step_hz":100000'),
    rawProfile.replace('"step_hz":100000', '"step_hz":100000.0'), rawProfile.replace('"step_hz":100000', '"step_hz":1e5')]) {
    const app = setup(); app.select(); app.edit(raw); const p = app.click('run'); assert.equal(JSON.parse(app.requests[0].options.body).profile_json, raw);
    app.respond(0, payload()); await p; assert.equal(app.dom.save.disabled, true, raw);
  }
  for (const status of [201, 204, 206, 400, 404, 409, 413, 500]) {
    const app = setup(); app.select(); const p = app.click('run'); app.requests[0].resolve(streamResponse([Buffer.from([0xff])], null, status)); await p;
    assert.equal(app.dom.save.disabled, true); assert.equal(app.dom.run.disabled, false); assert.match(app.dom.status.textContent, /Округление недоступно/);
    assert(!app.dom.status.textContent.includes('encoded'), 'error body is not decoded');
  }
  for (const status of [200, 201, 400, 404, 409, 413, 500]) for (const length of ['2000001', null, '1']) {
    const app = setup(); app.select(); const p = app.click('run'), response = streamResponse([Buffer.alloc(1000000, ' '), Buffer.alloc(1000001, ' ')], length, status);
    app.requests[0].resolve(response); await p; assert.equal(app.dom.save.disabled, true); assert.match(app.dom.status.textContent, /2 000 000 байт/);
    assert.equal(app.requests[0].options.signal.aborted, true); assert.equal(response.state.cancelled, 1); assert.equal(response.state.released, 1);
    assert.equal(response.state.reads, length === '2000001' ? 0 : 2);
  }
  const app = setup(); app.select(); const json = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data: payload() }));
  let p = app.click('run'); app.requests[0].resolve(streamResponse([Buffer.alloc(2000000 - json.length, ' '), json], '2000000')); await p; assert.equal(app.dom.save.disabled, false);
  for (const raw of ['{', Buffer.from([0xff]), '\ufeff' + json, json.toString().replace('"contract_version":1', '"contract_version":1,"contract_version":1'),
    json.toString().replace('"offset_hz":-25123', '"offset_hz":-25123.0'), json.toString().replace('"contract_version":1', '"contract_version":1e0'),
    json.toString().replace('"offset_hz":-25123', '"offset_hz":1e999')]) {
    p = app.click('run'); app.requests.at(-1).resolve(streamResponse([Buffer.from(raw)])); await p; assert.equal(app.dom.save.disabled, true);
  }
  p = app.click('run'); app.requests.at(-1).reject(new Error('offline')); await p; assert.match(app.dom.status.textContent, /offline/);
  p = app.click('run'); app.requests.at(-1).resolve({ status: 200, body: null }); await p; assert.match(app.dom.status.textContent, /потоковый/);
  // Exactly the 64 KiB raw profile boundary is accepted and sent unchanged.
  const valid = JSON.stringify(profile()), raw = valid + ' '.repeat(65536 - Buffer.byteLength(valid)); app.edit(raw);
  p = app.click('run'); app.respond(app.requests.length - 1); await p; assert.equal(app.dom.save.disabled, false);
}
async function staleAndReadiness() {
  for (const change of ['clear', 'input', 'template', 'file', 'candidate', 'session', 'server', 'offline', 'disconnect', 'pagehide', 'timeout']) for (const phase of ['fetch', 'body']) {
    const app = setup(); app.select(); const p = app.click('run'); let finishBody;
    if (phase === 'body') {
      app.requests[0].resolve({ status: 200, body: { getReader: () => ({ read: () => new Promise(resolve => { finishBody = resolve; }), cancel() { return Promise.resolve(); }, releaseLock() {} }) } });
      await new Promise(resolve => setImmediate(resolve));
    }
    if (change === 'clear') app.click('clear');
    if (change === 'input') app.edit('{');
    if (change === 'template') app.click('template');
    if (change === 'file') { app.dom.file.files = [localFile('{')]; await app.dom.file.listeners.change(); }
    if (change === 'candidate') { app.update([candidate(), candidate('next')]); app.choose('next'); }
    if (change === 'session') app.update([], 'other');
    if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update(); }
    if (change === 'offline' || change === 'pagehide') app.events[change]();
    if (change === 'disconnect') app.api.setConnected(false);
    if (change === 'timeout') { assert.equal([...app.timers.values()][0].ms, 8000); [...app.timers.values()][0].fn(); }
    assert.equal(app.requests[0].options.signal.aborted, true, `${change}/${phase}`); const status = app.dom.status.textContent;
    if (phase === 'fetch') app.respond(0); else finishBody({ done: false, value: Buffer.from('{}') });
    await p; assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.save.disabled, true); assert.equal(app.dom.status.textContent, status); assert.equal(app.timers.size, 0);
  }
  const app = setup(); app.select(); const first = app.click('run'); app.click('clear'); const second = app.click('run');
  app.respond(1); await second; const text = app.dom.summary.textContent; app.requests[0].reject(new Error('late failure')); await first;
  assert.equal(app.dom.summary.textContent, text); assert.equal(app.dom.save.disabled, false);
  app.api.setConnected(false); app.update([candidate()], 'session/1', false); app.api.setConnected(true); app.choose(candidate().id); await app.click('run');
  assert.equal(app.dom.run.disabled, true); assert.equal(app.requests.length, 2, 'cached render and reconnect cannot restore readiness');
  app.update(); assert.equal(app.dom.run.disabled, true, 'successful refresh still needs explicit candidate'); app.choose(candidate().id);
  const third = app.click('run'); app.respond(2); await third; assert.equal(app.dom.save.disabled, false);
  app.events.pagehide(); app.events.pageshow(); app.api.setConnected(true); app.update([candidate()], 'session/1', false); app.choose(candidate().id);
  await app.click('run'); assert.equal(app.requests.length, 3, 'page restoration needs a new snapshot');
  const changed = setup(); changed.select(); const request = changed.click('run'); changed.dom.editor.value = '{}';
  changed.respond(0); await request; assert.equal(changed.dom.save.disabled, true, 'raw draft comparison does not rely solely on events');
}
function localFile(text, size = Buffer.byteLength(text)) {
  const bytes = Buffer.from(text); return { size, slice(start, end) { assert.equal(start, 0); assert.equal(end, 65537); return { arrayBuffer: async () => bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length) }; } };
}
async function filesAndExportRaces() {
  const app = setup(); app.select(); let p = app.click('run'); app.respond(0); await p;
  app.dom.file.files = []; await app.dom.file.listeners.change(); assert.equal(app.dom.save.disabled, false, 'file dialog cancel preserves result');
  const draft = '{"name":"raw draft, not parsed"}'; app.dom.file.files = [localFile(draft)]; await app.dom.file.listeners.change();
  assert.equal(app.dom.editor.value, draft); assert.equal(app.dom.save.disabled, true); assert.equal(app.requests.length, 1);
  const previous = app.dom.editor.value;
  for (const file of [localFile('x', 65537), localFile('x', 2), localFile(Buffer.from([0xff])), localFile('x', -1)]) {
    app.dom.file.files = [file]; await app.dom.file.listeners.change(); assert.equal(app.dom.editor.value, previous); assert.match(app.dom.status.textContent, /Не удалось загрузить/);
  }
  app.dom.file.files = [localFile('\ufeff{}')]; await app.dom.file.listeners.change(); assert.equal(app.dom.editor.value, '\ufeff{}', 'BOM must not be stripped on import');
  app.dom.file.files = [localFile(' '.repeat(65536))]; await app.dom.file.listeners.change(); assert.equal(app.dom.editor.value.length, 65536);
  for (const change of ['clear', 'input', 'candidate', 'session', 'server', 'disconnect', 'pagehide', 'analysis', 'new-import', 'template']) {
    const app = setup(); app.select(); let resolveFile;
    app.dom.file.files = [{ size: 2, slice() { return { arrayBuffer: () => new Promise(resolve => { resolveFile = resolve; }) }; } }];
    const importing = app.dom.file.listeners.change(); let analysis;
    if (change === 'clear') app.click('clear');
    if (change === 'input') app.edit('{');
    if (change === 'candidate') { app.update([candidate('next')]); app.choose('next'); }
    if (change === 'session') app.update([], 'other');
    if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update(); }
    if (change === 'disconnect') app.api.setConnected(false);
    if (change === 'pagehide') app.events.pagehide();
    if (change === 'analysis') analysis = app.click('run');
    if (change === 'template') app.click('template');
    if (change === 'new-import') { app.dom.file.files = [localFile('{"new":true}')]; await app.dom.file.listeners.change(); }
    const text = app.dom.editor.value, status = app.dom.status.textContent;
    resolveFile(new Uint8Array([123, 125]).buffer); await importing;
    assert.equal(app.dom.editor.value, text, change); assert.equal(app.dom.status.textContent, status, change);
    if (analysis) { app.respond(0); await analysis; assert.equal(app.dom.save.disabled, false); }
  }
  for (const stage of ['blob', 'append']) for (const change of ['clear', 'input', 'candidate', 'session', 'server', 'disconnect', 'pagehide']) {
    const app = setup(); app.select(); const p = app.click('run'); app.respond(0); await p;
    app[stage === 'blob' ? 'blobHook' : 'appendHook'](() => {
      if (change === 'clear') app.click('clear'); if (change === 'input') app.edit('{');
      if (change === 'candidate') { app.update([candidate('next')]); app.choose('next'); }
      if (change === 'session') app.update([], 'other');
      if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update(); }
      if (change === 'disconnect') app.api.setConnected(false); if (change === 'pagehide') app.events.pagehide();
    });
    app.click('save'); assert.equal(app.downloads.length, 0, `${stage}/${change}`); assert.equal(app.dom.save.disabled, true);
    [...app.timers.values()].forEach(timer => timer.fn()); assert.equal(app.revoked.length, 1);
  }
  const template = setup(); template.click('template'); assert.deepEqual(JSON.parse(template.dom.editor.value), example);
  const silentEdit = setup(); silentEdit.select(); const pending = silentEdit.click('run'); silentEdit.respond(0); await pending;
  silentEdit.dom.editor.value = '{}'; silentEdit.click('save'); assert.equal(silentEdit.downloads.length, 0); assert.equal(silentEdit.dom.save.disabled, true);
}
async function platformIntegration() {
  const app = setup(), c = app.context; app.select(); let pending = app.click('run');
  c.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: [], candidates: [candidate()], refreshRunning: false };
  c.dom = { sessionSelect: { value: 'different' }, spectrumStream: { value: 'other' }, health: {} };
  c.storeSession = () => {}; c.closeHandoff = () => {}; c.refresh = () => {};
  function load(name, async = false) {
    const start = platformCode.indexOf(`  ${async ? 'async ' : ''}function ${name}(`), end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0); vm.runInContext(platformCode.slice(start, end), c);
  }
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) load(name);
  c.changeSpectrumStream(); assert.equal(app.requests[0].options.signal.aborted, false, 'independent of selected spectrum stream');
  c.changeSession(); assert.equal(app.requests[0].options.signal.aborted, true); app.respond(0); await pending;
  c.renderSession = () => {}; c.renderAll = () => c.updateComparison(); c.reportError = () => {}; c.scheduleRefresh = () => {};
  c.state.sessionId = 'session/1'; c.state.candidates = [candidate()]; app.select(); pending = app.click('run');
  c.performRefresh = async () => { throw new Error('snapshot failed'); }; load('refresh', true); await c.refresh();
  assert.equal(app.requests[1].options.signal.aborted, true); app.respond(1); await pending; assert.equal(app.dom.run.disabled, true);
  app.api.setConnected(true); c.updateComparison(); app.choose(candidate().id); await app.click('run'); assert.equal(app.requests.length, 2);
  c.performRefresh = async () => false; await c.refresh(); app.choose(candidate().id); assert.equal(app.dom.run.disabled, true, 'stale skipped refresh is not success');
  c.performRefresh = async () => true; await c.refresh(); app.choose(candidate().id); pending = app.click('run'); app.respond(2); await pending;
  assert.equal(app.dom.save.disabled, false); assert.equal(app.requests.length, 3);
  const handlers = {}; c.API_ROOT = '/api/v1'; c.encode = encodeURIComponent; c.EVENT_TYPES = []; c.setSseStatus = () => {};
  c.EventSource = function () { this.addEventListener = (name, fn) => handlers[name] = fn; this.close = () => {}; }; c.window.EventSource = c.EventSource;
  load('connectEvents'); c.connectEvents(); handlers.error(); assert.equal(app.dom.save.disabled, true);
  handlers.open(); c.updateComparison(); app.choose(candidate().id); await app.click('run'); assert.equal(app.requests.length, 3, 'SSE open is not a fresh candidate snapshot');
  await c.refresh(); app.choose(candidate().id); pending = app.click('run'); app.respond(3); await pending; assert.equal(app.dom.save.disabled, false);
}
async function httpSmoke(base) {
  const response = await fetch(`${base}/api/v1/sessions/rounding-one/candidates`); assert.equal(response.status, 200);
  const all = (await response.json()).data, app = setup(), calls = []; app.window.location.origin = base;
  app.context.fetch = (url, options) => { calls.push({ url, options }); return fetch(base + url, options); };
  app.update(all, 'rounding-one'); app.click('template'); assert.equal(calls.length, 0);
  for (const [id, status, nearest, offset] of [['one-rounded', 'rounded', 103000000, -25123], ['one-unchanged', 'unchanged', 103000000, 0],
    ['one-tolerance', 'outside_tolerance', 103000000, -50000], ['one-outside', 'outside_ranges', null, null],
    ['one-receiver', 'rounded', 145500000, -6250], ['one-upper', 'rounded', 103100000, 25000]]) {
    app.choose(id); await app.click('run'); assert.equal(app.dom.save.disabled, false, app.dom.status.textContent);
    assert(app.dom.summary.textContent.includes(`(${status})`)); assert(app.dom.summary.textContent.includes(id)); assert.match(app.dom.summary.textContent, /\.000001/);
    if (nearest !== null) assert(app.dom.summary.textContent.includes(`${nearest} Гц`));
    if (offset !== null) assert(app.dom.summary.textContent.includes(`${offset >= 0 ? '+' : ''}${offset} Гц`));
  }
  const frozen = app.dom.summary.textContent; app.update([], 'rounding-one'); assert.equal(app.dom.summary.textContent, frozen);
  await app.click('run'); assert.equal(app.dom.save.disabled, false, app.dom.status.textContent); assert.equal(calls.length, 7);
  app.click('save'); assert.deepEqual(JSON.parse(await app.blobs[0].text()), example);
  app.update([], 'rounding-two'); assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.save.disabled, true); assert.equal(app.dom.run.disabled, true);
  console.log('Web HTTP frequency-rounding smoke passed: 6 decisions, both units, exact UTC, retained candidate, canonical export, scope reset');
}
(async () => {
  await happyAndFrozen(); await exactArithmetic(); await identityAndSource(); await corruptResponses(); await rawDraftsAndBodies();
  await staleAndReadiness(); await filesAndExportRaces(); await platformIntegration(); if (process.argv[4]) await httpSmoke(process.argv[4]);
  console.log('frequency-rounding behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
