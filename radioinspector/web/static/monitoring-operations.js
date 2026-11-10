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
  async function poll(){if(closed)return;try{if(api.selected()){await alerts(false,true);await call('status');if(ticks++%6===0)await storage();}}finally{if(!closed)timer=window.setTimeout(poll,5000);}}
  window.addEventListener('beforeunload',()=>{closed=true;revision++;window.clearTimeout(timer);node('webhook-secret').value='';if(audio)audio.close();});
  timer=window.setTimeout(poll,5000);
})();
