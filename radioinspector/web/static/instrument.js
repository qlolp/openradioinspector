(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  if (!$('context')) return;
  let scope=null,generation=0,lease=null,cap=null,busy=false,active=null,revision=null,previewing=false;
  let pointer=null,heartbeat=null,screenTimer=null,artifactOffset=0;
  const terminal=new Set(['succeeded','failed','cancelled','unknown']);
  const mount=(window.location.pathname||'').replace(/\/instrument(?:\.html)?$/,'');
  const prefix=s=>`${mount}/api/v1/sessions/${encodeURIComponent(s.session)}/streams/${encodeURIComponent(s.stream)}/instrument`;
  const same=(g,s)=>g===generation&&scope===s;
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const requestID=()=>globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const ownerKey=()=>{const bytes=new Uint8Array(16);globalThis.crypto.getRandomValues(bytes);return Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');};
  function resultText(data){
    if(data.steps)return data.route+'\n\n'+data.steps.map((x,i)=>`${i+1}. ${x}`).join('\n\n');
    if(data.directory_text!==undefined)return data.directory_text||'Список SD пуст; проверьте наличие карты.';
    if(data.measurement){const m=data.measurement;return `${m.name}: ${m.value??'не определено'} ${m.unit}\nПогрешность: ${m.uncertainty??'не определена'}\n`+(m.warnings||[]).join('\n');}
    if(data.artifact)return `Сохранено: ${data.artifact.filename}\nТип: ${data.artifact.kind}\nБайт: ${data.artifact.size_bytes}\nSHA-256: ${data.artifact.sha256}`;
    if(data.preview)return 'Получен экран 480×320. В архив не записан.';
    if(data.available!==undefined)return data.available?data.devices.map(x=>`${x.index}: ${x.name} (${x.input_channels} каналов)`).join('\n'):data.reason;
    if(data.completion)return 'Команда запуска подтверждена. Завершение и результат проверьте на живом экране.';
    const state=data.status||data;
    if(state.mode)return `Режим: ${state.mode}\nНастройки (запрошенные):\n`+Object.entries(state.settings||{}).map(([k,v])=>`${cap?.settings[k]?.label||k}: ${JSON.stringify(v)}`).join('\n');
    return data.device_response||JSON.stringify(data,null,2);
  }
  function controls(){
    const ready=Boolean(scope&&cap),owned=Boolean(lease);
    for(const id of ['capture','save-screen','artifacts-refresh','off'])$(id).disabled=!ready||busy;
    for(const id of ['resume','leave','home','release'])$(id).disabled=!ready||!owned||busy;
    $('acquire').disabled=!ready||owned||busy||!$('rf').checked;
    $('cancel').disabled=!active;$('screen').dataset.lab=String(owned);
    for(const form of ['settings','operation','keypad'])for(const el of $(form).querySelectorAll('button,input,select,textarea'))el.disabled=!ready||busy||(form==='keypad'&&!owned);
  }
  async function json(s,path,body){
    const response=await fetch(prefix(s)+path,{cache:'no-store',headers:{'X-Instrument-Client':s.key,...(body===undefined?{}:{'Content-Type':'application/json'})},...(body===undefined?{}:{method:'POST',body:JSON.stringify(body)})});
    const envelope=await response.json();
    if(!response.ok)throw Error(envelope?.error?.message||`HTTP ${response.status}`);
    if(envelope.contract!=='openradioinspector-platform'||envelope.contract_version!==1||!envelope.data)throw Error('Некорректный ответ ядра');
    return envelope.data;
  }
  function showState(state){
    revision=state.revision??revision;
    $('state').textContent=`${state.mode||'?'} · rev ${revision??'?'} · ${state.profile_verified?'профиль подтверждён':'профиль ещё не проверен'} · настройки: ${state.settings_source||'unknown'} · физическая приёмка не выполнена`;
    if(!state.lease_active&&lease){lease=null;stopHeartbeat();}
    controls();
  }
  function input(spec,value){
    let el;
    if(spec.type==='choice'||spec.type==='boolean'){
      el=document.createElement('select');
      const values=spec.type==='boolean'?[false,true]:spec.choices;
      for(const item of values){const option=document.createElement('option');option.value=JSON.stringify(item);option.textContent=String(item);el.appendChild(option);}
      el.value=JSON.stringify(value??values[0]);
    }else{
      el=document.createElement(spec.type==='multiline'?'textarea':'input');el.value=String(value??'');
      if(['integer','number'].includes(spec.type)){el.type='number';el.step=spec.type==='integer'?'1':'any';if(spec.min!==undefined)el.min=spec.min;if(spec.max!==undefined)el.max=spec.max;}
      else if(spec.type!=='multiline')el.type='text';
    }
    el.required=spec.type!=='multiline';el.dataset.kind=spec.type;return el;
  }
  function value(el){
    if(['choice','boolean'].includes(el.dataset.kind))return JSON.parse(el.value);
    if(['integer','number'].includes(el.dataset.kind)){const n=Number(el.value);if(!el.value.trim()||!Number.isFinite(n)||(el.dataset.kind==='integer'&&!Number.isSafeInteger(n)))throw Error('Требуется корректное число');return n;}
    if(el.dataset.kind.endsWith('_or_auto'))return el.value.trim()==='auto'?'auto':Number(el.value);
    return el.value;
  }
  function fields(){
    const setting=cap.settings[$('setting-name').value];
    const label=document.createElement('label');label.textContent=setting.label+(setting.lab?' · только lab':'');
    label.appendChild(input(setting,setting.choices?.[0]??(setting.type==='boolean'?false:setting.min??'auto')));$('setting-value').replaceChildren(label);
    const operation=cap.operations[$('operation-name').value];const list=[];
    for(const [name,spec]of Object.entries(operation.fields)){const row=document.createElement('label');row.textContent=spec.label;const el=input(spec,spec.default);el.dataset.name=name;row.appendChild(el);list.push(row);}
    $('operation-fields').replaceChildren(...list);controls();
  }
  function menus(){
    const term=$('menu-search').value.toLowerCase();
    const items=cap.menu.filter(x=>x.path.join(' / ').toLowerCase().includes(term));
    $('menu').replaceChildren(...items.map(item=>{const el=document.createElement('option');el.value=item.id;el.textContent=item.path.join(' → ');return el;}));
  }
  async function artifacts(){
    const s=scope,g=generation;if(!s)return;
    const data=await json(s,`/artifacts?limit=100&offset=${artifactOffset}`);if(!same(g,s))return;
    $('artifacts-prev').disabled=artifactOffset===0;$('artifacts-next').disabled=data.items.length<100;
    $('artifacts').replaceChildren(...data.items.map(item=>{const li=document.createElement('li'),a=document.createElement('a');a.textContent=`${item.kind} · ${item.filename} · ${item.size_bytes} байт`;a.href=prefix(s)+`/artifacts/${encodeURIComponent(item.id)}/content`;a.download=item.filename;li.appendChild(a);if(item.mime_type==='audio/wav'){const play=document.createElement('button');play.textContent='Прослушать';play.onclick=()=>{$('audio').src=a.href;$('audio').hidden=false;$('audio').play().catch(e=>{$('status').textContent=e.message;});};li.appendChild(play);}return li;}));
  }
  async function operation(op,parameters={},options={}){
    const s=scope,g=generation;if(!s)throw Error('Нет выбранного потока');
    const payload={operation:op,parameters,request_id:requestID()};if(lease)payload.lease_id=lease;
    if(options.revision&&revision!==null)payload.expected_revision=revision;
    const job=await json(s,'/operations',payload);if(!same(g,s))return null;
    if(!options.quiet)active=job.operation_id;
    controls();
    for(let count=0;count<700;count++){
      if(!same(g,s))return null;
      const result=await json(s,`/operations/${job.operation_id}`);
      if(!same(g,s))return null;
      if(result.context_current===false)throw Error('Соединение прибора изменилось; подключите панель заново');
      if(terminal.has(result.state)){
        if(active===job.operation_id)active=null;
        if(result.state!=='succeeded')throw Error(`${result.state}: ${result.error||'операция не подтверждена'}`);
        const data=result.result||{};
        if(data.lease_id){lease=data.lease_id;startHeartbeat();}
        if(data.status)showState(data.status);else if(data.mode)showState(data);
        if(data.preview)$('screen').src=`data:image/png;base64,${data.preview.data_base64}`;
        if(data.artifact){await artifacts();if(same(g,s)&&data.artifact.mime_type==='audio/wav'){$('audio').src=prefix(s)+`/artifacts/${data.artifact.id}/content`;$('audio').hidden=false;}}
        if(!options.quiet){$('result').textContent=resultText(data);$('result-details').textContent=JSON.stringify({...data,...(data.lease_id?{lease_id:'(аренда этой панели)'}:{})},null,2);}
        controls();return data;
      }
      await sleep(150);
    }
    throw Error('Истекло ожидание. Проверьте операции ядра; изменение автоматически не повторяется.');
  }
  async function action(fn){if(busy)return;busy=true;controls();try{await fn();$('status').textContent='Операция завершена.';}catch(e){$('status').textContent=e.message;}finally{busy=false;controls();}}
  function stopHeartbeat(){if(heartbeat)clearInterval(heartbeat);heartbeat=null;}
  function startHeartbeat(){stopHeartbeat();heartbeat=setInterval(()=>{if(lease)operation('lease.renew',{}, {quiet:true}).catch(e=>{lease=null;stopHeartbeat();$('status').textContent=e.message;controls();});},8000);}
  function retire(){
    const old=scope,token=lease;generation++;lease=null;scope=null;cap=null;active=null;busy=false;pointer=null;revision=null;stopHeartbeat();
    if(old&&token)fetch(prefix(old)+'/operations',{method:'POST',headers:{'Content-Type':'application/json','X-Instrument-Client':old.key},body:JSON.stringify({operation:'lease.release',lease_id:token,parameters:{},request_id:requestID()}),keepalive:true}).catch(()=>{});
    $('screen').removeAttribute('src');$('audio').pause();$('audio').removeAttribute('src');$('audio').hidden=true;$('result').textContent='';$('result-details').textContent='';$('artifacts').replaceChildren();controls();
  }
  async function connect(event){event?.preventDefault();retire();artifactOffset=0;scope={session:$('session').value.trim(),stream:$('stream').value.trim(),key:ownerKey()};const s=scope,g=generation;
    try{const data=await json(s,'/capabilities');if(!same(g,s))return;cap=data.capabilities;
      for(const [id,values]of [['setting-name',cap.settings],['operation-name',cap.operations]])$(id).replaceChildren(...Object.entries(values).map(([key,spec])=>{const el=document.createElement('option');el.value=key;el.textContent=spec.label+(spec.lab?' · lab':'');return el;}));
      fields();menus();showState(data.status);await operation('inspect',{}, {quiet:true});await artifacts();if(same(g,s))$('status').textContent='Панель подключена. Аппаратная проверка не выполнена.';
    }catch(e){if(same(g,s))$('status').textContent=e.message;}controls();
  }
  $('context').addEventListener('submit',connect);
  $('rf').onchange=controls;$('setting-name').onchange=fields;$('operation-name').onchange=fields;
  $('menu-search').oninput=()=>{if(cap)menus();};$('menu').onchange=()=>{$('menu-path').textContent=cap.menu.find(x=>x.id===$('menu').value)?.path.join(' → ')||'';};
  $('settings').onsubmit=e=>{e.preventDefault();action(()=>operation('setting',{name:$('setting-name').value,value:value($('setting-value').querySelector('input,select,textarea'))},{revision:true}));};
  $('operation').onsubmit=e=>{e.preventDefault();action(()=>{const parameters={};for(const el of $('operation-fields').querySelectorAll('input,select,textarea'))parameters[el.dataset.name]=value(el);return operation($('operation-name').value,parameters);});};
  for(const [id,op,params]of [['acquire','lease.acquire',{acknowledge_rf:true}],['resume','monitor.resume',{}],['leave','lease.release',{}],['off','output.off',{}],['capture','capture',{save:false}],['save-screen','capture',{save:true}],['home','menu.home',{}],['release','release',{}]])$(id).onclick=()=>action(()=>operation(op,params));
  $('keypad').onsubmit=e=>{e.preventDefault();action(()=>operation('keypad',{text:$('keypad-text').value}));};
  $('artifacts-refresh').onclick=()=>action(artifacts);
  $('artifacts-prev').onclick=()=>action(()=>{artifactOffset=Math.max(0,artifactOffset-100);return artifacts();});
  $('artifacts-next').onclick=()=>action(()=>{artifactOffset+=100;return artifacts();});
  $('cancel').onclick=()=>{const s=scope,id=active;if(s&&id)json(s,`/operations/${id}/cancel`,{}).catch(e=>{$('status').textContent=e.message;});};
  $('screen').addEventListener('pointerdown',e=>{if(!lease||busy)return;e.preventDefault();const rect=$('screen').getBoundingClientRect();const x=Math.floor((e.clientX-rect.left)*480/rect.width),y=Math.floor((e.clientY-rect.top)*320/rect.height);if(x<0||x>=480||y<0||y>=320)return;
    const s=scope,g=generation;const held={released:false};pointer=held;$('screen').setPointerCapture?.(e.pointerId);
    held.g=g;held.s=s;held.promise=operation('touch',{x,y}).catch(err=>{if(same(g,s))$('status').textContent=err.message;});
  });
  for(const event of ['pointerup','pointercancel','lostpointercapture'])$('screen').addEventListener(event,()=>{const held=pointer;if(!held)return;pointer=null;held.released=true;held.promise?.then(()=>{if(same(held.g,held.s)&&lease)return operation('release',{});}).catch(e=>{$('status').textContent=e.message;});});
  screenTimer=setInterval(()=>{if(scope&&cap&&$('auto').checked&&!busy&&!previewing){previewing=true;operation('capture',{save:false},{quiet:true}).catch(e=>{$('status').textContent=e.message;}).finally(()=>{previewing=false;});}},500);
  window.addEventListener('pagehide',()=>{retire();clearInterval(screenTimer);});
  window.OpenRadioTinySA={connect,retire,operation,get scope(){return scope;}};
  const query=new URLSearchParams(window.location.search);$('session').value=query.get('session_id')||'';$('stream').value=query.get('stream_id')||'';
  if($('session').value&&$('stream').value)connect();else controls();
})();
