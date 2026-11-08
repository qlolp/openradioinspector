/* Execute production state, rendering, polling and event handlers, synthetic data only. */
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const root = require('node:path').resolve(__dirname, '..');
const model = require(root + '/radioinspector/web/static/spectrum-view.js');
const fixture = JSON.parse(fs.readFileSync(root + '/tests/fixtures/spectrum-presentation-v1.json'));
const copy = structuredClone;
for (const c of fixture.scale_cases) {
  const range = model.fit(c.levels);
  for (const [i, expected] of [c.expected_lower, c.expected_upper].entries()) assert(Math.abs(range[i] / expected - 1) < 1e-14, c.name);
  for (const v of c.levels) assert(Number.isFinite(model.fraction(v, range)));
}
const {scope, sweeps} = fixture.frame, v = new model.View();
const original = JSON.stringify(sweeps);
v.update(scope, sweeps); assert.deepEqual(v.bounds, [fixture.frame.expected_lower, fixture.frame.expected_upper]);
const first = [...v.bounds]; v.pause(); v.pause();
const newer = copy(sweeps); newer[1].id = 'new'; newer[1].levels = [100, 110, 120];
v.update(scope, newer); assert.equal(v.display.at(-1).id, 'sweep-2'); assert.deepEqual(v.bounds, first);
v.fit(); assert.deepEqual(v.bounds, first); v.manual(-80, -60); assert.equal(v.clipped(), 3);
v.resume(); v.resume(); assert.equal(v.display.at(-1).id, 'new'); assert.deepEqual(v.bounds, [-80, -60]);
for (const [lo, hi] of [...fixture.invalid_manual_bounds, [NaN, 2], [-Infinity, 1], [0, Infinity]]) { assert.equal(v.manual(lo, hi), false); assert.deepEqual(v.bounds, [-80, -60]); }
v.manual(-Number.MAX_VALUE, Number.MAX_VALUE); assert.equal(model.fraction(0, v.bounds), 0.5);
v.update(scope, Array.from({length: 60}, (_, i) => ({...sweeps[0], id: String(i)}))); assert.equal(v.display.length, fixture.expected_capacity);
const changed = copy(sweeps); changed[1].frequencies_hz = [100, 200, 500]; v.pause(); v.update(scope, changed); assert.equal(v.paused, false); assert.equal(v.display.length, 1);
v.pause(); v.update({...scope, server:'other'}, sweeps); assert.equal(v.paused, false);
for (const bad of [[], null, [{...sweeps[0], stream_id:'foreign'}], [{...sweeps[0], session_id:'foreign'}], [{...sweeps[0], level_unit:'s_meter_count'}], [{...sweeps[0], levels:[NaN]}]]) {
  v.update(scope, sweeps); v.pause(); v.update(scope, bad); assert.equal(v.display.length, 0); assert.equal(v.paused, false);
}
// Cross-client fixture parity, including exact renderer frequency limits.
for (const interval of fixture.valid_intervals) {
  v.update(scope, [{...sweeps[0], interval}]); assert.equal(v.display.length, 1);
  assert.deepEqual(v.display[0].interval, interval);
}
for (const interval of fixture.invalid_intervals) {
  v.update(scope, sweeps); v.pause(); v.update(scope, [{...sweeps[0], interval}]);
  assert.equal(v.display.length, 0); assert.equal(v.paused, false);
}
for (const [frequency, expectedCount] of [[fixture.maximum_frequency_hz, 1], [fixture.maximum_frequency_hz + 1, 0]]) {
  v.update(scope, [{...sweeps[0], frequencies_hz: [frequency], levels: [-80]}]);
  assert.equal(v.display.length, expectedCount);
}
// Calendar validity, strict UTC grammar and exact microsecond ordering.
for (const interval of [
  {started_at:'2026-02-31T00:00:00Z',ended_at:'2026-03-04T00:00:00Z'},
  {started_at:'2025-02-29T00:00:00Z',ended_at:'2025-03-01T00:00:00Z'},
  {started_at:'2026-11-08T24:00:00Z',ended_at:'2026-11-09T01:00:00Z'},
  {started_at:'2026-11-08T00:60:00Z',ended_at:'2026-11-09T01:00:00Z'},
  {started_at:'2026-11-08T00:00:60Z',ended_at:'2026-11-09T01:00:00Z'},
  {started_at:'2026-11-08T00:00:00.000002Z',ended_at:'2026-11-08T00:00:00.000001+00:00'},
  ...['2026-11-08', '2026-11-08Z', '2026-11-08T00:00:00',
      '2026-11-08T00:00:00+01:00', '2026-11-08T00:00:00.0000001Z',
      '2026-11-08T00:00:00Z\n', '0000-01-01T00:00:00Z'].map(t=>({started_at:t,ended_at:t})),
]) {
  v.update(scope,sweeps); v.pause(); v.update(scope,[{...sweeps[0],interval}]);
  assert.equal(v.display.length,0,JSON.stringify(interval)); assert.equal(v.paused,false);
}
for (const interval of [
  {started_at:'2024-02-29T00:00:00Z',ended_at:'2024-02-29T00:00:00.000001+00:00'},
  {started_at:'2026-11-08T00:00:00.1Z',ended_at:'2026-11-08T00:00:00.100000+00:00'},
  {started_at:'2026-11-08T00:00:00.000001Z',ended_at:'2026-11-08T00:00:00.000002Z'},
]) {
  v.update(scope,[{...sweeps[0],interval}]); assert.equal(v.display.length,1);
  assert.deepEqual(v.display[0].interval,interval);
}
assert.equal(JSON.stringify(sweeps), original);

