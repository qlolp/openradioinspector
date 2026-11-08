/* Execute the production comparison UI without requiring a browser or instruments. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const time = '2026-11-08T12:30:00+00:00';
function sweep(id, stream = 'tiny/1', level = -50) {
  return { id, stream_id: stream, level_unit: 'dBm', interval: { started_at: time, ended_at: time },
    frequencies_hz: [100, 200, 300], levels: [level, level + 1, level + 2], settings: { rbw: 1000 } };
}
function data(ref, target, session = 'session/1', stream = 'tiny/1') {
  return { session_id: session, stream_id: stream, reference: sweep(ref, stream), target: sweep(target, stream),
    frequencies_hz: [100, 200, 300], delta_levels: [-4, 2, 1], delta_unit: 'dB', points: 3,
    max_increase: { frequency_hz: 200, delta_db: 2 }, min_change: { frequency_hz: 100, delta_db: -4 } };
}
function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map();
  let timerId = 0;
  const drawing = [];
  const context2d = new Proxy({}, { get(target, key) { return target[key] || ((...args) => drawing.push([key, ...args])); } });
  for (const id of ['reference-select', 'pin', 'run', 'clear', 'reference', 'status', 'summary', 'canvas']) {
    dom[id] = { textContent: '', value: '', hidden: false, disabled: false, width: 960, height: 260,
      listeners: {}, addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...options) { this.options = options; this.value = options[0]?.value || ''; },
      setAttribute(name, value) { this[name] = value; }, getContext() { return context2d; } };
    Object.defineProperty(dom[id], 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
  }
  const window = { location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); } };
  const context = vm.createContext({ window, document: { getElementById(id) { return dom[id.replace('comparison-', '')]; } },
    Option: function(text, value) { this.textContent = text; this.value = value; }, URLSearchParams, AbortController,
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } });
  vm.runInContext(code, context);
  function update(sweeps, sessionId = 'session/1', streamId = 'tiny/1') {
    window.OpenRadioSweepComparison.update({ sessionId, streamId, sweeps });
  }
  function click(id) { return dom[id].listeners.click(); }
  function respond(index, value, status = 200) {
    requests[index].resolve({ ok: status === 200, status,
      json: async () => ({ contract: 'openradioinspector-platform', contract_version: 1, data: value }) });
  }
  return { context, dom, window, events, requests, timers, drawing, update, click, respond };
}
async function happyAndRolling() {
  const app = setup(), { dom } = app;
  const ref = '<img onerror=alert(1)> /?&', target = 'target/?&';
  app.update([sweep(ref), sweep(target)]);
  assert.equal(app.requests.length, 0, 'snapshot must not request a comparison');
  dom['reference-select'].value = ref;
  app.click('pin');
  assert(dom.reference.textContent.includes(ref));
  assert(dom.reference.textContent.includes('2026-11-08T12:30:00.000Z'));
  assert.equal(dom.run.disabled, false);
  const pending = app.click('run');
  app.click('run');
  assert.equal(app.requests.length, 1, 'repeat clicks coalesce');
  assert.equal(dom.run.disabled, true);
  const url = new URL(app.requests[0].url, app.window.location.origin);
  assert.equal(url.pathname, '/api/v1/sessions/session%2F1/sweep-comparison');
  assert.equal(url.searchParams.get('reference_id'), ref);
  assert.equal(url.searchParams.get('target_id'), target);
  assert.equal(app.requests[0].options.method, 'GET');
  app.respond(0, data(ref, target));
  await pending;
  assert.equal(dom.canvas.hidden, false);
  assert.match(dom.summary.textContent, /Δ = сравниваемый − опорный/);
  assert.match(dom.summary.textContent, /2 dB @ 200 Гц/);
  assert.match(dom.summary.textContent, /dBm/);
  assert(dom.summary.textContent.includes(ref));
  assert(app.drawing.some(row => row[0] === 'lineTo'), 'delta trace drawn');
  assert.equal(app.timers.size, 0);
  const frozen = dom.summary.textContent;
  const next = Array.from({ length: 40 }, (_, index) => sweep(`next-${index}`));
  app.update(next);
  assert.equal(app.requests.length, 1, 'poll does not recalculate');
  assert.equal(dom.summary.textContent, frozen, 'result stays labeled as the fixed pair');
  assert.match(dom.reference.textContent, /вне окна последних 40/);
  const comparison = app.click('run');
  const second = new URL(app.requests[1].url, app.window.location.origin);
  assert.equal(second.searchParams.get('reference_id'), ref, 'pin survives rolling window');
  assert.equal(second.searchParams.get('target_id'), 'next-39');
  app.respond(1, data(ref, 'next-39'));
  await comparison;
  app.click('clear');
  assert.equal(dom.canvas.hidden, true);
  assert.equal(dom.summary.textContent, '');
  assert.equal(dom.run.disabled, true);
}
async function errors() {
  for (const status of [404, 409, 500]) {
    const app = setup();
    app.update([sweep('a'), sweep('b')]); app.click('pin');
    const request = app.click('run'); app.respond(0, {}, status); await request;
    assert.equal(app.dom.canvas.hidden, true);
    assert.match(app.dom.status.textContent, /Сравнение недоступно/);
    assert.equal(app.dom.run.disabled, false);
    assert.match(app.dom.reference.textContent, /b/);
    if (status === 404) assert.match(app.dom.status.textContent, /не найден/);
    if (status === 409) assert.match(app.dom.status.textContent, /несовместимы/);
  }
  const app = setup(); app.update([sweep('a')]); app.click('pin');
  const request = app.click('run'); app.requests[0].reject(new Error('network offline')); await request;
  assert.match(app.dom.status.textContent, /network offline/);
  assert.equal(app.dom.run.disabled, false);
  for (const corrupt of [d => d.delta_unit = 'dBm', d => d.delta_levels = [Infinity], d => d.stream_id = 'other',
    d => d.reference.id = 'wrong', d => d.target.level_unit = 'S-meter', d => d.target.interval = {},
    d => d.frequencies_hz = [100, 99, 200], d => d.points = 16385, d => d.max_increase = null, d => d.max_increase.delta_db = 100,
    d => d.min_change.frequency_hz = 200, d => d.reference.settings = {},
    d => d.reference.settings = { sequence: 1 }, d => d.target.settings = { rbw: null }]) {
    const d = data('a', 'a'); corrupt(d);
    const pending = app.click('run'); app.respond(app.requests.length - 1, d); await pending;
    assert.equal(app.dom.canvas.hidden, true);
    assert.match(app.dom.status.textContent, /Сравнение недоступно/);
  }
}
async function staleAndScope() {
  for (const change of ['clear', 'pin', 'session', 'stream', 'server', 'pagehide', 'timeout']) {
    const app = setup(); app.update([sweep('a'), sweep('b')]); app.click('pin');
    const request = app.click('run');
    if (change === 'clear' || change === 'pin') app.click(change);
    if (change === 'session') app.update([sweep('new')], 'new-session');
    if (change === 'stream') app.update([sweep('new', 'tiny/2')], 'session/1', 'tiny/2');
    if (change === 'server') { app.window.location.origin = 'http://server-b'; app.update([sweep('new')]); }
    if (change === 'pagehide') app.events.pagehide();
    if (change === 'timeout') {
      assert.equal([...app.timers.values()][0].ms, 8000);
      app.update([sweep('next')]); // Polling must not reset deadline.
      assert.equal(app.timers.size, 1);
      [...app.timers.values()][0].fn();
      assert.match(app.dom.status.textContent, /таймаут/);
    }
    assert.equal(app.requests[0].options.signal.aborted, true, change);
    const status = app.dom.status.textContent;
    app.respond(0, data('b', 'b')); await request; // Simulate late response despite abort.
    assert.equal(app.dom.canvas.hidden, true, change);
    assert.equal(app.dom.summary.textContent, '', change);
    assert.equal(app.dom.status.textContent, status, change);
    if (!['pin', 'timeout'].includes(change)) assert.equal(app.dom.run.disabled, true, change);
    if (change === 'pagehide') { app.events.pageshow(); assert.equal(app.dom.run.disabled, true); }
  }
}
async function zeroAndNegative() {
  for (const values of [[0, 0, 0], [-3, -2, -1]]) {
    const app = setup(); app.update([sweep('same')]); app.click('pin');
    const request = app.click('run'), d = data('same', 'same');
    d.delta_levels = values;
    d.max_increase = { frequency_hz: values[0] === values[2] ? 100 : 300, delta_db: values[2] };
    d.min_change = { frequency_hz: 100, delta_db: values[0] };
    app.respond(0, d); await request;
    assert.equal(app.dom.canvas.hidden, false);
    assert.match(app.dom.summary.textContent, /Максимальное изменение/);
    assert(app.dom.summary.textContent.includes(`${values[2]} dB`));
  }
}
async function plotFrequenciesAndExtremes() {
  for (const grid of [[0, 1, 1000], [100, 100, 100], [100, 100, 300]]) {
    const app = setup(); app.update([sweep('a')]); app.click('pin');
    const request = app.click('run'), d = data('a', 'a');
    d.frequencies_hz = grid; d.delta_levels = [-1e308, 0, 1e308];
    d.max_increase = { frequency_hz: grid[2], delta_db: 1e308 };
    d.min_change = { frequency_hz: grid[0], delta_db: -1e308 };
    app.respond(0, d); await request;
    assert.equal(app.dom.canvas.hidden, false);
    const coords = app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0]));
    assert(coords.flatMap(row => row.slice(1)).every(Number.isFinite));
    const delta = coords.slice(-3);
    assert(delta[0][2] > delta[2][2], 'negative and positive extrema both preserved');
    if (grid[0] === grid[2]) assert.equal(delta[0][1], delta[2][1]);
    if (grid[0] === 0) assert(Math.abs((delta[1][1] - delta[0][1]) / (delta[2][1] - delta[0][1]) - .001) < 1e-8);
  }
  const app = setup();
  app.update([sweep('ref'), { ...sweep('latest'), level_unit: 's_meter_count' }]);
  app.click('pin'); const request = app.click('run');
  assert.equal(new URL(app.requests[0].url, app.window.location.origin).searchParams.get('target_id'), 'latest');
  app.respond(0, {}, 409); await request;
  assert.match(app.dom.status.textContent, /несовместимы/);
}
async function pointLimitsAndTies() {
  for (const count of [1, 16384]) {
    const app = setup(); app.update([sweep('a')]); app.click('pin');
    const request = app.click('run'), d = data('a', 'a');
    d.points = count;
    d.frequencies_hz = Array.from({ length: count }, (_, i) => i);
    d.delta_levels = Array(count).fill(0);
    d.delta_levels[0] = -5; d.delta_levels[count - 1] = 10;
    d.reference.levels = Array(count).fill(-50); d.target.levels = Array(count).fill(-40);
    const low = Math.min(...d.delta_levels), high = Math.max(...d.delta_levels);
    d.max_increase = { frequency_hz: d.delta_levels.indexOf(high), delta_db: high };
    d.min_change = { frequency_hz: d.delta_levels.indexOf(low), delta_db: low };
    app.respond(0, d); await request;
    assert.equal(app.dom.canvas.hidden, false);
    assert.equal(app.drawing.filter(row => ['moveTo', 'lineTo'].includes(row[0])).length, count + 2, 'every signed bin drawn');
  }
  const app = setup(); app.update([sweep('a')]); app.click('pin');
  const request = app.click('run'), d = data('a', 'a');
  d.delta_levels = [2, 2, -4];
  d.max_increase = { frequency_hz: 200, delta_db: 2 }; // Wrong later tied bin.
  d.min_change = { frequency_hz: 300, delta_db: -4 };
  app.respond(0, d); await request;
  assert.match(app.dom.status.textContent, /Некорректная сводка/);
}
async function platformIntegration() {
  const app = setup(); app.update([sweep('a')]); app.click('pin');
  const pending = app.click('run');
  const context = app.context;
  context.state = { sessionId: 'session/1', spectrumStreamId: 'tiny/1', sweeps: [sweep('a')] };
  context.dom = { sessionSelect: { value: 'session/2' }, spectrumStream: { value: 'tiny/2' } };
  context.storeSession = () => {}; context.closeHandoff = () => {};
  context.refresh = () => assert(app.requests[0].options.signal.aborted, 'scope invalidates before refresh is dispatched');
  for (const name of ['updateComparison', 'changeSession', 'changeSpectrumStream']) {
    const start = platformCode.indexOf(`  function ${name}(`);
    const end = platformCode.indexOf('\n  }', start) + 4;
    assert(start >= 0 && end > start);
    vm.runInContext(platformCode.slice(start, end), context);
  }
  context.changeSession();
  assert.equal(context.state.sweeps.length, 0);
  app.respond(0, data('a', 'a')); await pending;
  assert.equal(app.dom.canvas.hidden, true);
  context.changeSpectrumStream();
  assert.equal(context.state.spectrumStreamId, 'tiny/2');
}
(async () => {
  await happyAndRolling(); await errors(); await staleAndScope(); await zeroAndNegative(); await plotFrequenciesAndExtremes(); await pointLimitsAndTies(); await platformIntegration();
  await flush(); console.log('comparison behavior checks completed');
})().catch(error => { console.error(error); process.exitCode = 1; });
