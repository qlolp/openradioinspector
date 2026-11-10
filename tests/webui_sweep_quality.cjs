const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[2],'utf8');
function source(id='sweep/1') { return {id,stream_id:'stream/1',level_unit:'dBm',frequencies_hz:[100,200,300],levels:[0,-20,5],settings:{nullable:null,nested:[true,1,'x']},interval:{started_at:'2026-11-09T00:00:00.000001Z',ended_at:'2026-11-09T00:00:01.000002Z'}}; }
function report(src=source(),margin=20,high=0) {
  const n=src.levels.length,peak=src.levels.indexOf(Math.max(...src.levels));
  const edges=[[0,1,'first'],[n-1,n-2,'last']].map(([index,neighbor_index,edge])=>({edge,index,frequency_hz:src.frequencies_hz[index],level_dbm:src.levels[index],neighbor_index,neighbor_level_dbm:src.levels[neighbor_index],delta_db:src.levels[index]-src.levels[neighbor_index],flagged:src.levels[index]-src.levels[neighbor_index]>=margin}));
  const count=src.levels.filter(v=>v>=high).length,flags=edges.filter(e=>e.flagged).map(e=>`${e.edge}_edge_jump`);if(count)flags.push('level_threshold');
  return {session_id:'session/1',stream_id:src.stream_id,sweep:{id:src.id,interval:src.interval,level_unit:src.level_unit,settings:src.settings},parameters:{edge_margin_db:margin,high_level_dbm:high},points:n,peak_index:peak,peak_frequency_hz:src.frequencies_hz[peak],peak_level_dbm:src.levels[peak],high_level_count:count,edge_assessments:edges,flags,assessment:flags.length?'review':'no_flags',method:'archived_edge_level_review_v1',hardware_diagnosis:false,raw_modified:false};
}
function stream(text,status=200,length=null) {let sent=false;return {status,headers:{get:()=>length},body:{getReader:()=>({async read(){if(sent)return {done:true};sent=true;return {done:false,value:Buffer.from(text)};},cancel(){return Promise.resolve();},releaseLock(){}})}};}
function setup() {
 const dom={},events={},requests=[],timers=new Map();let id=0;
 const element=()=>({value:'',textContent:'',children:[],listeners:{},disabled:false,addEventListener(name,fn){this.listeners[name]=fn;},replaceChildren(...children){this.children=children;}});
 for(const name of ['anchor','margin','high','run','clear','status','summary'])dom[name]=element();
 dom.margin.value='20';dom.high.value='0';
 const window={location:{origin:'http://localhost'},addEventListener(name,fn){events[name]=fn;},setTimeout(fn){const key=++id;timers.set(key,fn);return key;},clearTimeout(key){timers.delete(key);}};
 vm.runInNewContext(code,{window,document:{getElementById:id=>dom[id.replace('quality-','')],createElement:element},TextEncoder,TextDecoder,AbortController,fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}))});
 const update=(sweeps=[source()],sessionId='session/1',streamId='stream/1')=>window.OpenRadioSweepQuality.update({sweeps,sessionId,streamId});
 const click=()=>dom.run.listeners.click();
 const reply=(data=report(),status=200,index=0)=>requests[index].resolve(stream(JSON.stringify({contract:'openradioinspector-platform',contract_version:1,data}),status));
 return {dom,events,requests,timers,window,update,click,reply};
}
(async()=>{
 let a=setup();a.update();assert.equal(a.requests.length,0);let p=a.click();a.click();assert.equal(a.requests.length,1);
 assert.equal(a.requests[0].url,'/api/v1/sessions/session%2F1/sweep-quality');
 assert.equal(a.requests[0].options.method,'POST');assert.deepEqual(JSON.parse(a.requests[0].options.body),{sweep_id:'sweep/1',edge_margin_db:20,high_level_dbm:0});
 a.reply();await p;assert.match(a.dom.summary.textContent,/first_edge_jump, last_edge_jump, level_threshold/);assert.match(a.dom.summary.textContent,/не диагностирует/);assert.equal(a.timers.size,0);
 a.update([]);assert.equal(a.dom.anchor.value,'sweep/1');p=a.click();a.reply(report(),200,1);await p;assert.match(a.dom.summary.textContent,/review/);
 for(const settings of [null,{},[],{a:[null,{b:true}]}]){a=setup();const s=source();s.settings=settings;a.update([s]);p=a.click();a.reply(report(s));await p;assert.match(a.dom.summary.textContent,/review/);}
 for(const mutate of [r=>r.session_id='foreign',r=>r.stream_id='foreign',r=>r.sweep.id='bad',r=>r.sweep.interval.started_at='2026-11-09T00:00:00.000002Z',r=>r.sweep.settings={bad:1},r=>r.points=true,r=>r.points=16385,r=>r.peak_index=0,r=>r.peak_level_dbm=true,r=>r.peak_frequency_hz=0,r=>r.high_level_count=1,r=>r.edge_assessments.pop(),r=>r.edge_assessments[0].delta_db=19,r=>r.edge_assessments[0].flagged=1,r=>r.edge_assessments[1].neighbor_index=0,r=>r.flags.reverse(),r=>r.flags.push('diagnosis'),r=>r.assessment='no_flags',r=>r.method='other',r=>r.raw_modified=true,r=>r.hardware_diagnosis=0,r=>r.parameters.edge_margin_db=true]){
  a=setup();a.update();p=a.click();const r=report();mutate(r);a.reply(r);await p;assert.equal(a.dom.summary.textContent,'');assert.match(a.dom.status.textContent,/недоступен/);
 }
 for(const path of [[],['sweep'],['sweep','interval'],['parameters'],['edge_assessments',0],['edge_assessments',1]]) {
  a=setup();a.update();p=a.click();const r=report();let target=r;for(const key of path)target=target[key];target.unexpected=null;
  a.reply(r);await p;assert.equal(a.dom.summary.textContent,'',JSON.stringify(path));assert.match(a.dom.status.textContent,/недоступен/);
 }
 for(const [field,value] of [['margin','0'],['margin','201'],['margin','NaN'],['high','101'],['high','-201'],['high',''],['high','Infinity']]){a=setup();a.update();a.dom[field].value=value;await a.click();assert.equal(a.requests.length,0);}
 for(const change of [s=>s.levels=[true,-20,5],s=>s.frequencies_hz=[100,100,300],s=>s.frequencies_hz=[100,300,200],s=>s.id='x'.repeat(129),s=>s.id='\ud800',s=>s.session_id='wrong',s=>s.levels=[5],s=>s.settings={a:Infinity}]){a=setup();const s=source();change(s);a.update([s]);await a.click();assert.equal(a.requests.length,0);}
 for(const status of [201,202,204,400,404,409,413,500]){a=setup();a.update();p=a.click();a.reply(report(),status);await p;assert.equal(a.dom.summary.textContent,'');assert.match(a.dom.status.textContent,/недоступен/);}
 for(const action of ['clear','margin','high','selection','scope','disconnect','offline','pagehide','timeout']){
  a=setup();a.update([source(),source('sweep/2')]);p=a.click();
  if(action==='clear')a.dom.clear.listeners.click();
  if(['margin','high'].includes(action)){a.dom[action].value='10';a.dom[action].listeners.input();}
  if(action==='selection'){a.dom.anchor.value='sweep/1';a.dom.anchor.listeners.change();}
  if(action==='scope')a.update([source()],'other');
  if(action==='disconnect')a.window.OpenRadioSweepQuality.setConnected(false);
  if(['offline','pagehide'].includes(action))a.events[action]();
  if(action==='timeout')[...a.timers.values()][0]();
  assert(a.requests[0].options.signal.aborted,action);a.reply(report(source('sweep/2')));await p;assert.equal(a.dom.summary.textContent,'',action);
 }
 a=setup();a.update();p=a.click();a.dom.margin.listeners.input();const q=a.click();a.reply(report(),200,1);await q;const current=a.dom.summary.textContent;a.reply();await p;assert.equal(a.dom.summary.textContent,current);
 for(const raw of ['{"contract":1,"contract":2}', '{"data":1e999}']){a=setup();a.update();p=a.click();a.requests[0].resolve(stream(raw));await p;assert.equal(a.dom.summary.textContent,'');}
 a=setup();a.update();p=a.click();a.requests[0].resolve(stream('x',200,'2000001'));await p;assert.match(a.dom.status.textContent,/предел/);
 a=setup();const two=source();two.levels=[0,0];two.frequencies_hz=[0,Number.MAX_SAFE_INTEGER];a.update([two]);p=a.click();a.reply(report(two));await p;assert.match(a.dom.summary.textContent,/2 точек/);
 console.log('quality behavior checks completed');
})().catch(e=>{console.error(e);process.exitCode=1;});
