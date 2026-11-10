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
      if (Array.isArray(data.items)) {
        const previous = projects.value;
        projects.replaceChildren(...data.items.map(p => new Option(`${p.name} · ${p.site} · ${p.state}`, p.id)));
        if ([...projects.options].some(o => o.value === previous)) projects.value = previous;
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
  window.oriMonitoring = {request, selected: () => projects.value, root};
  request();
})();
