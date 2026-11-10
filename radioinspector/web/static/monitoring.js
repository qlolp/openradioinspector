/* Project state belongs to the core; workspace selection is local presentation. */
(() => {
  'use strict';
  const root = document.getElementById('monitoring-workspace');
  if (!root) return;
  const projects = root.querySelector('[name=project]'), info = root.querySelector('pre');
  const previousPage=root.querySelector('[data-action=projects-previous]'),nextPage=root.querySelector('[data-action=projects-next]');
  let pageAfter='',pageHistory=[],nextCursor=null,pageBusy=false;
  let generation=0,activeRequests=0,foregroundCount=0,foregroundTail=Promise.resolve(),writeEpoch=0,followedSession='';
  function request(path='',body,options={}) {
    const current=generation;
    const invoke=()=>current===generation?performRequest(path,body,options):Promise.resolve();
    if(options.quiet && body===undefined){
      if(activeRequests || foregroundCount)return Promise.resolve();
      return invoke();
    }
    if(foregroundCount>=32){info.textContent='Очередь действий клиента заполнена; дождитесь ответа';return Promise.resolve();}
    const result=foregroundCount?foregroundTail.then(invoke):invoke();
    foregroundCount++;
    const tracked=result.finally(()=>{foregroundCount--;});
    foregroundTail=tracked.catch(()=>{});
    return tracked;
  }
  async function performRequest(path,body,options) {
    activeRequests++;const current=generation;
    if(body!==undefined)writeEpoch++;
    const epoch=writeEpoch,controller=new AbortController();
    const timeout=window.setTimeout(()=>controller.abort(),path.endsWith('/disarm')?45000:10000);
    try {
      const response = await fetch('/api/v1/monitoring-projects' + path, body === undefined ? {signal:controller.signal} : {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),signal:controller.signal
      });
      const raw = await response.text();
      if (raw.length > 2000000) throw Error('Ответ проекта превышает предел');
      const envelope = JSON.parse(raw);
      if (!response.ok) throw Error(envelope.error?.message || `HTTP ${response.status}`);
      if (current !== generation || options.quiet && body===undefined && epoch!==writeEpoch) return;
      const data = envelope.data;
      const project = data.project || (data.active_session_id ? data : null);
      if(project){
        const states={armed:'на охране',degraded:'неполная связка приборов',error:'ошибка',collecting:'сбор эталона',ready:'готов, охрана выключена',draft:'подготовка',interrupted:'прерван, требуется подготовка'};
        const manual=(data.manual||[]).map(x=>`${x.role}: ручное управление${x.expired?' (аренда истекла)':''}`).join(' · ');
        root.querySelector('[data-role=guard-live-status]').textContent=`Охрана: ${states[project.state]||project.state} · ${manual} ${data.error||project.last_error||''}`;
        if(window.oriMonitoringResearch)window.oriMonitoringResearch.coverage(project.coverage||'stopped');
      }
      const scope = project ? `${project.id}|${project.active_session_id}` : '';
      if (scope && scope !== followedSession) {
        followedSession = scope;
        window.dispatchEvent(new CustomEvent('ori-monitoring-session', {detail:{sessionId:project.active_session_id}}));
      }
      if (path.endsWith('/events') && Array.isArray(data.items)) renderEvents(data.items);
      if (path === '/audio-inputs') {
        const select = root.querySelector('[name=audio-input]');
        select.replaceChildren(new Option('Не выбран', ''), ...(data.devices || []).map(d => {
          const option = new Option(`${d.index}: ${d.name}`, d.index); option.dataset.deviceName = d.name; return option;
        }));
      }
      if (!options.quiet) info.textContent = JSON.stringify(data, null, 2);
      return data;
    } catch (error) { if (current === generation && !options.quiet) info.textContent = error.message; }
    finally { activeRequests--;window.clearTimeout(timeout); }
  }
  function pager(){
    previousPage.disabled=pageBusy||!pageHistory.length;nextPage.disabled=pageBusy||!nextCursor;
    root.querySelector('[data-action=refresh]').disabled=pageBusy;
  }
  async function loadProjects(after='',history=[],selected){
    if(pageBusy)return;
    pageBusy=true;pager();
    try {
      const data=await request('?'+new URLSearchParams({limit:'200',...(after?{after}:{})}));
      if(!data || !Array.isArray(data.items))return;
      const previous=selected?.id||projects.value;
      const retained=selected?new Option(`${selected.name} · ${selected.site} · ${selected.state}`,selected.id):[...projects.options].find(o=>o.value===previous);
      const items=data.items.slice(0,200),pinned=retained&&!items.some(p=>p.id===previous);
      const before=projects.value;
      projects.replaceChildren(...(pinned?[retained]:[]),...items.map(p=>new Option(`${p.name} · ${p.site} · ${p.state}`,p.id)));
      if([...projects.options].some(o=>o.value===previous))projects.value=previous;
      pageAfter=after;pageHistory=history;nextCursor=items.length===200?(data.next_cursor||null):null;
      root.querySelector('[data-role=projects-page]').textContent=`Страница ${history.length+1} · ${items.length} проектов${pinned?' · выбранный проект вне страницы':''}`;
      if(before!==projects.value)projects.dispatchEvent(new Event('change'));
    } finally {pageBusy=false;pager();}
  }
  previousPage.onclick=()=>pageHistory.length&&loadProjects(pageHistory.at(-1),pageHistory.slice(0,-1));
  nextPage.onclick=()=>nextCursor&&loadProjects(nextCursor,[...pageHistory,pageAfter]);
  root.querySelector('[name=workspace]').addEventListener('change', event => {
    document.body.dataset.workspaceMode = event.target.value;
    try { localStorage.setItem('ori-workspace-mode', event.target.value); } catch {}
  });
  let mode = 'research';
  try { if (localStorage.getItem('ori-workspace-mode') === 'monitoring') mode = 'monitoring'; } catch {}
  root.querySelector('[name=workspace]').value = mode;
  document.body.dataset.workspaceMode = mode;
  root.querySelector('[data-action=refresh]').onclick = () => loadProjects();
  root.querySelector('[data-action=preflight]').onclick = () => projects.value && request('/' + encodeURIComponent(projects.value) + '/preflight');
  root.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const fields = new FormData(event.target), session = document.getElementById('session-select').value;
    const result = await request('', {name:fields.get('name'), site:fields.get('site'), session_id:session});
    if (result) await loadProjects('',[],result);
  };
  const value = name => root.querySelector(`[name=${name}]`).value;
  function scoped(action, body) { return projects.value && request('/'+encodeURIComponent(projects.value)+'/'+action, body); }
  root.querySelector('[data-action=baseline]').onclick = () => scoped('baselines', {name:value('baseline-name'),location:value('baseline-location'),confirm:root.querySelector('[name=baseline-confirm]').checked});
  root.querySelector('[data-action=allowed]').onclick = () => scoped('allowed', {name:value('allowed-name'),start_hz:Number(value('allowed-start')),end_hz:Number(value('allowed-end')),watch_changes:root.querySelector('[name=allowed-watch]').checked});
  root.querySelector('[data-action=analyze]').onclick = () => scoped('analyze', {});
  root.querySelector('[data-action=events]').onclick = () => scoped('events');
  root.querySelector('[data-action=arm]').onclick = () => scoped('arm', {});
  root.querySelector('[data-action=disarm]').onclick = () => scoped('disarm', {});
  root.querySelector('[data-action=guard-status]').onclick = () => scoped('status');
  root.querySelector('[data-action=receiver-config]').onclick = () => scoped('configure', {receiver_mode:value('receiver-mode')});
  root.querySelector('[data-action=audio-inputs]').onclick = () => request('/audio-inputs');
  root.querySelector('[data-action=audio-config]').onclick = () => {
    const option = root.querySelector('[name=audio-input]').selectedOptions[0];
    if (!option?.value || !root.querySelector('[name=audio-confirm]').checked) { info.textContent = 'Выберите и подтвердите аналоговый аудиовход'; return; }
    scoped('configure', {audio:{device:Number(option.value),name:option.dataset.deviceName,confirm:true}});
  };
  root.querySelector('[data-action=audio-disable]').onclick = () => scoped('configure', {audio:null});
  projects.addEventListener('change', () => { generation++; followedSession = ''; root.querySelector('[data-role=events]').replaceChildren(); root.querySelector('audio').pause(); root.querySelector('audio').removeAttribute('src'); });
  function renderEvents(items) {
    const list = root.querySelector('[data-role=events]'); list.replaceChildren();
    const eventChoice = root.querySelector('[name=search-event]'), previous = eventChoice.value;
    eventChoice.replaceChildren(new Option('Выберите событие',''), ...items.map(e => new Option(`${e.center_frequency_hz} Гц · ${e.first_seen}`,e.id)));
    if (items.some(e => e.id === previous)) eventChoice.value = previous;
    for (const event of items) {
      const row = document.createElement('div'), detail = document.createElement('button');
      detail.textContent = `${event.first_seen} · ${event.center_frequency_hz} Гц · ${event.peak_level} dBm · ${event.kind} · ${event.analysis?.state || 'ожидает анализа'}`;
      detail.onclick = () => { eventChoice.value = event.id; return scoped('events/' + encodeURIComponent(event.id)); }; row.append(detail);
      const source = document.createElement('button'); source.textContent = 'Исходные панорамы';
      source.onclick = () => {
        eventChoice.value = event.id; root.querySelector('[name=history-event]').checked = true;
        root.querySelector('[data-action=history]').onclick();
      };
      row.append(source);
      if (event.analysis?.audio) {
        const play = document.createElement('button'); play.textContent = 'Слушать запись';
        play.onclick = () => { const player = root.querySelector('audio'); player.src = `/api/v1/monitoring-projects/${encodeURIComponent(projects.value)}/events/${encodeURIComponent(event.id)}/audio`; player.play().catch(error => { info.textContent = error.message; }); };
        row.append(play);
      }
      list.append(row);
    }
  }
  window.oriMonitoring = {request, selected: () => projects.value, root};
  loadProjects();
})();
