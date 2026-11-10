/* Core-produced display data; local state never owns the monitoring lifecycle. */
(() => {
  'use strict';
  const api = window.oriMonitoring;
  if (!api) return;
  const root = api.root, node = name => root.querySelector(`[name=${name}]`);
  const output = root.querySelector('pre');
  let revision = 0, leases = {}, cursor = null, sweeps = [];
  const value = name => node(name).value;
  async function call(resource, body, query) {
    const project = api.selected(), current = revision;
    if (!project) { output.textContent = 'Выберите проект'; return; }
    const suffix = query ? '?' + new URLSearchParams(query) : '';
    const data = await api.request('/' + encodeURIComponent(project) + '/' + resource + suffix, body);
    return current === revision && project === api.selected() ? data : undefined;
  }
  function lease(role, expiredOK = false) {
    const item = leases[role];
    if (!item || (!expiredOK && Date.now() >= item.until)) {
      output.textContent = 'Нужна действующая ручная аренда. Истёкшая аренда оставляет паузу; верните прибор или снимите охрану.';
      return null;
    }
    return item;
  }
  const bind = (action, fn) => { root.querySelector(`[data-action=${action}]`).onclick = fn; };
  bind('manual-acquire', async () => {
    const role = value('manual-role'), data = await call('manual/acquire', {role});
    if (data) leases[role] = {token:data.token,until:Date.now()+data.expires_seconds*1000};
  });
  bind('manual-renew', async () => {
    const role = value('manual-role'), owner = lease(role); if (!owner) return;
    const data = await call('manual/renew', {token:owner.token});
    if (data) owner.until = Date.now()+data.expires_seconds*1000;
  });
  bind('manual-return', async () => {
    const role = value('manual-role'), owner = lease(role,true); if (!owner) return;
    const data = await call('manual/return', {token:owner.token});
    if (data) { delete leases[role]; coverage(data.state === 'restoring' ? 'restoring' : 'wide'); }
  });
  bind('manual-tune', () => {
    const owner = lease('receiver'); if (!owner) return;
    return call('manual/tune', {token:owner.token,frequency_hz:Number(value('manual-frequency')),mode:value('receiver-mode')});
  });
  bind('search-start', async () => {
    const owner = lease('analyzer'); if (!owner) return;
    const data = await call('search/start', {token:owner.token,event_id:value('search-event'),span_hz:Number(value('search-span'))});
    if (data) renderSearch(data);
  });
  bind('search-status', async () => { const data = await call('search/status'); if (data) renderSearch(data); });
  bind('search-mark', async () => {
    const owner = lease('analyzer'); if (!owner) return;
    const data = await call('search/mark', {token:owner.token,label:value('position-label')});
    if (data) { const status = await call('search/status'); if (status) renderSearch(status); }
  });
  function coverage(state) {
    const labels = {narrow:'Узкий обзор: ШИРОКОЕ ПОКРЫТИЕ ПРИОСТАНОВЛЕНО',paused:'Широкое покрытие на паузе',restoring:'Проверяется возврат широкого покрытия',wide:'Широкое покрытие восстановлено',stopped:'Охрана остановлена',error:'Ошибка покрытия'};
    root.querySelector('[data-role=coverage]').textContent = labels[state] || state;
  }
  function canvas(role) {
    const element = root.querySelector(`[data-role=${role}]`), ctx = element.getContext('2d');
    ctx.clearRect(0,0,element.width,element.height); ctx.fillStyle = '#cbd8e8'; ctx.font = '12px sans-serif';
    return {element,ctx,w:element.width,h:element.height};
  }
  function drawLines(role, series, xRange, yRange, labels) {
    const {ctx,w,h} = canvas(role), left = 70, right = w-20, top = 22, bottom = h-34;
    const x = v => left+(v-xRange[0])/(xRange[1]-xRange[0] || 1)*(right-left);
    const y = v => bottom-(v-yRange[0])/(yRange[1]-yRange[0] || 1)*(bottom-top);
    ctx.fillText(labels[0],left,h-8); ctx.fillText(labels[1],left,14);
    ctx.fillText(String(yRange[0])+' dBm',2,bottom); ctx.fillText(String(yRange[1])+' dBm',2,top);
    for (const line of series) {
      ctx.strokeStyle = line.color; ctx.beginPath();
      line.points.forEach(([a,b],i) => i ? ctx.lineTo(x(a),y(b)) : ctx.moveTo(x(a),y(b))); ctx.stroke();
    }
  }
  function renderSearch(data) {
    coverage(data.coverage);
    const series = data.series || [];
    drawLines('source-chart',[{color:'#63d8bb',points:series.map(p => [Date.parse(p.started_at),p.peak_level])}],
      [Date.parse(series[0]?.started_at || 0),Date.parse(series.at(-1)?.ended_at || 0)],[-130,0],
      [`${series[0]?.started_at || ''} → ${series.at(-1)?.ended_at || ''} UTC`, `Максимум ядра: ${data.maximum_level ?? '—'} dBm · отметок: ${(data.marks || []).length}`]);
  }
  async function loadHistory(next = false) {
    const query = {limit:'32'};
    for (const [field,key] of [['history-start','start'],['history-end','end'],['history-min','min_hz'],['history-max','max_hz']]) if (value(field)) query[key] = value(field);
    if (node('history-event').checked) { if (!value('search-event')) { output.textContent = 'Выберите событие'; return; } query.event_id = value('search-event'); }
    if (next && cursor) query.after = cursor;
    const data = await call('history', undefined, query); if (!data) return;
    sweeps = data.items || []; cursor = data.next_cursor;
    root.querySelector('[data-action=history-next]').disabled = !cursor;
    node('history-sweep').replaceChildren(...sweeps.map(s => new Option(`${s.interval.started_at} · ${s.id} · ${s.original_point_count} исходных точек`,s.id)));
    drawHistory(); drawSelected();
  }
  bind('history', () => loadHistory()); bind('history-next', () => loadHistory(true));
  node('history-sweep').addEventListener('change', drawSelected);
  bind('history-raw', () => value('history-sweep') && call('history/'+encodeURIComponent(value('history-sweep'))));
  bind('history-compare', async () => {
    if (!value('history-sweep')) return;
    const data = await call('history/'+encodeURIComponent(value('history-sweep'))+'/comparison'); if (!data) return;
    if (!data.compatible) { output.textContent = data.reason; return; }
    const lines = [['current_dbm','#63d8bb'],['baseline_p50_dbm','#8eb8ff'],['baseline_p95_dbm','#e6b866']].map(([key,color]) => ({color,points:data.items.map(p => [p.frequency_hz,p[key]])}));
    drawLines('history-spectrum',lines,[data.items[0]?.frequency_hz || 0,data.items.at(-1)?.frequency_hz || 1],[-130,0],['Гц · только отображение','Текущий: зелёный; медиана: синий; верхний квантиль: жёлтый']);
  });
  function drawSelected() {
    const sweep = sweeps.find(s => s.id === value('history-sweep'));
    const points = sweep ? sweep.frequencies_hz.map((hz,i) => [hz,sweep.levels[i]]) : [];
    drawLines('history-spectrum',[{color:'#63d8bb',points}], [points[0]?.[0] || 0,points.at(-1)?.[0] || 1],[-130,0],['Гц · отображение, исходники сохранены',sweep?.interval.started_at || 'Нет свипа']);
  }
  function drawHistory() {
    const {ctx,w,h,element} = canvas('history-chart'); if (!sweeps.length) return;
    const times = sweeps.flatMap(s => [Date.parse(s.interval.started_at),Date.parse(s.interval.ended_at)]);
    const first = Math.min(...times), last = Math.max(...times), low = Math.min(...sweeps.map(s => s.frequencies_hz[0])), high = Math.max(...sweeps.map(s => s.frequencies_hz.at(-1)));
    element.dataset.startUtc = new Date(first).toISOString(); element.dataset.endUtc = new Date(last).toISOString();
    const x = hz => 70+(hz-low)/(high-low || 1)*(w-90), y = ms => 25+(ms-first)/(last-first || 1)*(h-65);
    for (const sweep of sweeps) {
      const top = y(Date.parse(sweep.interval.started_at)), bottom = y(Date.parse(sweep.interval.ended_at));
      sweep.frequencies_hz.forEach((hz,i,grid) => {
        const left = i ? (grid[i-1]+hz)/2 : hz, right = i+1<grid.length ? (grid[i+1]+hz)/2 : hz;
        const heat = Math.max(0,Math.min(1,(sweep.levels[i]+130)/130)); ctx.fillStyle = `hsl(${240-240*heat} 85% 50%)`;
        ctx.fillRect(x(left),top,Math.max(1,x(right)-x(left)),Math.max(1,bottom-top));
      });
    }
    ctx.fillStyle = '#cbd8e8'; ctx.fillText(`${low} → ${high} Гц`,70,h-23);ctx.fillText(`${new Date(first).toISOString()} → ${new Date(last).toISOString()} UTC`,70,h-7);
    ctx.fillText('UTC ↓',2,20);
  }
  node('project').addEventListener('change', () => {
    revision++; leases = {}; cursor = null; sweeps = [];
    node('history-sweep').replaceChildren(); node('search-event').replaceChildren(new Option('Выберите событие',''));
    for (const role of ['source-chart','history-chart','history-spectrum']) canvas(role);
    coverage('paused');
  });
})();
