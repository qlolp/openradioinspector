/* Execute production raster handlers with synthetic data and adversarial transport timing. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

function table() {
  return { format: 'openradioinspector-frequency-raster', format_version: 1, name: 'Synthetic raster',
    frequency_unit: 'Hz', entries: [{ name: 'band', start_hz: 100_000_000, stop_hz: 110_000_000, grid_step_hz: 100_000 }] };
}
function candidate(id = 'candidate/1', frequency_hz = 101_234_567) {
  return { id, center_frequency_hz: frequency_hz, source_stream_id: 'tiny/1', level_unit: 'dBm', state: 'new',
    interval: { started_at: '2026-11-08T12:30:00+00:00', ended_at: '2026-11-08T12:30:01+00:00' } };
}
function item(c = candidate(), entry = 'band', matched = true) {
  const rounded = matched ? 101_200_000 : null;
  return { candidate_id: c.id, frequency_hz: c.center_frequency_hz, matched,
    rounded_frequency_hz: rounded, delta_hz: matched ? rounded - c.center_frequency_hz : null,
    grid_step_hz: matched ? 100_000 : null, entry_name: matched ? entry : null };
}
function payload(items = [item()], artifact = table(), session = 'session/1') {
  const matched = items.filter(row => row.matched).length;
  return { session_id: session, order: 'request', table: structuredClone(artifact),
    count: items.length, matched_count: matched, unmatched_count: items.length - matched,
    items: structuredClone(items) };
}

function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map();
  let timerId = 0;
  const blobs = [], downloads = [], revoked = [];
  function element(id = '') {
    const result = {
      id, value: '', textContent: '', hidden: false, disabled: false, children: [], listeners: {},
      options: [], selectedOptions: [],
      addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...items) { this.children = items; this.options = items; this.selectedOptions = items.filter(i => i.selected); },
      setAttribute(name, value) { this[name] = value; }, click() { downloads.push(this); }, remove() {},
    };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'editor', 'file', 'template', 'save', 'draft', 'source', 'status', 'summary', 'items', 'candidates']) {
    dom[name] = element(`raster-${name}`);
  }
  dom.editor.value = JSON.stringify(table());
  dom.file.files = null; dom.file.value = '';
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('raster-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL(blob) { blobs.push(blob); return `blob:${blobs.length}`; }, revokeObjectURL(url) { revoked.push(url); } },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(candidates, sessionId = 'session/1') {
    window.OpenRadioFrequencyRaster.update({ sessionId, candidates });
  }
  function select(ids) {
    dom.candidates.children.forEach(option => { option.selected = ids.includes(option.value); });
    dom.candidates.selectedOptions = dom.candidates.children.filter(option => option.selected);
    if (dom.candidates.listeners.change) dom.candidates.listeners.change();
  }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const body = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope }));
    requests[index].resolve({ status, headers: { get: () => String(body.length) }, text: async () => body.toString() });
  }
  return { dom, events, requests, timers, window, context, update, select, click, respond, blobs, downloads, revoked };
}

async function happyPath() {
  const app = setup(), one = candidate('candidate/1'), two = candidate('candidate/2', 50_000_000);
  app.update([one, two]);
  assert.equal(app.requests.length, 0, 'snapshot updates never round');
  assert.equal(app.dom.candidates.disabled, false);
  assert.equal(app.dom.run.disabled, true, 'run needs a selection');
  app.select([one.id]);
  assert.equal(app.dom.run.disabled, false);
  const promise = app.click('run'); app.click('run');
  assert.equal(app.requests.length, 1, 'single flight');
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/frequency-rounding'); assert.equal(url.search, '');
  const sent = JSON.parse(app.requests[0].options.body);
  assert.deepEqual(Object.keys(sent).sort(), ['candidate_ids', 'table_json']);
  assert.deepEqual(sent.candidate_ids, [one.id]);
  assert.equal(sent.table_json, app.dom.editor.value);
  assert.equal(app.requests[0].options.method, 'POST');
  assert.equal(app.requests[0].options.headers['Content-Type'], 'application/json');
  assert.equal(app.requests[0].options.cache, 'no-store');
  app.respond(0, payload([item(one)])); await promise;
  assert.match(app.dom.summary.textContent, /Кандидатов: 1; совпало с диапазонами таблицы: 1; вне таблицы: 0/);
  assert.match(app.dom.summary.textContent, /Исходные частоты не изменены/);
  assert.equal(app.dom.items.children.length, 1);
  assert.match(app.dom.items.children[0].textContent, /candidate\/1: 101234567 Гц → 101200000 Гц \(Δ -34567 Гц, band, шаг 100000 Гц\)/);
  assert.match(app.dom.status.textContent, /кандидаты в архиве не изменялись/);
  assert.equal(app.dom.save.disabled, false);
  const frozen = app.dom.summary.textContent;
  app.update([one, two, candidate('candidate/3')]);
  assert.equal(app.dom.summary.textContent, frozen, 'new candidates never re-round');
  app.click('clear');
  assert.equal(app.dom.summary.textContent, ''); assert.equal(app.dom.items.children.length, 0);
  assert.equal(app.dom.save.disabled, true);
}

async function unmatchedAndGridGuard() {
  const app = setup(), one = candidate('candidate/1', 50_000_000), two = candidate('candidate/2');
  app.update([one, two]); app.select([one.id, two.id]);
  const promise = app.click('run');
  app.respond(0, payload([item(one, null, false), item(two)])); await promise;
  assert.match(app.dom.summary.textContent, /Кандидатов: 2; совпало с диапазонами таблицы: 1; вне таблицы: 1/);
  assert.match(app.dom.items.children[0].textContent, /вне диапазонов таблицы, частота не округлялась/);
  assert.equal(app.dom.items.children[0].textContent.includes('→'), false);
  // A faulty server cannot bless an off-grid rounded frequency.
  const bad = setup(); bad.update([candidate()]); bad.select(['candidate/1']);
  const forged = item(candidate());
  forged.rounded_frequency_hz = 101_234_500; forged.delta_hz = forged.rounded_frequency_hz - forged.frequency_hz;
  const pending = bad.click('run'); bad.respond(0, payload([forged])); await pending;
  assert.equal(bad.dom.save.disabled, true);
  assert.match(bad.dom.status.textContent, /не лежит на сетке своей записи/);
  // Unmatched rows must not carry derived values.
  const leaky = setup(); leaky.update([candidate()]); leaky.select(['candidate/1']);
  const leaked = item(candidate(), null, false); leaked.rounded_frequency_hz = 1;
  const pendingLeak = leaky.click('run'); leaky.respond(0, payload([leaked])); await pendingLeak;
  assert.equal(leaky.dom.save.disabled, true);
  assert.match(leaky.dom.status.textContent, /не должна иметь округлённого значения/);
  // Duplicate names in the echoed table are refused before rendering.
  const duplicate = setup(); duplicate.update([candidate()]); duplicate.select(['candidate/1']);
  const wrong = payload([item(candidate())]);
  wrong.table.entries = [wrong.table.entries[0], structuredClone(wrong.table.entries[0])];
  const pendingDuplicate = duplicate.click('run'); duplicate.respond(0, wrong); await pendingDuplicate;
  assert.equal(duplicate.dom.save.disabled, true);
  assert.match(duplicate.dom.status.textContent, /Имена записей таблицы не должны повторяться/);
}

async function draftAndTransport() {
  // Empty drafts disable run; malformed drafts reach the core raw and are refused there.
  for (const value of ['', '   ', '{', '{"name":"x","name":"y"}']) {
    const app = setup(); app.update([candidate()]);
    app.dom.editor.value = value; app.dom.editor.listeners.input();
    if (!value.trim()) {
      assert.equal(app.dom.run.disabled, true, 'empty draft disables run');
      continue;
    }
    app.select(['candidate/1']);
    const pending = app.click('run');
    assert.equal(JSON.parse(app.requests[0].options.body).table_json, value, 'raw draft is never rewritten');
    app.respond(0, {}, 400); await pending;
    assert.equal(app.dom.save.disabled, true);
  }
  // The template covers the candidates but is explicitly non-normative.
  const app = setup(); app.update([candidate(), candidate('candidate/2', 109_000_000)]);
  app.dom.editor.value = ''; app.dom.editor.listeners.input();
  app.click('template');
  assert.match(app.dom.editor.value, /НЕНОРМАТИВНЫЙ пример 100 кГц/);
  const draft = JSON.parse(app.dom.editor.value);
  assert.equal(draft.entries[0].grid_step_hz, 100_000);
  assert.equal(draft.entries[0].start_hz, 101_234_567 - 1_000_000);
  assert.equal(draft.entries[0].stop_hz, 109_000_000 + 1_000_000);
  assert.match(app.dom.status.textContent, /Черновик изменён/);
  // A run echoes the actual draft table, never the canned one.
  app.select(['candidate/1']);
  const entry = draft.entries[0];
  const echoed = item(candidate(), entry.name);
  echoed.rounded_frequency_hz = 101_234_567; echoed.delta_hz = 0; echoed.grid_step_hz = entry.grid_step_hz;
  const promise = app.click('run'); app.respond(0, payload([echoed], draft)); await promise;
  assert.equal(app.dom.save.disabled, false);
  // Editing the draft clears the frozen result.
  app.dom.editor.value = app.dom.editor.value.replace('"grid_step_hz": 100000', '"grid_step_hz": 200000');
  app.dom.editor.listeners.input();
  assert.equal(app.dom.save.disabled, true, 'editing clears the frozen result');
  assert.equal(app.dom.summary.textContent, '');
  // A selection change clears it too; the second run echoes the edited draft.
  const edited = JSON.parse(app.dom.editor.value);
  const echoedEdited = item(candidate(), edited.entries[0].name);
  echoedEdited.rounded_frequency_hz = 101_234_567; echoedEdited.delta_hz = 0;
  echoedEdited.grid_step_hz = edited.entries[0].grid_step_hz;
  const again = app.click('run'); app.respond(1, payload([echoedEdited], edited)); await again;
  assert.equal(app.dom.save.disabled, false);
  app.select([]);
  assert.equal(app.dom.save.disabled, true, 'selection change clears the result');
  assert.equal(app.dom.run.disabled, true, 'no selection disables run');
  // Transport failures and wrong envelopes never render a result.
  for (const status of [201, 400, 404, 409, 500]) {
    const fresh = setup(); fresh.update([candidate()]); fresh.select(['candidate/1']);
    const pending = fresh.click('run'); fresh.respond(0, payload(), status); await pending;
    assert.equal(fresh.dom.items.children.length, 0);
    assert.equal(fresh.dom.save.disabled, true);
  }
  const offline = setup(); offline.update([candidate()]); offline.select(['candidate/1']);
  const rejected = offline.click('run'); offline.requests[0].reject(new Error('offline')); await rejected;
  assert.match(offline.dom.status.textContent, /offline/);
  const wrongContract = setup(); wrongContract.update([candidate()]); wrongContract.select(['candidate/1']);
  const pendingWrong = wrongContract.click('run');
  wrongContract.respond(0, payload(), 200, { contract: 'wrong' }); await pendingWrong;
  assert.equal(wrongContract.dom.save.disabled, true);
  // Connection loss disables the panel without dropping the draft.
  const lost = setup(); lost.update([candidate()]); lost.select(['candidate/1']);
  window_setConnected(lost, false);
  assert.equal(lost.dom.run.disabled, true);
  assert.equal(lost.dom.candidates.disabled, true);
  assert.equal(lost.dom.editor.value, JSON.stringify(table()));
}
function window_setConnected(app, value) {
  app.window.OpenRadioFrequencyRaster.setConnected(value);
}


async function refreshAndFileRaces() {
  const app = setup(); app.update([candidate()]); app.select(['candidate/1']);
  const pending = app.click('run');
  app.update([candidate()]); window_setConnected(app, true); app.click('run');
  assert.equal(app.requests.length, 1, 'refresh must preserve single flight');
  app.respond(0, payload()); await pending;
  let completeA, completeB;
  function file(text, resolve) {
    const bytes = Buffer.from(text);
    return { size: bytes.length, slice: () => ({ arrayBuffer: () => new Promise(done => resolve(() => done(Uint8Array.from(bytes).buffer))) }) };
  }
  app.dom.file.files = [file('file A', done => { completeA = done; })];
  const first = app.dom.file.listeners.change();
  app.dom.file.files = [file('file B', done => { completeB = done; })];
  const second = app.dom.file.listeners.change();
  completeB(); await second;
  assert.equal(app.dom.editor.value, 'file B');
  app.dom.editor.value = 'user edit'; app.dom.editor.listeners.input();
  completeA(); await first;
  assert.equal(app.dom.editor.value, 'user edit', 'late file must not overwrite current draft');
  const concurrent = setup(); concurrent.update([candidate()]); concurrent.select(['candidate/1']);
  let finishRead;
  concurrent.dom.file.files = [file(JSON.stringify(table()), done => { finishRead = done; })];
  const reading = concurrent.dom.file.listeners.change();
  const calculation = concurrent.click('run');
  finishRead(); await reading;
  assert.equal(concurrent.requests[0].options.signal.aborted, true, 'applying imported draft cancels an older calculation');
  concurrent.respond(0, payload()); await calculation;
  assert.equal(concurrent.dom.save.disabled, true, 'previous table cannot be exported beside imported draft');

}

(async () => {
  await refreshAndFileRaces();
  await happyPath();
  await unmatchedAndGridGuard();
  await draftAndTransport();
  // platform.js must feed the panel the session candidates and its connection state.
  const wiring = setup();
  const start = platformCode.indexOf('  function renderCandidates('), end = platformCode.indexOf('\n  }', start) + 4;
  assert(start >= 0, 'renderCandidates exists');
  const slice = platformCode.slice(start, end);
  assert.match(slice, /OpenRadioFrequencyRaster\?\.update\(\{[\s\S]*sessionId: state\.sessionId, candidates: state\.candidates/);
  for (const name of ['window.OpenRadioFrequencyRaster?.setConnected(true)', 'window.OpenRadioFrequencyRaster?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  if (!vm.runInContext('Boolean(window.OpenRadioFrequencyRaster)', wiring.context)) throw new Error('panel API missing');
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("raster-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('frequency-raster behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
