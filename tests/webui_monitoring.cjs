const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
function element(){ return {value:'',textContent:'',dataset:{},selectedOptions:[],options:[],children:[],listeners:{},checked:false,
 addEventListener(e,f){(this.listeners[e] ||= []).push(f);},dispatchEvent(e){for(const fn of this.listeners[e.type]||[])fn(e);},removeAttribute(){},pause(){},play(){return Promise.resolve();},
 width:1000,height:240,getContext(){return new Proxy({}, {get:()=>()=>{}});},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;this.options=x;if(!x.some(v=>v.value===this.value))this.value=x[0]?.value||'';}}; }
function workspace(storage={getItem:()=>null,setItem(){}}) {
const nodes={}, root=element(), pending=[], emitted=[], timers=new Map();let timerId=0;
root.querySelector=s=>nodes[s]||(nodes[s]=element());
const context=vm.createContext({document:{getElementById:()=>root,body:{dataset:{}},createElement:()=>element()},
 window:{dispatchEvent:e=>emitted.push(e),addEventListener(){},setTimeout(fn){const id=++timerId;timers.set(id,fn);return id;},clearTimeout(id){timers.delete(id);}},AbortController,Event,CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},URLSearchParams,Option:function(t,v){return Object.assign(element(),{textContent:t,value:v});},localStorage:storage,
 fetch:(url,options)=>new Promise(resolve=>pending.push({url,options,resolve})),FormData:class{get(name){return {name:'Created project',site:'Fixture site'}[name];}},console});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
