const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
function element(){ return {value:'',textContent:'',dataset:{},selectedOptions:[],options:[],children:[],listeners:{},checked:false,
 addEventListener(e,f){(this.listeners[e] ||= []).push(f);},removeAttribute(){},pause(){},play(){return Promise.resolve();},
 width:1000,height:240,getContext(){return new Proxy({}, {get:()=>()=>{}});},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;this.options=x;if(!x.some(v=>v.value===this.value))this.value=x[0]?.value||'';}}; }
const nodes={}, root=element(), pending=[], emitted=[], timers=new Map();let timerId=0;
root.querySelector=s=>nodes[s]||(nodes[s]=element());
const context=vm.createContext({document:{getElementById:()=>root,body:{dataset:{}},createElement:()=>element()},
 window:{dispatchEvent:e=>emitted.push(e),addEventListener(){},setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}},AbortController,CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},URLSearchParams,Option:function(t,v){return Object.assign(element(),{textContent:t,value:v});},localStorage:{getItem:()=>null,setItem(){}},
 fetch:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve})),FormData:class{},console});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
async function respond(data){pending.shift().resolve({ok:true,text:async()=>JSON.stringify({data})});await new Promise(setImmediate);}
(async()=>{
 await respond({items:[{id:'project-1',name:'Room',site:'Office',state:'ready'}]});
 const projects=nodes['[name=project]']; assert.equal(projects.value,'project-1');
 const eventRequest=nodes['[data-action=events]'].onclick();
 await respond({items:[{id:'event-1',first_seen:'UTC',center_frequency_hz:100000000,peak_level:-70,kind:'impulse'}]});await eventRequest;
 assert.equal(projects.value,'project-1','event list must not replace project selection');
 assert.equal(projects.options[0].textContent,'Room · Office · ready');
 const arm=nodes['[data-action=arm]'].onclick();assert.equal(pending[0].url,'/api/v1/monitoring-projects/project-1/arm');
 assert.equal(pending[0].options.body,'{}');await respond({state:'armed'});await arm;
 nodes['[data-action=audio-config]'].onclick();assert.equal(pending.length,0,'audio requires explicit consent');
 if(process.argv[3]) {
  vm.runInContext(fs.readFileSync(process.argv[3],'utf8'),context);
  root.querySelector('[name=manual-role]').value='receiver'; root.querySelector('[name=manual-frequency]').value='100000000';root.querySelector('[name=receiver-mode]').value='AUTO';
  nodes['[data-action=manual-tune]'].onclick();assert.equal(pending.length,0,'manual tune needs ownership');
  const acquired=nodes['[data-action=manual-acquire]'].onclick();
  assert.equal(JSON.parse(pending[0].options.body).role,'receiver');await respond({token:'private-token',role:'receiver',expires_seconds:30});await acquired;
  const tuned=nodes['[data-action=manual-tune]'].onclick();assert.equal(JSON.parse(pending[0].options.body).frequency_hz,100000000);await respond({tuned:{}});await tuned;
  const history=nodes['[data-action=history]'].onclick();assert.match(pending[0].url,/history\?limit=32/);
  await respond({items:[{id:'sweep',session_id:'s',stream_id:'a',interval:{started_at:'2026-01-01T00:00:00Z',ended_at:'2026-01-01T00:00:01Z'},frequencies_hz:[100,200],levels:[-90,-80],original_point_count:2,display_only:true}],next_cursor:null});await history;
  assert.equal(nodes['[data-role=history-chart]'].dataset.startUtc,'2026-01-01T00:00:00.000Z');
  assert.equal(nodes['[data-action=history-next]'].disabled,true);
  assert.equal(projects.value,'project-1');
 }
 const status1=context.window.oriMonitoring.request('/project-1/status');await respond({project:{id:'project-1',active_session_id:'new-run'}});await status1;
 const status2=context.window.oriMonitoring.request('/project-1/status');await respond({project:{id:'project-1',active_session_id:'new-run'}});await status2;
 assert.equal(emitted.filter(e=>e.type==='ori-monitoring-session').length,1,'follow new guard session exactly once');
 if(process.argv[4]){
  vm.runInContext(fs.readFileSync(process.argv[4],'utf8'),context);
  for(const [name,value] of Object.entries({'archive-reserve':'256','archive-retention':'3600','webhook-url':'http://127.0.0.1/hook','webhook-secret':'private-fixture-secret'}))root.querySelector(`[name=${name}]`).value=value;
  const save=nodes['[data-action=settings-save]'].onclick();assert.equal(JSON.parse(pending[0].options.body).webhook.secret,'private-fixture-secret');
  assert.equal(root.querySelector('[name=webhook-secret]').value,'');
  await respond({autostart:false,archive:{minimum_free_bytes:268435456,minimum_retention_seconds:3600},webhook:{enabled:false,url:'http://127.0.0.1/hook',secret_configured:true}});await save;
  assert.equal(nodes.pre.textContent.includes('private-fixture-secret'),false,'secret stays out of diagnostics');
  const again=nodes['[data-action=settings-save]'].onclick();assert.equal(Object.hasOwn(JSON.parse(pending[0].options.body).webhook,'secret'),false,'blank secret preserves existing value');
  await respond({autostart:false,archive:{minimum_free_bytes:268435456,minimum_retention_seconds:3600},webhook:{enabled:false,url:'http://127.0.0.1/hook',secret_configured:true}});await again;
  assert.equal(timers.size,1,'one bounded polling timer');
 }
 const api=context.window.oriMonitoring;
 const poll=api.request('/project-1/status',undefined,{quiet:true});
 const stop=nodes['[data-action=disarm]'].onclick();
 assert.equal(pending.length,2,'Disarm must be sent while a background read is pending');
 const returning=api.request('/project-1/manual/return',{token:'explicit-owner'});
 const background=pending.shift(),stopping=pending.shift();
 stopping.resolve({ok:true,text:async()=>JSON.stringify({data:{state:'ready'}})});await stop;await new Promise(setImmediate);
 assert.equal(pending[0].url,'/api/v1/monitoring-projects/project-1/manual/return','foreground writes retain explicit order');
 await respond({state:'returned'});await returning;
 background.resolve({ok:true,text:async()=>JSON.stringify({data:{project:{id:'project-1',active_session_id:'stale-run'}}})});await poll;
 assert.equal(emitted.some(e=>e.detail.sessionId==='stale-run'),false,'stale poll cannot override a newer operator action');
 console.log('Monitoring project scope, explicit arm and audio consent passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
