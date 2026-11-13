const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const nodes = {};
for (const name of ['run', 'clear', 'sweep', 'frequency', 'profile', 'status', 'result']) {
  nodes[`field-${name}`] = {value:'', textContent:'', disabled:false, listeners:{}, children:[],
    addEventListener(event, fn) { this.listeners[event] = fn; },
    replaceChildren(...items) { this.children=items; this.value=items[0]?.value || ''; }};
}
let resolve, calls = [];
const window = {addEventListener() {}, setTimeout, clearTimeout};
const context = {window, document: {getElementById:id=>nodes[id], createElement:()=>({})},
  TextEncoder, TextDecoder, AbortController, console,
  fetch: (path, options) => { calls.push({path,options}); return new Promise(r=>{resolve=r;}); }};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const panel=window.OpenRadioFieldStrength;
const scope={sessionId:fixture.source.session_id, streamId:fixture.source.stream_id, sweeps:[fixture.source]};
function draft(){nodes['field-profile'].value=JSON.stringify(fixture.request.calibration);nodes['field-frequency'].value='200';}
function response(result, ok=true) {
  const raw=JSON.stringify(ok ? {contract:'openradioinspector-platform',contract_version:1,data:result}
    : {error:{message:'Несовместимая калибровка'}});
  return {ok, body:new ReadableStream({start(c){c.enqueue(new TextEncoder().encode(raw));c.close();}})};
}
async function run() {
  panel.update(scope); draft();
  const first=nodes['field-run'].listeners.click();
  assert.equal(calls.length,1);assert.deepEqual(JSON.parse(calls[0].options.body),fixture.request);
  resolve(response(fixture.result)); await first;
  assert.match(nodes['field-result'].textContent,/55/);assert.match(nodes['field-result'].textContent,/V\/m/);
  // Switching the source scope cancels and clears a stale reply.
  const stale=nodes['field-run'].listeners.click();
  panel.update({...scope, sessionId:'other'});
  resolve(response(fixture.result));await stale;assert.equal(nodes['field-result'].textContent,'');
  panel.update(scope);draft();
  const forged=nodes['field-run'].listeners.click();
  resolve(response({...fixture.result, source:{...fixture.result.source,id:'wrong'}}));await forged;
  assert.equal(nodes['field-result'].textContent,'');assert.match(nodes['field-status'].textContent,/источник|запрос/i);
  const missing=nodes['field-run'].listeners.click();resolve(response(null,false));await missing;
  assert.match(nodes['field-status'].textContent,/калибровка/i);
  const unknown=nodes['field-run'].listeners.click();
  resolve(response({...fixture.result,uncertainty_db:null}));await unknown;
  assert.match(nodes['field-result'].textContent,/неизвестна/);
  nodes['field-profile'].listeners.input();assert.equal(nodes['field-result'].textContent,'');
  // A rolling archive page can replace the selected source without changing scope.
  const inFlight=nodes['field-run'].listeners.click();
  panel.update({...scope,sweeps:[{...fixture.source,id:'replacement'}]});
  resolve(response(fixture.result));await inFlight;
  assert.equal(nodes['field-result'].textContent,'','source replacement rejects late reply');
  panel.update(scope);draft();
  const completed=nodes['field-run'].listeners.click();resolve(response(fixture.result));await completed;
  assert.match(nodes['field-result'].textContent,/V\/m/);
  panel.update({...scope,sweeps:[{...fixture.source,id:'replacement'}]});
  assert.equal(nodes['field-result'].textContent,'','source replacement clears frozen result');
  // Preserve original draft bytes for the core's strict JSON validation.
  panel.update(scope);draft();
  const ambiguous=nodes['field-profile'].value.replace('"version":1', '"version":1,"version":2');
  nodes['field-profile'].value=ambiguous;
  const duplicate=nodes['field-run'].listeners.click();
  assert.ok(calls.at(-1).options.body.includes('"version":1,"version":2'),
    'duplicate calibration keys must reach the strict core decoder unchanged');
  resolve(response(null,false));await duplicate;
  draft();nodes['field-profile'].value=nodes['field-profile'].value.replace('"uncertainty_db":2','"uncertainty_db":1e999');
  const nonfinite=nodes['field-run'].listeners.click();
  assert.ok(calls.at(-1).options.body.includes('1e999'),'nonfinite numbers must not be converted into null');
  resolve(response(null,false));await nonfinite;
  panel.setConnected(false);assert.equal(nodes['field-run'].disabled,true);
  console.log('field-strength WebUI behavior passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
