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
      const listing = path === '' && body === undefined;
      const items = [], cursors = new Set();
      let nextPath = path, data;
      do {
        const response = await fetch('/api/v1/monitoring-projects' + nextPath, body === undefined ? {} : {
          method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
        });
        const raw = await response.text();
        if (raw.length > 2000000) throw Error('Ответ проекта превышает предел');
        const envelope = JSON.parse(raw);
        if (!response.ok) throw Error(envelope.error?.message || `HTTP ${response.status}`);
        if (current !== generation) return;
        data = envelope.data;
        if (!listing) break;
        if (!Array.isArray(data?.items)) throw Error('Некорректный список проектов');
        items.push(...data.items);
        const cursor = data.next_cursor;
        if (cursor === null || cursor === undefined || cursor === '') break;
        if (typeof cursor !== 'string' || cursors.has(cursor)) throw Error('Некорректный курсор проектов');
        cursors.add(cursor);
        nextPath = '?after=' + encodeURIComponent(cursor);
      } while (true);
      // Commit only a complete list: a failed later page must not hide the selection.
      if (listing) data = {...data, items, next_cursor: null};
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
    try { localStorage.setItem('ori-workspace-mode', event.target.value); } catch (_) { /* Optional preference. */ }
  });
  let mode = 'research';
  try {
    const saved = localStorage.getItem('ori-workspace-mode');
    if (saved === 'monitoring' || saved === 'research') mode = saved;
  } catch (_) { /* Storage may be disabled by browser privacy settings. */ }
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
  window.oriMonitoring = {request, selected: () => projects.value, root};
  request();
})();
