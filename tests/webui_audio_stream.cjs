/* Execute production audio-stream handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

const STREAMS = [{ id: 'tiny/1', device: 'AR-DV1' }];

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
  for (const name of ['connect', 'disconnect', 'refresh', 'stream', 'source', 'status', 'summary', 'items']) {
    dom[name] = element(`audio-${name}`);
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
      getElementById(id) { return dom[id.replace('audio-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(streams = STREAMS, sessionId = 'session/1') {
    window.OpenRadioAudioStream.update({ sessionId, streams });
  }
  function click(name) { return dom[name].listeners.click(); }
  function streamResponse(chunks) {
    const state = { reads: 0 };
    return {
      ok: true, status: 200,
      headers: { get: (name) => name === 'Content-Type' ? 'audio/pcm' : null },
      body: { getReader: () => ({
        async read() {
          const value = state.reads < chunks.length ? chunks[state.reads++] : undefined;
          return value === undefined ? { done: true } : { done: false, value };
        },
        async cancel() {},
      }) },
    };
  }
  return { dom, events, requests, timers, window, context, update, click, streamResponse };
}

async function happyPath() {
  const app = setup();
  app.update();
  assert.equal(app.dom.connect.disabled, false);
  const promise = app.click('connect');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.requests[0].url, /\/audio\/stream\?stream_id=tiny%2F1/);
  assert.equal(app.dom.connect.disabled, true, 'no double connect');
  app.requests[0].resolve(app.streamResponse([new Uint8Array([1, 2, 3]), new Uint8Array([4, 5])]));
  await promise;
  assert.match(app.dom.items.children[0].textContent, /5 байт PCM16LE \(2 блоков\)/);
  assert.match(app.dom.status.textContent, /отключён|завершён/);
  assert.equal(app.dom.connect.disabled, false, 'reconnect after the stream ends');
}

async function guards() {
  // Wrong content type is refused before any rendering.
  const wrongType = setup();
  wrongType.update();
  const pending = wrongType.click('connect');
  await new Promise(resolve => setImmediate(resolve));
  wrongType.requests[0].resolve({
    ok: true, status: 200,
    headers: { get: () => 'text/html' },
    body: { getReader: () => ({ async read() { return { done: true }; }, async cancel() {} }) },
  });
  await pending;
  assert.match(wrongType.dom.status.textContent, /audio\/pcm/);
  // Errors never leave the stream marked open.
  const failed = setup();
  failed.update();
  const pendingFail = failed.click('connect');
  await new Promise(resolve => setImmediate(resolve));
  failed.requests[0].resolve({ ok: false, status: 503, headers: { get: () => null }, body: null });
  await pendingFail;
  assert.equal(failed.dom.connect.disabled, false);
  assert.match(failed.dom.status.textContent, /прерван|Ошибка/);
  // Status refresh renders the subscriber counters.
  const status = setup();
  status.update();
  const pendingStatus = status.click('refresh');
  await new Promise(resolve => setImmediate(resolve));
  status.requests[0].resolve({
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => ({ contract: 'openradioinspector-platform', contract_version: 1,
      data: { session_id: 'session/1', stream_id: 'tiny/1', subscribers: 1, max_subscribers: 4,
              queued_blocks: 2, dropped_blocks: 3 } }),
    text: async () => '',
  });
  await pendingStatus;
  assert.match(status.dom.items.children[0].textContent, /Подписчиков: 1 из 4/);
  assert.match(status.dom.items.children[0].textContent, /потеряно 3 блоков/);
  // Connection loss aborts the stream and disables the controls.
  const lost = setup();
  lost.update();
  lost.window.OpenRadioAudioStream.setConnected(false);
  assert.equal(lost.dom.connect.disabled, true);
  assert.equal(lost.dom.refresh.disabled, true);
}


async function oldStreamCannotPublishIntoNewScope() {
  const app = setup(); app.update();
  assert.equal(app.dom.stream.disabled, false);
  let completeRead;
  const old = app.click('connect');
  app.requests[0].resolve({ok: true, headers: {get: () => 'audio/pcm'}, body: {getReader: () => ({
    read: () => new Promise(resolve => { completeRead = resolve; }), cancel: async () => {},
  })}});
  await new Promise(resolve => setImmediate(resolve));
  app.update(STREAMS, 'session/2');
  assert.equal(app.requests[0].options.signal.aborted, true);
  const next = app.click('connect');
  completeRead({done: false, value: new Uint8Array([1,2,3])}); await old;
  assert.equal(app.dom.connect.disabled, true, 'old finally cannot release new connection');
  app.requests[1].resolve(app.streamResponse([])); await next;
  assert(!app.dom.items.children[0].textContent.includes('3 байт'), 'old bytes cannot enter new session');
}

(async () => {
  await oldStreamCannotPublishIntoNewScope();
  await happyPath();
  await guards();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioAudioStream\?\.update/);
  for (const name of ['window.OpenRadioAudioStream?.setConnected(true)',
                      'window.OpenRadioAudioStream?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("audio-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('audio-stream behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
