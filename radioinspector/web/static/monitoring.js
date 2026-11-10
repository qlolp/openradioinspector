/* Project state belongs to the core; workspace selection is local presentation. */
(() => {
  'use strict';
  const root = document.getElementById('monitoring-workspace');
  if (!root) return;
  const projects = root.querySelector('[name=project]'), info = root.querySelector('pre');
  let generation = 0, busy = false;
  async function request(path = '', body) {
    if (busy) return;
    busy = true; const current = generation;
    try {
      const response = await fetch('/api/v1/monitoring-projects' + path, body === undefined ? {} : {
        method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
      });
      const raw = await response.text();
      if (raw.length > 2000000) throw Error('Ответ проекта превышает предел');
      const envelope = JSON.parse(raw);
      if (!response.ok) throw Error(envelope.error?.message || `HTTP ${response.status}`);
      if (current !== generation) return;
      const data = envelope.data;
      if (path === '' && Array.isArray(data.items)) {
        const previous = projects.value;
        projects.replaceChildren(...data.items.map(p => new Option(`${p.name} · ${p.site} · ${p.state}`, p.id)));
        if ([...projects.options].some(o => o.value === previous)) projects.value = previous;
      }
      if (path.endsWith('/events') && Array.isArray(data.items)) renderEvents(data.items);
      if (path === '/audio-inputs') {
        const select = root.querySelector('[name=audio-input]');
        select.replaceChildren(new Option('Не выбран', ''), ...(data.devices || []).map(d => {
          const option = new Option(`${d.index}: ${d.name}`, d.index); option.dataset.deviceName = d.name; return option;
        }));
      }
      info.textContent = JSON.stringify(data, null, 2);
      return data;
    } catch (error) { if (current === generation) info.textContent = error.message; }
    finally { busy = false; }
  }
  root.querySelector('[name=workspace]').addEventListener('change', event => {
    document.body.dataset.workspaceMode = event.target.value;
    localStorage.setItem('ori-workspace-mode', event.target.value);
  });
  const mode = localStorage.getItem('ori-workspace-mode') || 'research';
  root.querySelector('[name=workspace]').value = mode;
  document.body.dataset.workspaceMode = mode;
  root.querySelector('[data-action=refresh]').onclick = () => request();
  root.querySelector('[data-action=preflight]').onclick = () => projects.value && request('/' + encodeURIComponent(projects.value) + '/preflight');
  root.querySelector('form').onsubmit = async event => {
    event.preventDefault();
    const fields = new FormData(event.target), session = document.getElementById('session-select').value;
    const result = await request('', {name:fields.get('name'), site:fields.get('site'), session_id:session});
    if (result) await request();
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
  projects.addEventListener('change', () => { generation++; root.querySelector('[data-role=events]').replaceChildren(); root.querySelector('audio').pause(); root.querySelector('audio').removeAttribute('src'); });
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
  request();
})();
