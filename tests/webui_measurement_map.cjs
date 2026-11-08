/* Execute production measurement-map handlers with synthetic data. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[2], 'utf8');
const platformCode = fs.readFileSync(process.argv[3], 'utf8');

function mapJson() {
  return JSON.stringify({
    format: 'openradioinspector-measurement-map', format_version: 1, name: 'Карта',
    points: [{ latitude_deg: 55.75, longitude_deg: 37.62, label: 'Точка 1',
      sector: { azimuth_deg: 90, beamwidth_deg: 60, radius_m: 1000 } }],
  });
}
function result() {
  return {
    session_id: 'session/1',
    map: JSON.parse(mapJson()),
    geojson: { type: 'FeatureCollection', features: [
      { type: 'Feature', geometry: { type: 'Point', coordinates: [37.62, 55.75] },
        properties: { kind: 'measurement_point', index: 0, label: 'Точка 1' } },
      { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[37.62, 55.75], [37.63, 55.76], [37.62, 55.75]]] },
        properties: { kind: 'antenna_sector', index: 0, label: 'Точка 1' } },
    ] },
    summary: { point_count: 1, sector_count: 1,
      bbox: { min_latitude_deg: 55.75, max_latitude_deg: 55.75,
              min_longitude_deg: 37.62, max_longitude_deg: 37.62 } },
  };
}

function setup() {
  const dom = {}, events = {}, requests = [], timers = new Map();
  const drawing = [];
  let timerId = 0;
  function element(id = '') {
    const result = {
      id, value: '', textContent: '', hidden: true, disabled: false, children: [], listeners: {},
      width: 960, height: 320,
      addEventListener(event, fn) { this.listeners[event] = fn; },
      replaceChildren(...items) { this.children = items; },
      setAttribute(name, value) { this[name] = value; }, click() {}, remove() {},
      getContext() {
        return new Proxy({}, { get(target, key) { return target[key] || ((...args) => drawing.push([key, ...args])); },
          set(target, key, value) { target[key] = value; drawing.push([key, value]); return true; } });
      },
    };
    Object.defineProperty(result, 'innerHTML', { set() { throw new Error('Unsafe HTML rendering'); } });
    return result;
  }
  for (const name of ['run', 'clear', 'editor', 'source', 'status', 'summary', 'items', 'canvas']) {
    dom[name] = element(`map-${name}`);
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
      getElementById(id) { return dom[id.replace('map-', '')] || element(id); },
      createElement(tag) { return element(tag); },
      body: { appendChild() {} },
    },
    URLSearchParams, AbortController, TextDecoder, TextEncoder, Blob,
    URL: { createObjectURL() { return 'blob:1'; }, revokeObjectURL() {} },
    fetch(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); },
  });
  vm.runInContext(code, context);
  function update(sessionId = 'session/1') {
    window.OpenRadioMeasurementMap.update({ sessionId });
  }
  function click(name) { return dom[name].listeners.click(); }
  function respond(index, data, status = 200, envelope = {}) {
    const body = Buffer.from(JSON.stringify({ contract: 'openradioinspector-platform', contract_version: 1, data, ...envelope }));
    requests[index].resolve({ status, ok: status >= 200 && status < 300,
                              headers: { get: () => String(body.length) }, text: async () => body.toString() });
  }
  return { dom, events, requests, timers, drawing, window, context, update, click, respond };
}

async function happyPath() {
  const app = setup();
  app.update();
  app.dom.editor.value = mapJson();
  const promise = app.click('run'); app.click('run');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(app.requests.length, 1, 'single flight');
  assert.match(app.requests[0].url, /\/api\/v1\/sessions\/session%2F1\/measurement-map$/);
  const sent = JSON.parse(app.requests[0].options.body);
  assert.deepEqual(Object.keys(sent), ['map_json']);
  assert.equal(sent.map_json, app.dom.editor.value);
  app.respond(0, result());
  await promise;
  assert.equal(app.dom.canvas.hidden, false);
  assert.match(app.dom.summary.textContent, /не измерение/);
  assert.match(app.dom.items.children[0].textContent, /Точек: 1, секторов: 1/);
  assert.match(app.dom.status.textContent, /GeoJSON в черновике/);
  // Both the polygon and the point are drawn.
  const fills = app.drawing.filter(row => row[0] === 'fillStyle');
  assert.ok(fills.length >= 2);
  assert.ok(app.drawing.some(row => row[0] === 'strokeStyle'));
}

async function guards() {
  const empty = setup();
  empty.update();
  await empty.click('run');
  assert.equal(empty.requests.length, 0, 'empty draft is refused before the request');
  assert.match(empty.dom.status.textContent, /Вставьте карту/);
  const forged = setup();
  forged.update();
  forged.dom.editor.value = mapJson();
  const pending = forged.click('run');
  await new Promise(resolve => setImmediate(resolve));
  forged.respond(0, { ...result(), session_id: 'other' });
  await pending;
  assert.equal(forged.dom.canvas.hidden, true);
  assert.match(forged.dom.status.textContent, /Не удалось построить карту/);
  for (const status of [400, 404, 409, 500]) {
    const app = setup();
    app.update();
    app.dom.editor.value = mapJson();
    const promise = app.click('run');
    await new Promise(resolve => setImmediate(resolve));
    app.respond(0, {}, status);
    await promise;
    assert.equal(app.dom.canvas.hidden, true);
  }
  const lost = setup();
  lost.update();
  lost.window.OpenRadioMeasurementMap.setConnected(false);
  assert.equal(lost.dom.run.disabled, true);
}

(async () => {
  await happyPath();
  await guards();
  const slice = platformCode.slice(platformCode.indexOf('  function updateComparison('),
                                   platformCode.indexOf('\n  }', platformCode.indexOf('  function updateComparison(')) + 4);
  assert.match(slice, /OpenRadioMeasurementMap\?\.update/);
  for (const name of ['window.OpenRadioMeasurementMap?.setConnected(true)',
                      'window.OpenRadioMeasurementMap?.setConnected(false)']) {
    assert(platformCode.includes(name), `${name} is wired`);
  }
  const guarded = setup();
  assert.throws(() => vm.runInContext('document.getElementById("map-summary").innerHTML = "<img>"', guarded.context), /Unsafe HTML/);
  console.log('measurement-map behavior checks completed');
})().catch(error => { console.error(error); process.exit(1); });
