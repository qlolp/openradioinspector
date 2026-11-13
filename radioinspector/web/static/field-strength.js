/* Thin archived-field client: the core owns calibration and calculations. */
(() => {
  'use strict';
  const dom = Object.fromEntries(['run','clear','sweep','frequency','profile','status','result']
    .map(name => [name,document.getElementById(`field-${name}`)]));
  if (!dom.status) return;
  let session='', stream='', connected=true, generation=0, pending=null;
  const equal = (a,b) => a===b || (a && b && typeof a==='object' && typeof b==='object'
    && Object.keys(a).length===Object.keys(b).length && Object.keys(a).every(k=>equal(a[k],b[k])));
  const finite = v => typeof v==='number' && Number.isFinite(v);
  function controls() { dom.run.disabled=!connected || !session || !dom.sweep.value || Boolean(pending); }
  function clear() {
    generation++; pending?.abort();pending=null;dom.result.textContent='';dom.status.textContent='';controls();
  }
  async function boundedJSON(response) {
    const reader=response.body.getReader(), chunks=[];let size=0;
    try {
      for (;;) {
        const {done,value}=await reader.read();if(done)break;
        size+=value.byteLength;if(size>2000000)throw new Error('Ответ больше 2 МБ');chunks.push(value);
      }
    } catch(error) { await reader.cancel();throw error; }
    const bytes=new Uint8Array(size);let offset=0;
    for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}
    return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));
  }
  function validate(data,sent) {
    if (!data || data.methodology!=='field_strength_rms_v2' || data.unit!=='V/m'
      || !finite(data.value) || data.value<=0 || !finite(data.value_dbuv_per_m)
      || !(data.uncertainty_db===null || (finite(data.uncertainty_db) && data.uncertainty_db>=0))
      || data.source?.session_id!==session || data.source?.stream_id!==stream
      || data.source?.id!==sent.sweep_id || data.source?.frequency_hz!==sent.frequency_hz
      || !equal(data.calibration?.profile,sent.calibration)
      || !/^[a-f0-9]{64}$/.test(data.calibration?.sha256 || '')) {
      throw new Error('Ответ не соответствует источнику, запросу или калибровке');
    }
    return data;
  }
  async function analyze() {
    if(pending || !connected || !session)return;
    clear();const current=generation;
    try {
      const frequency=Number(dom.frequency.value);
      if(!dom.frequency.value.trim() || !Number.isSafeInteger(frequency) || frequency<0)
        throw new Error('Частота: целые неотрицательные Гц');
      const sent={sweep_id:dom.sweep.value,frequency_hz:frequency,calibration:JSON.parse(dom.profile.value)};
      const body=JSON.stringify(sent);
      if(new TextEncoder().encode(body).byteLength>65536)throw new Error('Запрос больше 64 КиБ');
      const controller=new AbortController();pending=controller;controls();dom.status.textContent='Вычисление…';
      const timer=window.setTimeout(()=>controller.abort(),6000);
      try {
        const response=await fetch(`/api/v1/sessions/${encodeURIComponent(session)}/field-strength-rms-v2`,
          {method:'POST',headers:{'Content-Type':'application/json'},body,signal:controller.signal});
        const envelope=await boundedJSON(response);
        if(current!==generation)return;
        if(!response.ok)throw new Error(envelope.error?.message || 'Расчёт недоступен');
        if(envelope.contract!=='openradioinspector-platform' || envelope.contract_version!==1)
          throw new Error('Неизвестный контракт');
        const data=validate(envelope.data,sent);
        dom.result.textContent=`${data.value} V/m (${data.value_dbuv_per_m} dBuV/m) · `
          +(data.uncertainty_db===null?'погрешность неизвестна':`погрешность ${data.uncertainty_db} dB`)
          +`\nСвип ${data.source.id} · ${data.source.frequency_hz} Гц · калибровка ${data.calibration.sha256}`;
        dom.status.textContent='Узкополосная RMS-эквивалентная оценка CW. Исходники не изменялись.';
      } finally {window.clearTimeout(timer);}
    } catch(error) {if(current===generation)dom.status.textContent=`Оценка недоступна: ${error.message}`;}
    finally {if(current===generation){pending=null;controls();}}
  }
  function update(next) {
    if(session!==(next.sessionId || '') || stream!==(next.streamId || ''))clear();
    session=next.sessionId || '';stream=next.streamId || '';
    const previous=dom.sweep.value;
    const choices=(next.sweeps || []).filter(s=>s.stream_id===stream && s.session_id===session);
    dom.sweep.replaceChildren(...choices.map(s=>{
      const option=document.createElement('option');option.value=s.id;option.textContent=s.id;return option;
    }));
    if(choices.some(s=>s.id===previous))dom.sweep.value=previous;
    if(dom.sweep.value!==previous)clear();
    controls();
  }
  function setConnected(value){connected=Boolean(value);if(!connected)clear();controls();}
  dom.run.addEventListener('click',analyze);dom.clear.addEventListener('click',clear);
  for(const name of ['sweep','frequency','profile'])dom[name].addEventListener('input',clear);
  window.addEventListener('pagehide',()=>setConnected(false));
  window.addEventListener('pageshow',()=>setConnected(true));
  window.addEventListener('offline',()=>setConnected(false));
  window.OpenRadioFieldStrength={update,setConnected};controls();
})();