async function respond(data){pending.shift().resolve({ok:true,text:async()=>JSON.stringify({data})});await new Promise(setImmediate);}
return {nodes,root,pending,emitted,timers,context,respond};
}
const {nodes,root,pending,emitted,timers,context,respond}=workspace();
(async()=>{
 const blocked=workspace({getItem(){throw new Error('SecurityError');},setItem(){throw new Error('SecurityError');}});
 assert.equal(blocked.context.document.body.dataset.workspaceMode,'research','blocked storage defaults to research');
 await blocked.respond({items:[]});
 assert.equal(typeof blocked.nodes['[data-action=arm]'].onclick,'function','blocked storage preserves guard controls');
 blocked.nodes['[name=workspace]'].listeners.change[0]({target:{value:'monitoring'}});
 assert.equal(blocked.context.document.body.dataset.workspaceMode,'monitoring','blocked writes still switch workspace');
 await respond({items:[{id:'project-1',name:'Room',site:'Office',state:'ready'}]});
 const projects=nodes['[name=project]']; assert.equal(projects.value,'project-1');
 const pages=workspace();
 const firstPage=Array.from({length:200},(_,i)=>({id:'p-'+String(i).padStart(3,'0'),name:'Project '+i,site:'Synthetic',state:'ready'}));
 await pages.respond({items:firstPage,next_cursor:'p-199'});
 assert.equal(pages.root.querySelector('[data-action=projects-previous]').disabled,true,'first page has no predecessor');
 assert.equal(pages.nodes['[data-action=projects-next]'].disabled,false,'project cursor exposes next page');
 const nextPage=pages.nodes['[data-action=projects-next]'].onclick();
 assert.match(pages.pending[0].url,/limit=200/);assert.match(pages.pending[0].url,/after=p-199/);
 await pages.respond({items:[{id:'p-200',name:'Last project',site:'Synthetic',state:'ready'}],next_cursor:'p-200'});await nextPage;
 assert.equal(pages.nodes['[name=project]'].options.some(x=>x.value==='p-200'),true,'projects beyond first 200 are reachable');
 assert.ok(pages.nodes['[name=project]'].options.length<=201,'project pages replace bounded contents');
 assert.equal(pages.nodes['[data-action=projects-next]'].disabled,true);
 const previousPage=pages.nodes['[data-action=projects-previous]'].onclick();
 assert.equal(new URL(pages.pending[0].url,'http://fixture').searchParams.has('after'),false);
 await pages.respond({items:firstPage,next_cursor:'p-199'});await previousPage;
 const created=pages.nodes.form.onsubmit({preventDefault(){},target:{}});
 assert.equal(pages.pending[0].options.method,'POST');
 await pages.respond({id:'z-created',name:'Created project',site:'Fixture site',state:'draft'});
 await pages.respond({items:firstPage,next_cursor:'p-199'});await created;
 assert.equal(pages.nodes['[name=project]'].value,'z-created','new project outside page remains selected and reachable');
 assert.ok(pages.nodes['[name=project]'].options.length<=201,'only the current selection may accompany a bounded page');
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
  for(const [role,coverage] of [['receiver','narrow'],['analyzer','paused']]) {
   root.querySelector('[name=manual-role]').value=role;
   if(role==='analyzer') {
    const acquireAnalyzer=nodes['[data-action=manual-acquire]'].onclick();
    await respond({token:'analyzer-token',role:'analyzer',expires_seconds:30});await acquireAnalyzer;
   }
   context.window.oriMonitoringResearch.coverage('paused');
   const returned=nodes['[data-action=manual-return]'].onclick();
   await respond({state:'returned'});
   assert.equal(root.dataset.searchCoverage,'paused','return response cannot establish wide coverage');
   assert.equal(pending[0]?.url,'/api/v1/monitoring-projects/project-1/search/status','return refreshes authoritative search coverage');
   await respond({active:coverage==='narrow',coverage,series:[],marks:[]});await returned;
   assert.equal(root.dataset.searchCoverage,coverage,'server remains authoritative after '+role+' return');
  }
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
  assert.equal(nodes['[data-action=settings-save]'].disabled,true,'settings save waits for current project settings');
  const loaded=nodes['[data-action=settings-load]'].onclick();
  await respond({autostart:true,archive:{minimum_free_bytes:536870912,minimum_retention_seconds:7200},webhook:{enabled:true,url:'http://127.0.0.1/project-a',secret_configured:true}});await loaded;
  assert.equal(nodes['[data-action=settings-save]'].disabled,false);
  for(const [name,value] of Object.entries({'archive-reserve':'256','archive-retention':'3600','webhook-url':'http://127.0.0.1/hook','webhook-secret':'private-fixture-secret'}))root.querySelector(`[name=${name}]`).value=value;
  const save=nodes['[data-action=settings-save]'].onclick();assert.equal(JSON.parse(pending[0].options.body).webhook.secret,'private-fixture-secret');
  assert.equal(root.querySelector('[name=webhook-secret]').value,'');
  await respond({autostart:false,archive:{minimum_free_bytes:268435456,minimum_retention_seconds:3600},webhook:{enabled:false,url:'http://127.0.0.1/hook',secret_configured:true}});await save;
  assert.equal(nodes.pre.textContent.includes('private-fixture-secret'),false,'secret stays out of diagnostics');
  const again=nodes['[data-action=settings-save]'].onclick();assert.equal(Object.hasOwn(JSON.parse(pending[0].options.body).webhook,'secret'),false,'blank secret preserves existing value');
  await respond({autostart:false,archive:{minimum_free_bytes:268435456,minimum_retention_seconds:3600},webhook:{enabled:false,url:'http://127.0.0.1/hook',secret_configured:true}});await again;
  assert.equal(timers.size,1,'one bounded polling timer');
  const staleLoad=nodes['[data-action=settings-load]'].onclick();
  projects.value='project-2';for(const listener of projects.listeners.change)listener();
  assert.equal(nodes['[data-action=settings-save]'].disabled,true,'switching projects blocks stale settings submission');
  for(const name of ['webhook-url','webhook-secret','archive-reserve','archive-retention'])assert.equal(root.querySelector(`[name=${name}]`).value,'','switching projects clears '+name);
  for(const name of ['autostart','webhook-enabled'])assert.equal(root.querySelector(`[name=${name}]`).checked,false);
  await nodes['[data-action=settings-save]'].onclick();assert.equal(pending.length,1,'save cannot post project A settings to project B');
  await respond({autostart:true,archive:{minimum_free_bytes:536870912,minimum_retention_seconds:7200},webhook:{enabled:true,url:'http://127.0.0.1/project-a',secret_configured:true}});await staleLoad;
  assert.equal(nodes['[data-action=settings-save]'].disabled,true,'stale load cannot enable settings for project B');
  assert.equal(root.querySelector('[name=webhook-url]').value,'');
  const loadB=nodes['[data-action=settings-load]'].onclick();
  await respond({autostart:false,archive:{minimum_free_bytes:134217728,minimum_retention_seconds:1800},webhook:{enabled:false,url:'http://127.0.0.1/project-b',secret_configured:true}});await loadB;
  root.querySelector('[name=webhook-secret]').value='project-b-secret';
  const saveB=nodes['[data-action=settings-save]'].onclick();
  assert.equal(pending[0].url,'/api/v1/monitoring-projects/project-2/settings');
  assert.deepEqual(JSON.parse(pending[0].options.body),{autostart:false,archive:{minimum_free_bytes:134217728,minimum_retention_seconds:1800},webhook:{enabled:false,url:'http://127.0.0.1/project-b',secret:'project-b-secret'}});
  await respond({autostart:false,archive:{minimum_free_bytes:134217728,minimum_retention_seconds:1800},webhook:{enabled:false,url:'http://127.0.0.1/project-b',secret_configured:true}});await saveB;
  projects.value='project-1';for(const listener of projects.listeners.change)listener();
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
