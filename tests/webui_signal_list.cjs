/* Execute production signal-list handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

const STREAMS = [{ id: 'tiny/1', level_unit: 'dBm' }];
function entry() {
  return { frequency_hz: 101800000, bandwidth_hz: 12500,
    interval: { started_at: '2026-11-08T12:00:00Z', ended_at: '2026-11-08T12:00:01Z' },
    level_unit: 'dBm', peak_level: -50.5 };
}
function artifact(count = 1) {
  return { format: 'openradioinspector-signal-list', format_version: 1, name: 'Test',
    frequency_unit: 'Hz', entries: Array.from({ length: count }, entry) };
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
  for (const name of ['export', 'import', 'stream', 'name', 'editor', 'source', 'status', 'summary', 'items']) {
    dom[name] = element(`siglist-${name}`);
  }
  dom.name.value = 'Список сигналов';
  const window = {
    location: { origin: 'http://server-a' },
    addEventListener(event, fn) { events[event] = fn; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const context = vm.createContext({
    window,
    document: {
      getElementById(id) { return dom[id.replace('siglist-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(streams = STREAMS, sessionId = 'session/1') {
    window.OpenRadioSignalList.update({ sessionId, streams });
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
  assert.equal(app.dom.export.disabled, false);
  // Export fills the draft with the portable artifact.
  const exportPromise = app.click('export');
  await new Promise(resolve => setImmediate(resolve));
  assert.match(app.requests[0].url, /\/signal-list\/export\?name=/);
  app.respond(0, { session_id: 'session/1', exported: 1, list: artifact() });
  await exportPromise;
  assert.equal(JSON.parse(app.dom.editor.value).format, 'openradioinspector-signal-list');
  assert.match(app.dom.items.children[0].textContent, /Экспортировано сигналов: 1/);
  assert.match(app.dom.summary.textContent, /исходные записи не изменяются/);
  // Import creates candidates through the normal lifecycle.
  const importPromise = app.click('import');
  await new Promise(resolve => setImmediate(resolve));
  const sent = JSON.parse(app.requests[1].options.body);
  assert.deepEqual(Object.keys(sent).sort(), ['list_json', 'stream_id']);
  assert.equal(sent.stream_id, 'tiny/1');
  app.respond(1, { session_id: 'session/1', stream_id: 'tiny/1', imported: 1,
                   candidate_ids: ['candidate-1'], name: 'Test' });
  await importPromise;
  assert.match(app.dom.items.children[0].textContent, /Импортировано сигналов: 1/);
  assert.match(app.dom.items.children[0].textContent, /состояния new/);
  assert.match(app.dom.summary.textContent, /не конвертируются/);
}

async function guards() {
  // Empty draft is refused before any request.
  const empty = setup();
  empty.update();
  await empty.click('import');
  assert.equal(empty.requests.length, 0);
  assert.match(empty.dom.status.textContent, /Вставьте список/);
  // Forged responses never render.
  const forged = setup();
  forged.update();
  const pendingExport = forged.click('export');
  await new Promise(resolve => setImmediate(resolve));
  forged.respond(0, { session_id: 'other', exported: 1, list: artifact() });
  await pendingExport;
  assert.equal(forged.dom.items.children.length, 0);
  assert.match(forged.dom.status.textContent, /Ошибка/);
  const forgedImport = setup();
  forgedImport.update();
  forgedImport.dom.editor.value = JSON.stringify(artifact());
  const pendingImport = forgedImport.click('import');
  await new Promise(resolve => setImmediate(resolve));
  forgedImport.respond(0, { session_id: 'session/1', stream_id: 'tiny/1', imported: 2,
                            candidate_ids: ['a'], name: 'Test' });
  await pendingImport;
  assert.equal(forgedImport.dom.items.children.length, 0);
  for (const status of [400, 404, 409, 500]) {
    const app = setup();
    app.update();
    const promise = app.click('export');
    await new Promise(resolve => setImmediate(resolve));
    app.respond(0, {}, status);
    await promise;
    assert.equal(app.dom.items.children.length, 0);
    assert.equal(app.dom.status.textContent.includes('Ошибка'), true);
  }
  const lost = setup();
  lost.update();
  lost.window.OpenRadioSignalList.setConnected(false);
  assert.equal(lost.dom.export.disabled, true);
  assert.equal(lost.dom.import.disabled, true);
}

(async () => {
  await happyPath();
  await guards();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioSignalList\?\.update/);
  for (const name of ['window.OpenRadioSignalList?.setConnected(true)',
                      'window.OpenRadioSignalList?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("siglist-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('signal-list behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
