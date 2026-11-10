/* Read-only client polling; guard and webhook ownership remain in the core. */
(() => {
  'use strict';
  const api=window.oriMonitoring;if(!api)return;
  const root=api.root,node=name=>root.querySelector(`[name=${name}]`),status=role=>root.querySelector(`[data-role=${role}]`);
  let revision=0,cursor=null,pageAfter='',timer=null,closed=false,seen=null,audio=null,ticks=0;
  const bind=(name,fn)=>{root.querySelector(`[data-action=${name}]`).onclick=fn;};
  async function call(resource,body,query){
    const project=api.selected(),current=revision;if(!project)return;
    const data=await api.request('/'+encodeURIComponent(project)+'/'+resource+(query?'?'+new URLSearchParams(query):''),body,{quiet:true});
    return current===revision && project===api.selected()?data:undefined;
  }
  function settings(data){
    node('autostart').checked=data.autostart===true;node('archive-reserve').value=data.archive.minimum_free_bytes/1048576;
    node('archive-retention').value=data.archive.minimum_retention_seconds;node('webhook-enabled').checked=data.webhook.enabled===true;
    node('webhook-url').value=data.webhook.url;node('webhook-secret').value='';
    status('settings-status').textContent='Настройки загружены · секрет '+(data.webhook.secret_configured?'сохранён':'не настроен');
  }
  bind('settings-load',async()=>{const data=await call('settings');if(data)settings(data);else status('settings-status').textContent='Настройки недоступны';});
  bind('settings-save',async()=>{
    const webhook={enabled:node('webhook-enabled').checked,url:node('webhook-url').value};
    const secret=node('webhook-secret').value;if(secret)webhook.secret=secret;node('webhook-secret').value='';
    const body={autostart:node('autostart').checked,archive:{minimum_free_bytes:Number(node('archive-reserve').value)*1048576,minimum_retention_seconds:Number(node('archive-retention').value)},webhook};
    const data=await call('settings',body);
    // Never render raw settings responses or reflected errors in diagnostics.
    if(data){settings(data);status('settings-status').textContent='Настройки сохранены';}
    else status('settings-status').textContent='Не удалось сохранить настройки. Проверьте адрес, секрет и бюджет архива.';
  });
  const detectorFields={margin_db:'detector-margin',impulse_db:'detector-impulse',hysteresis_db:'detector-hysteresis',confirmations:'detector-confirmations',window_sweeps:'detector-window',dwell_seconds:'detector-dwell'};
  const fillDetector=data=>{for(const [key,field] of Object.entries(detectorFields))node(field).value=data[key];};
  bind('detector-balanced',()=>fillDetector({margin_db:6,impulse_db:12,hysteresis_db:3,confirmations:3,window_sweeps:5,dwell_seconds:10}));
  bind('detector-load',async()=>{const data=await call('settings');if(data)fillDetector(data.detector);});
  bind('detector-save',async()=>{const detector={};for(const [key,field] of Object.entries(detectorFields))detector[key]=Number(node(field).value);
    if(!Number.isInteger(detector.confirmations)||!Number.isInteger(detector.window_sweeps)){status('settings-status').textContent='Подтверждения и окно должны быть целыми';return;}
    const data=await call('settings',{detector});status('settings-status').textContent=data?'Версия параметров детектора сохранена':'Снимите охрану и проверьте параметры';});
  bind('preparation-load',async()=>{const project=await call('');if(!project)return;const preparation=project.preparation||{};
    node('antenna-notes').value=preparation.antenna_notes||'';node('receive-path-confirmed').checked=preparation.receive_path_confirmed===true;node('location-confirmed').checked=preparation.location_confirmed===true;
    status('preparation-status').textContent=project.profile?.analyzer?.settings?.adapter==='tinysa-sim'?'Явный симулятор: физические подтверждения не требуются':'Подтвердите реальную антенну, тракт и место';});
  bind('preparation-save',async()=>{const data=await call('preparation',{antenna_notes:node('antenna-notes').value,receive_path_confirmed:node('receive-path-confirmed').checked,location_confirmed:node('location-confirmed').checked});
    status('preparation-status').textContent=data?'Декларации оператора сохранены':'Остановите сбор/охрану и проверьте декларации';});
  for(const action of ['start','stop'])bind('preparation-'+action,async()=>{const data=await call('preparation/'+action,{});status('preparation-status').textContent=data?(action==='start'?'Приём начат; проверьте прогресс и качество':'Сбор остановлен'):'Проверьте приборы, архив и подтверждения';});
  bind('preparation-progress',async()=>{const data=await call('preflight');if(!data)return;const progress=data.baseline_collection;
    status('preparation-status').textContent=`${progress.sweeps}/${progress.minimum_sweeps} свипов · ${progress.elapsed_seconds.toFixed(1)}/${progress.minimum_seconds} с · ${progress.ready?'Выборка готова, оцените обычность обстановки':'Накопление не завершено'} · аппаратная приёмка: ${data.hardware_validation}`;});
  const mib=value=>value==null?'неизвестно':(value/1048576).toFixed(1)+' МиБ';
  async function storage(){const data=await call('storage');if(!data)return;
    status('storage-status').textContent=`${data.ready?'Архив готов':'АРХИВ НЕ ГОТОВ'} · свободно ${mib(data.filesystem_free_bytes)} · резерв ${mib(data.minimum_free_bytes)} · доступно ${mib(data.available_bytes)} · поток ${mib(data.bytes_per_second)}/с · прогноз ${data.estimated_retention_seconds==null?'неизвестен':(data.estimated_retention_seconds/3600).toFixed(1)+' ч'} · требуется ${(data.required_retention_seconds/3600).toFixed(1)} ч · автоудаление выключено`;}
  bind('storage-refresh',storage);
  function beep(){if(!node('alarm-sound').checked || !audio)return;const oscillator=audio.createOscillator(),gain=audio.createGain();oscillator.frequency.value=880;gain.gain.value=.07;oscillator.connect(gain);gain.connect(audio.destination);oscillator.start();oscillator.stop(audio.currentTime+.15);}
  node('alarm-sound').addEventListener('change',()=>{if(node('alarm-sound').checked){const Constructor=window.AudioContext||window.webkitAudioContext;if(Constructor){audio ||= new Constructor();audio.resume();}}});
  async function alerts(next=false,quiet=false){
    const data=await call('alerts',undefined,{limit:'50',...(next&&cursor?{after:cursor}:{})});if(!data)return;
    const rows=data.items||[];
    if(!next){if(seen && rows.some(x=>!seen.has(x.id)&&!x.acknowledged_at))beep();seen=new Set(rows.map(x=>x.id));}
    if(quiet && pageAfter)return;
    pageAfter=next?(cursor||''):'';
    cursor=data.next_cursor;root.querySelector('[data-action=alerts-next]').disabled=!cursor;
    const list=status('alerts');list.replaceChildren();
    for(const item of rows){const row=document.createElement('div'),text=document.createElement('span'),ack=document.createElement('button');
      text.textContent=`${item.created_at} · ${item.severity} · ${item.message} · ${item.acknowledged_at?'подтверждена':'не подтверждена'} · доставка: ${item.delivery.state}, попыток ${item.delivery.attempts}${item.delivery.last_error?' · '+item.delivery.last_error:''}${item.delivery.next_attempt_at?' · повтор '+item.delivery.next_attempt_at:''}`;
      ack.textContent='Подтвердить';ack.disabled=!!item.acknowledged_at;ack.onclick=async()=>{if(await call('alerts/'+encodeURIComponent(item.id)+'/ack',{}))await alerts();};row.append(text,ack);list.append(row);
    }
    status('alerts-status').textContent='Тревог на странице: '+rows.length+' · выбор экрана не останавливает охрану';
  }
  bind('alerts-refresh',()=>alerts());bind('alerts-next',()=>alerts(true));
  bind('event-review',async()=>{const event=node('search-event').value;if(!event){status('alerts-status').textContent='Выберите событие';return;}
    if(await call('events/'+encodeURIComponent(event)+'/review',{disposition:node('event-disposition').value,comment:node('event-comment').value}))status('alerts-status').textContent='Вердикт записан отдельно от технического результата';});
  node('project').addEventListener('change',()=>{revision++;seen=null;cursor=null;pageAfter='';node('webhook-secret').value='';status('alerts').replaceChildren();});
  async function poll(){if(closed)return;try{if(api.selected()){await alerts(false,true);const snapshot=await call('status');if(snapshot?.project)window.oriMonitoringResearch?.coverage(snapshot.project.coverage);if(root.dataset.searchActive==='true'||root.dataset.searchCoverage==='restoring'){const search=await call('search/status');if(search)window.oriMonitoringResearch?.renderSearch(search);}if(ticks++%6===0)await storage();}}finally{if(!closed)timer=window.setTimeout(poll,5000);}}
  window.addEventListener('beforeunload',()=>{closed=true;revision++;window.clearTimeout(timer);node('webhook-secret').value='';if(audio)audio.close();});
  timer=window.setTimeout(poll,5000);
})();
