const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
function element(){ return {value:'',textContent:'',dataset:{},selectedOptions:[],options:[],children:[],listeners:{},checked:false,
 addEventListener(e,f){(this.listeners[e] ||= []).push(f);},removeAttribute(){},pause(){},play(){return Promise.resolve();},
 width:1000,height:240,getContext(){return new Proxy({}, {get:()=>()=>{}});},append(...x){this.children.push(...x);},replaceChildren(...x){this.children=x;this.options=x;if(!x.some(v=>v.value===this.value))this.value=x[0]?.value||'';}}; }
const nodes={}, root=element(), pending=[];
root.querySelector=s=>nodes[s]||(nodes[s]=element());
const context=vm.createContext({document:{getElementById:()=>root,body:{dataset:{}},createElement:()=>element()},
 window:{},URLSearchParams,Option:function(t,v){return Object.assign(element(),{textContent:t,value:v});},localStorage:{getItem:()=>null,setItem(){}},
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
 console.log('Monitoring project scope, explicit arm and audio consent passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
