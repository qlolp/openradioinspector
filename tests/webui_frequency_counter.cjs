/* Execute production frequency-counter handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

function sweep(id = 'sweep-0') {
  return { id, level_unit: 'dBm', points: 4, stream_id: 'tiny/1',
    interval: { started_at: '2026-11-08T12:00:00Z', ended_at: '2026-11-08T12:00:01Z' },
    settings: { rbw_hz: 1000 }, frequencies_hz: [100, 200, 300, 400], levels: [-90, -80, -50, -52] };
}
function result(overrides = {}) {
  return {
    session_id: 'session/1', stream_id: 'tiny/1',
    sweep: { id: 'sweep-0', interval: sweep().interval, level_unit: 'dBm', settings: { rbw_hz: 1000 } },
    window: { start_hz: 100, stop_hz: 400 }, resolution_hz: 10, raster_hz: 100,
    method: 'parabolic', peak_index: 2, peak_frequency_hz: 300, peak_level_dbm: -50,
    measured_frequency_hz: 343.75, uncertainty_hz: 50,
    measurement_methodology: 'frequency_counter',
    values: { measured_frequency_hz: 343.75, resolution_hz: 10, uncertainty_hz: 50 },
    ...overrides,
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
      replaceChildren(...items) {
        this.children = items; this.options = items;
        if (items.length && !items.some(option => option.value === this.value)) {
          this.value = items[0].value ?? this.value;
        }
      },
      setAttribute(name, value) { this[name] = value; }, click() {}, remove() {},
    };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'sweep', 'resolution', 'start', 'stop',
                      'source', 'status', 'summary', 'items']) {
    dom[name] = element(`counter-${name}`);
  }
  dom.resolution.value = '10'; dom.start.value = '100'; dom.stop.value = '400';
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('counter-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(sweeps, sessionId = 'session/1') {
    window.OpenRadioFrequencyCounter.update({ sessionId, streamId: 'tiny/1', sweeps });
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
  app.update([sweep(), { ...sweep('other'), level_unit: 's_meter_count' }]);
  assert.equal(app.dom.sweep.options.length, 1, 'only dBm sweeps are offered');
  assert.equal(app.dom.run.disabled, false);
  const promise = app.click('run'); app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.length, 1, 'single flight');
  assert.match(app.requests[0].url, /\/api\/v1\/sessions\/session%2F1\/frequency-counter$/);
  const sent = JSON.parse(app.requests[0].options.body);
  assert.deepEqual(Object.keys(sent).sort(), ['resolution_hz', 'start_hz', 'stop_hz', 'sweep_id']);
  assert.deepEqual(sent, { sweep_id: 'sweep-0', resolution_hz: 10, start_hz: 100, stop_hz: 400 });
  app.respond(0, result());
  await promise;
  assert.match(app.dom.summary.textContent, /343\.75 Гц ± 50 Гц/);
  assert.match(app.dom.summary.textContent, /max\(разрешение, сетка\)\/2/);
  assert.match(app.dom.summary.textContent, /frequency_counter/);
  assert.equal(app.dom.items.children.length, 1);
  assert.match(app.dom.items.children[0].textContent, /parabolic/);
  assert.match(app.dom.status.textContent, /не показание аппаратного счётчика/);
  // A new sweep list never re-measures automatically.
  const frozen = app.dom.summary.textContent;
  app.update([sweep(), sweep('sweep-1')]);
  assert.equal(app.dom.summary.textContent, frozen);
}

async function guards() {
  // Bad inputs are refused before any request.
  for (const change of [{ resolution: '0' }, { resolution: 'abc' },
                        { start: '500', stop: '400' }, { stop: '' }]) {
    const app = setup();
    app.update([sweep()]);
    for (const [key, value] of Object.entries(change)) app.dom[key].value = value;
    await app.click('run');
    assert.equal(app.requests.length, 0, JSON.stringify(change));
  }
  // Forged responses never render: off-window measurement is refused.
  const forged = setup();
  forged.update([sweep()]);
  const pending = forged.click('run');
  await new Promise(resolve => setImmediate(resolve));
  forged.respond(0, result({ measured_frequency_hz: 999 }));
  await pending;
  assert.equal(forged.dom.items.children.length, 0);
  assert.match(forged.dom.status.textContent, /вне окна/);
  // Values that contradict the methodology are refused.
  const wrongValues = setup();
  wrongValues.update([sweep()]);
  const pendingWrong = wrongValues.click('run');
  await new Promise(resolve => setImmediate(resolve));
  wrongValues.respond(0, result({ values: { measured_frequency_hz: 343.75 } }));
  await pendingWrong;
  assert.equal(wrongValues.dom.items.children.length, 0);
  // Transport failures and wrong envelopes never render.
  for (const status of [400, 404, 409, 500]) {
    const app = setup();
    app.update([sweep()]);
    const promise = app.click('run');
    await new Promise(resolve => setImmediate(resolve));
    app.respond(0, {}, status);
    await promise;
    assert.equal(app.dom.items.children.length, 0);
    assert.equal(app.dom.status.textContent.includes('Не удалось'), true);
  }
  // Connection loss disables the panel without dropping inputs.
  const lost = setup();
  lost.update([sweep()]);
  lost.window.OpenRadioFrequencyCounter.setConnected(false);
  assert.equal(lost.dom.run.disabled, true);
  assert.equal(lost.dom.resolution.value, '10');
}

(async () => {
  await happyPath();
  await guards();
  const wiring = setup();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioFrequencyCounter\?\.update/);
  for (const name of ['window.OpenRadioFrequencyCounter?.setConnected(true)',
                      'window.OpenRadioFrequencyCounter?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("counter-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('frequency-counter behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
