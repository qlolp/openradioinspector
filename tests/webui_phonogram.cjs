/* Execute production phonogram handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

const STREAMS = [{ id: 'tiny/1', device: 'AR-DV1' }];
function status(recording = false, last = null) {
  return {
    session_id: 'session/1', stream_id: 'tiny/1', sample_rate_hz: 48000, recording,
    pre_history_seconds: 5, buffered_seconds: 2.5, recorded_seconds: recording ? 1.25 : 0,
    started_at: recording ? '2026-11-08T12:00:00Z' : null, last,
  };
}
function last() {
  return {
    sample_rate_hz: 48000, pre_history_samples: 240000, recorded_samples: 48000,
    pre_history_seconds: 5, recorded_seconds: 1, duration_seconds: 6,
    started_at: '2026-11-08T12:00:00Z', ended_at: '2026-11-08T12:00:06Z',
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
  for (const name of ['start', 'stop', 'refresh', 'stream', 'source', 'status', 'summary', 'items']) {
    dom[name] = element(`phono-${name}`);
  }
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('phono-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(streams = STREAMS, sessionId = 'session/1') {
    window.OpenRadioPhonogram.update({ sessionId, streams });
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
  app.update();
  assert.equal(app.dom.start.disabled, false);
  // Start: POST to phonogram/start with the selected stream.
  const startPromise = app.click('start');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.length, 1);
  assert.match(app.requests[0].url, /\/phonogram\/start$/);
  assert.deepEqual(JSON.parse(app.requests[0].options.body), { stream_id: 'tiny/1' });
  app.respond(0, status(true));
  await startPromise;
  assert.match(app.dom.items.children[0].textContent, /запись: идёт/);
  // Stop returns the frozen result with the pre-history split.
  const stopPromise = app.click('stop');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.requests[1].url, /\/phonogram\/stop$/);
  app.respond(1, status(false, last()));
  await stopPromise;
  assert.match(app.dom.items.children[1].textContent, /5\.00 с предыстории/);
  assert.match(app.dom.items.children[1].textContent, /1\.00 с записи/);
  assert.match(app.dom.summary.textContent, /Предыстория фиксирована/);
  assert.match(app.dom.summary.textContent, /не генерирует/);
}

async function guards() {
  // No streams: the controls stay disabled and nothing is sent.
  const empty = setup();
  empty.update([]);
  assert.equal(empty.dom.start.disabled, true);
  assert.equal(empty.dom.stop.disabled, true);
  // Forged status never renders.
  const forged = setup();
  forged.update();
  const pending = forged.click('refresh');
  await new Promise(resolve => setImmediate(resolve));
  forged.respond(0, status(false, { pre_history_seconds: 'x' }));
  await pending;
  assert.equal(forged.dom.items.children.length, 0);
  assert.match(forged.dom.status.textContent, /Ошибка/);
  for (const code of [400, 404, 409, 500]) {
    const app = setup();
    app.update();
    const promise = app.click('start');
    await new Promise(resolve => setImmediate(resolve));
    app.respond(0, {}, code);
    await promise;
    assert.equal(app.dom.items.children.length, 0);
    assert.equal(app.dom.status.textContent.includes('Ошибка'), true);
  }
  const lost = setup();
  lost.update();
  lost.window.OpenRadioPhonogram.setConnected(false);
  assert.equal(lost.dom.start.disabled, true);
  // A session change clears the frozen state.
  const moved = setup();
  moved.update();
  const movePromise = moved.click('refresh');
  await new Promise(resolve => setImmediate(resolve));
  moved.respond(0, status(false, last()));
  await movePromise;
  assert.equal(moved.dom.items.children.length, 2);
  moved.update(STREAMS, 'session/2');
  assert.equal(moved.dom.items.children.length, 0);
}

(async () => {
  await happyPath();
  await guards();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioPhonogram\?\.update/);
  for (const name of ['window.OpenRadioPhonogram?.setConnected(true)',
                      'window.OpenRadioPhonogram?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("phono-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('phonogram behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