async function integration() {
  const dom = {}, requests = [], intervals = [], drawing = [];
  const ctx = new Proxy({ createLinearGradient: () => ({addColorStop(){}}), createImageData: (w,h) => ({data:new Uint8ClampedArray(w*h*4)}) }, {
    get(t,k) { return t[k] || ((...args) => { for (const a of args) if (typeof a === 'number') assert(Number.isFinite(a), `${k} nonfinite`); drawing.push([k,...args]); }); }
  });
  function element() { return {value:'', textContent:'', dataset:{}, width:96, height:60, listeners:{}, children:[],
    addEventListener(k,f){this.listeners[k]=f;}, replaceChildren(...x){this.children=x;}, append(...x){this.children.push(...x);},
    setAttribute(k,v){this[k]=v;}, getContext(){return ctx;}, focus(){}, reset(){}, scrollIntoView(){} }; }
  let current = copy(sweeps), fail = false;
  const session = {id:scope.session, name:'synthetic', state:'running', streams:[{id:scope.stream, role:'spectrum_analyzer', device:'synthetic', level_unit:'dBm',settings:{}}]};
  const document = { getElementById(id){return dom[id] ||= element();}, createElement:element, createTextNode:s=>s, addEventListener(){}};
  const window = {location:{origin:scope.server}, localStorage:{getItem(){return null;},setItem(){}}, addEventListener(){}, setTimeout(){return 1;}, clearTimeout(){},setInterval(f){intervals.push(f);}};
  const context = vm.createContext({window,document,console,Intl,Date,Uint8ClampedArray,Option:function(t,v){this.value=v;},fetch:async(url,options)=>{
    requests.push({url,options}); if(fail) throw Error('offline');
    let data = [];
    if(url.endsWith('/capabilities')) data={features:{}};
    else if(url.endsWith('/health')) data={status:'ok',last_event_id:1,sessions:1};
    else if(url.endsWith('/sessions')) data=[session];
    else if(url.includes('/sweeps?')) data=current;
    else if(url.endsWith('/automatic-detection') || url.endsWith('/automatic-handoff')) data={policy:{enabled:false},runtime:{}};
    else if(url.endsWith('/next')) data=null;
    return {ok:true,status:200,text:async()=>JSON.stringify({data})};
  }});
  vm.runInContext(fs.readFileSync(root+'/radioinspector/web/static/spectrum-view.js','utf8'),context);
  vm.runInContext(fs.readFileSync(root+'/radioinspector/web/static/platform.js','utf8'),context);
  const settle = async()=>{for(let i=0;i<30;i++) await Promise.resolve();}; await settle();
  const click = async(id)=>{dom[id].listeners.click(); await settle();};
  assert.match(dom['time-readout'].textContent,/sweep-2/); assert.match(dom['time-readout'].textContent,/000001\+00:00/);
  assert.match(dom['waterfall-scale'].textContent,/не время.*порядок бинов/);
  await click('spectrum-pause'); const frozen = dom['time-readout'].textContent, peak = dom['peak-readout'].textContent;
  assert.match(dom['spectrum-view-status'].textContent,/пауза только просмотра; состояние сбора не изменено/);
  assert.equal(dom['mark-peak'].disabled,true); await click('mark-peak'); assert.equal(dom['candidate-form'].dataset.observationId,undefined);
  current=copy(newer); intervals[0](); await settle(); assert.equal(dom['time-readout'].textContent,frozen); assert.equal(dom['peak-readout'].textContent,peak);
  const manual=(lo,hi)=>{document.getElementById('spectrum-min').value=lo;document.getElementById('spectrum-max').value=hi;dom['spectrum-scale-form'].listeners.submit({preventDefault(){}});};
  // Copied visible example and placeholder must work through the real submit handler.
  const html = fs.readFileSync(root+'/radioinspector/web/static/platform.html','utf8');
  const sample = /Границы:[^<]*\(например ([^)]+)\)/.exec(html)[1];
  const placeholder = /id="spectrum-min"[^>]*placeholder="([^"]+)"/.exec(html)[1];
  for (const text of [sample, placeholder]) {
    manual(text,'0');
    assert.equal(dom['spectrum-scale-error'].textContent,'');
    assert(dom['spectrum-view-status'].textContent.includes(`шкала ${Number(text)} … 0 dBm`));
  }
  manual('-80','-60'); assert.match(dom['spectrum-view-status'].textContent,/отсечено значений: 3/);
  for(const invalid of ['', '0xff','NaN','Infinity']) {manual(invalid,'200');assert.match(dom['spectrum-scale-error'].textContent,/конечные/);}
  for (const c of fixture.manual_cases) {
    const before = dom['spectrum-view-status'].textContent;
    manual(c.lower,c.upper);
    if (c.valid) {
      assert.equal(dom['spectrum-scale-error'].textContent,'');
      assert(dom['spectrum-view-status'].textContent.includes(`шкала ${Number(c.lower)} … ${Number(c.upper)} dBm`));
    } else {
      assert.match(dom['spectrum-scale-error'].textContent,/конечные/);
      assert.equal(dom['spectrum-view-status'].textContent,before);
    }
  }
  await click('spectrum-fit'); assert.equal(dom['spectrum-scale-error'].textContent,'');
  manual('-1.7976931348623157e308','1.7976931348623157e308');
  await click('spectrum-pause'); assert.match(dom['time-readout'].textContent,/new/); assert.equal(dom['mark-peak'].disabled,false);
  await click('mark-peak'); assert.equal(dom['candidate-form'].dataset.observationId,'new');
  assert.equal(requests.filter(r=>r.options.method!=='GET').length,0);
  // A draft survives polling; compatible grid suffix and foreign source responses fail closed.
  manual('-100','0'); document.getElementById('spectrum-min').value='-123.5';
  intervals[0](); await settle(); assert.equal(dom['spectrum-min'].value,'-123.5');
  await click('spectrum-pause'); current=copy(changed); intervals[0](); await settle();
  assert.equal(dom['spectrum-pause'].textContent,'Pause view'); assert.match(dom['waterfall-scale'].textContent,/1 свипов/);
  current=[{...sweeps[0],session_id:'foreign'}]; intervals[0](); await settle(); assert.equal(dom['time-readout'].textContent,'—');
  current=[{...sweeps[0],session_id:'foreign'}, ...Array.from({length:40},()=>copy(sweeps[0]))];
  intervals[0](); await settle(); assert.equal(dom['time-readout'].textContent,'—');
  current=copy(sweeps); intervals[0](); await settle();
  dom['spectrum-stream'].value='other'; dom['spectrum-stream'].listeners.change();
  assert.equal(dom['time-readout'].textContent,'—'); await settle();
  assert.match(dom['time-readout'].textContent,/sweep-2/);
  // Refresh stays operational during pause; loss of data and network fail closed.
  await click('spectrum-pause'); current=[]; intervals[0](); await settle(); assert.equal(dom['time-readout'].textContent,'—');
  current=copy(sweeps); intervals[0](); await settle(); assert.match(dom['time-readout'].textContent,/sweep-2/);
  fail=true; intervals[0](); await settle(); assert.equal(dom['time-readout'].textContent,'—'); assert.equal(dom['mark-peak'].disabled,true);
  assert.equal(JSON.stringify(sweeps),original); assert(drawing.length>0);
}
integration().then(()=>console.log('spectrum-view behavior checks completed')).catch(e=>{console.error(e);process.exitCode=1;});
