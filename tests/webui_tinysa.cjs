const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(process.argv[2],'utf8');
function setup(pathname=''){
  const dom={},events={},calls=[],jobs=new Map(),intervals=new Map();let timer=0,defer=null;
  function element(tag,id=''){
    const el={tagName:tag.toUpperCase(),id,value:'',textContent:'',children:[],dataset:{},disabled:false,listeners:{},checked:false,
      addEventListener(name,fn){this.listeners[name]=fn;},
      appendChild(child){this.children.push(child);if(tag==='select'&&this.children.length===1)this.value=child.value;return child;},
      replaceChildren(...children){this.children=[];this.value='';for(const child of children)this.appendChild(child);},
      querySelectorAll(selector){return this.children.flatMap(child=>[...(selector.split(',').includes(child.tagName.toLowerCase())?[child]:[]),...child.querySelectorAll(selector)]);},
      querySelector(selector){return this.querySelectorAll(selector)[0];},removeAttribute(name){delete this[name];},
      getBoundingClientRect(){return {left:0,top:0,width:240,height:160};},pause(){},play(){return Promise.resolve();},setPointerCapture(){},
    };
    Object.defineProperty(el,'innerHTML',{set(){throw Error('Unsafe HTML');}});return el;
  }
  const forms=new Set(['context','settings','operation','keypad']);
  const selects=new Set(['setting-name','operation-name','menu']);
  const document={getElementById(id){return dom[id]??=(element(forms.has(id)?'form':selects.has(id)?'select':'div',id));},createElement:tag=>element(tag)};
  const cap={settings:{rbw_hz:{label:'RBW',type:'choice',choices:[200,10000]}},operations:{inspect:{label:'Inspect',fields:{}}},menu:[{id:'zero',path:['analyzer','FREQUENCY','ZERO SPAN']}]};
  const window={location:{search:'',pathname},addEventListener(name,fn){events[name]=fn;}};
  async function fetch(url,options={}){
    const body=options.body?JSON.parse(options.body):null;calls.push({url,body});
    let data;
    if(url.endsWith('/capabilities'))data={capabilities:cap,status:{mode:'monitoring',revision:0,lease_active:false}};
    else if(url.includes('/artifacts?'))data={items:[]};
    else if(url.endsWith('/operations')){
      const id=String(jobs.size+1);jobs.set(id,body);data={operation_id:id};
    }else if(url.endsWith('/cancel'))data={state:'cancelled'};
    else {
      const input=jobs.get(url.split('/').pop());let result={};
      if(input.operation==='lease.acquire')result={lease_id:'SECRET',status:{mode:'lab',revision:1,lease_active:true}};
      if(input.operation==='capture')result={preview:{data_base64:'NEW-SCREEN'}};
      data={state:'succeeded',result,context_current:true};
      if(defer){const next=defer;defer=null;return new Promise(resolve=>next(()=>resolve({ok:true,json:async()=>({contract:'openradioinspector-platform',contract_version:1,data})})));}
    }
    return {ok:true,json:async()=>({contract:'openradioinspector-platform',contract_version:1,data})};
  }
  const context=vm.createContext({document,window,fetch,URLSearchParams,console,Date,Math,JSON,Promise,Set,Uint8Array,crypto:{getRandomValues(bytes){bytes.fill(7);return bytes;}},
    setTimeout:fn=>{fn();return 1;},setInterval:fn=>{intervals.set(++timer,fn);return timer;},clearInterval:id=>intervals.delete(id)});
  vm.runInContext(source,context);
  async function connect(session='s',stream='t'){dom.session.value=session;dom.stream.value=stream;await window.OpenRadioTinySA.connect();}
  return {dom,window,calls,events,intervals,connect,defer(fn){defer=fn;}};
}
(async()=>{
  let f=setup();await f.connect();
  assert.match(f.dom.status.textContent,/подключена/);
  await f.window.OpenRadioTinySA.operation('lease.acquire',{acknowledge_rf:true});
  assert(!f.dom.result.textContent.includes('SECRET'));
  f.dom.screen.listeners.pointerdown({preventDefault(){},clientX:120,clientY:80,pointerId:1});
  f.dom.screen.listeners.pointerup();
  for(let i=0;i<60;i++)await Promise.resolve();
  const sent=f.calls.filter(x=>x.body).map(x=>x.body);
  const touch=sent.find(x=>x.operation==='touch');assert.equal(touch.parameters.x,240);assert.equal(touch.parameters.y,160);
  assert.equal(sent.filter(x=>x.operation==='release').length,1,'release once even when pointer-up precedes ACK');
  let unblock;f.defer(resolve=>{unblock=resolve;});
  const pending=f.window.OpenRadioTinySA.operation('capture');
  for(let i=0;i<8;i++)await Promise.resolve();
  assert(unblock);f.window.OpenRadioTinySA.retire();unblock();await pending;
  assert(!f.dom.screen.src,'retired context cannot display stale screen');
  assert(f.calls.some(x=>x.body?.operation==='lease.release'&&x.body.lease_id==='SECRET'));
  assert([...f.intervals.values()].length===1,'only screen timer remains after retire');
  f=setup('/prefix/instrument');await f.connect('other','other-stream');
  assert(f.calls.every(x=>x.url.startsWith('/prefix/api/v1/')),'native server path prefix preserved');
  console.log('tinySA WebUI behavior checks completed');
})().catch(error=>{console.error(error);process.exitCode=1;});
