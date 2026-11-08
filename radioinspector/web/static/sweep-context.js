(() => {
  "use strict";

  // Read-only archive presentation. Window selection and compatibility belong to the core.
  const dom = Object.fromEntries(["anchor", "before", "after", "run", "clear", "source", "pinned", "status", "summary", "items", "selected", "canvas", "threshold", "occupancy-run", "occupancy"]
    .map(name => [name, document.getElementById(`context-${name}`)]));
  if (!dom.status) return;
  const MAX_RESPONSE_BYTES = 2000000;
  let scope = "", snapshot = {}, sweeps = [], pending = null, generation = 0, pinned = null, result = null, chosen = null;
  let connected = true, suspended = false;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const scopeIdentity = value => typeof value === "string" && value.trim().length > 0 &&
    !Array.from(value).some(character => character.length === 1 && /[\uD800-\uDFFF]/.test(character));
  const identity = value => scopeIdentity(value) && Array.from(value).length <= 128;
  const clone = value => JSON.parse(JSON.stringify(value));
  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  }
  function setting(value, allowNull = false) {
    if (value === null) return allowNull;
    if (typeof value === "number") return finite(value);
    if (["boolean", "string"].includes(typeof value)) return true;
    if (Array.isArray(value)) return value.every(item => setting(item, allowNull));
    return object(value) && Object.values(value).every(item => setting(item, allowNull));
  }
  function settingsValid(settings) {
    return object(settings) && Object.keys(settings).some(key => key !== "sequence") &&
      Object.entries(settings).every(([key, value]) => setting(value, key === "sequence"));
  }
  const acquisition = settings => Object.fromEntries(Object.entries(settings).filter(([key]) => key !== "sequence"));
  // Do not use Date for ordering sub-millisecond timestamps or localeCompare for exact IDs.
  function utc(value) {
    const match = typeof value === "string" && /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value);
    const date = match && new Date(`${match[1]}Z`);
    if (!match || match[0] !== value || match[1].startsWith("0000-") || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== match[1]) {
      throw new Error("Некорректное время UTC");
    }
    return `${match[1]}.${(match[2] || "").padEnd(6, "0")}`;
  }
  function interval(value) {
    if (!object(value) || utc(value.started_at) > utc(value.ended_at)) throw new Error("Некорректный интервал UTC");
    return `${value.started_at} … ${value.ended_at}`;
  }
  function sameInterval(a, b) {
    interval(a); interval(b);
    return utc(a.started_at) === utc(b.started_at) && utc(a.ended_at) === utc(b.ended_at);
  }
  function idCompare(a, b) {
    const left = Array.from(a, c => c.codePointAt(0)), right = Array.from(b, c => c.codePointAt(0));
    for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
    return left.length - right.length; // Unicode scalar order equals UTF-8 byte order for valid IDs.
  }
  function ordered(a, b) {
    const left = utc(a.interval.started_at), right = utc(b.interval.started_at);
    return left === right ? idCompare(a.id, b.id) : left < right ? -1 : 1;
  }
  function sourceValid(source, frequencies, points) {
    if (!object(source) || !identity(source.id) || source.level_unit !== "dBm" ||
        !Number.isSafeInteger(points) || points < 1 || points > 16384 ||
        !Array.isArray(frequencies) || frequencies.length !== points ||
        !frequencies.every((v, i) => Number.isSafeInteger(v) && v >= 0 && (!i || v >= frequencies[i - 1])) ||
        !Array.isArray(source.levels) || source.levels.length !== points || !source.levels.every(finite) ||
        !settingsValid(source.settings)) throw new Error("Некорректный dBm свип, сетка или записанные настройки");
    interval(source.interval);
  }
  function count(name) {
    const raw = dom[name].value.trim(), value = Number(raw);
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(value) || value < 0 || value > 16) throw new Error("N1 и N2: целые числа от 0 до 16");
    return value;
  }
  function validate(data, request) {
    if (!object(data) || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        data.anchor_id !== request.source.id || data.order !== "started_at_then_id" || data.level_unit !== "dBm" ||
        !object(data.requested) || data.requested.before !== request.before || data.requested.after !== request.after ||
        !object(data.available) || !object(data.missing) || !Array.isArray(data.items)) {
      throw new Error("Ответ не соответствует зафиксированному якорю, области или запросу N1/N2");
    }
    for (const side of ["before", "after"]) {
      if (!Number.isSafeInteger(data.available[side]) || data.available[side] < 0 || data.available[side] > request[side] ||
          !Number.isSafeInteger(data.missing[side]) || data.missing[side] !== request[side] - data.available[side]) {
        throw new Error("Некорректное число доступных или недостающих свипов");
      }
    }
    if (data.complete !== (data.missing.before === 0 && data.missing.after === 0) ||
        data.items.length !== data.available.before + 1 + data.available.after || data.items.length > 33 ||
        !Number.isSafeInteger(data.points) || data.points * data.items.length > 131072) throw new Error("Некорректный размер или полнота окна");
    const ids = new Set();
    data.items.forEach((item, index) => {
      sourceValid(item, data.frequencies_hz, data.points);
      if (!Number.isSafeInteger(item.offset) || item.offset !== index - data.available.before || ids.has(item.id) ||
          !equal(acquisition(item.settings), acquisition(request.source.settings)) ||
          (index && ordered(data.items[index - 1], item) >= 0)) {
        throw new Error("Некорректные смещения, порядок, ID или несовместимые настройки окна");
      }
      ids.add(item.id);
    });
    const anchor = data.items[data.available.before];
    if (anchor.id !== request.source.id || !equal(data.frequencies_hz, request.source.frequencies_hz) ||
        !equal(anchor.levels, request.source.levels) || !equal(anchor.settings, request.source.settings) ||
        !sameInterval(anchor.interval, request.source.interval)) throw new Error("Исходные данные якоря не совпадают с зафиксированным архивным свипом");
    return data;
  }
  function cancelReader(reader) {
    try { Promise.resolve(reader.cancel()).catch(() => {}); } catch (_) { /* Already closed. */ }
  }
  function cancel() {
    generation++;
    if (pending) {
      window.clearTimeout(pending.timer); pending.controller.abort();
      if (pending.reader) cancelReader(pending.reader);
      pending = null;
    }
  }
  function strictJSON(text) {
    // JSON.parse alone silently overwrites duplicate keys and accepts 1e999 as Infinity.
    // Scan structure first, comparing decoded keys exactly, including escaped spellings.
    let position = 0;
    const space = () => { while (/[\t\n\r ]/.test(text[position] || "!") && position < text.length) position++; };
    function string() {
      if (text[position] !== '"') throw new Error("Некорректный JSON");
      const start = position++;
      while (position < text.length) {
        if (text[position] === "\\") { position += 2; continue; }
        if (text[position++] === '"') return JSON.parse(text.slice(start, position));
      }
      throw new Error("Незавершённая строка JSON");
    }
    function visit() {
      space();
      if (text[position] === '"') { string(); return; }
      const opening = text[position];
      if (opening === "{" || opening === "[") {
        const closing = opening === "{" ? "}" : "]", keys = new Set(); position++; space();
        if (text[position] === closing) { position++; return; }
        while (position < text.length) {
          space();
          if (opening === "{") {
            const key = string();
            if (keys.has(key)) throw new Error("Повторяющийся ключ JSON");
            keys.add(key); space();
            if (text[position++] !== ":") throw new Error("Некорректный JSON");
          }
          visit(); space();
          const separator = text[position++];
          if (separator === closing) return;
          if (separator !== ",") throw new Error("Некорректный JSON");
        }
        throw new Error("Незавершённый JSON");
      }
      const start = position;
      while (position < text.length && !/[\t\n\r ,\]}]/.test(text[position])) position++;
      const value = JSON.parse(text.slice(start, position));
      if (typeof value === "number" && !finite(value)) throw new Error("Неконечное число JSON");
    }
    visit(); space();
    if (position !== text.length) throw new Error("Некорректный JSON");
    return JSON.parse(text);
  }
  async function readPayload(response, controller, current) {
    if (!response.body?.getReader) throw new Error("Сервер не предоставил потоковый ответ");
    const reader = response.body.getReader();
    if (pending) pending.reader = reader;
    let complete = false;
    try {
      const length = response.headers?.get("Content-Length");
      if (length != null && Number(length) > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает предел 2 000 000 байт; данные не усечены");
      const chunks = []; let bytes = 0;
      while (true) {
        const part = await reader.read();
        if (current !== generation) return null;
        if (part.done) { complete = true; break; }
        bytes += part.value.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает предел 2 000 000 байт; данные не усечены");
        chunks.push(part.value);
      }
      // Bound every HTTP status before decoding; only 200 is a complete result.
      if (response.status !== 200) {
        const detail = response.status === 404 ? "сервер не поддерживает контекст либо сеанс/свип не найден" :
          response.status === 409 ? "окно несовместимо: сетка, единицы, настройки или предел точек" :
          response.status === 400 ? "сервер отклонил параметры" : response.status === 413 ? "превышен размер запроса" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const body = new Uint8Array(bytes); let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
      return strictJSON(new TextDecoder("utf-8", { fatal: true }).decode(body));
    } finally {
      if (!complete) { controller.abort(); cancelReader(reader); }
      reader.releaseLock();
      if (pending?.reader === reader) pending.reader = null;
    }
  }
  function discard() {
    dom.occupancy.textContent = "";
    result = null; dom.summary.textContent = ""; dom.selected.textContent = ""; dom.items.replaceChildren();
    dom.canvas.hidden = true; dom.canvas.getContext("2d").clearRect(0, 0, dom.canvas.width, dom.canvas.height);
  }
  function candidate() { return chosen?.id === dom.anchor.value ? chosen : sweeps.find(item => item.id === dom.anchor.value); }
  function controls() {
    const source = candidate();
    dom.run.disabled = suspended || !connected || Boolean(pending) || !source || !snapshot.sessionId || !snapshot.streamId;
    dom["occupancy-run"].disabled = dom.run.disabled;
    dom.anchor.disabled = suspended || !connected || (!sweeps.length && !chosen);
    dom.source.textContent = source ? `Для следующей загрузки: ${source.id}. ${chosen ? "Выбор сохранён локально; повторная загрузка проверит текущее окно архива." : "Выберите любой полученный dBm свип этого потока."}` : "Нет полученного архивного dBm свипа выбранного потока.";
    dom.pinned.textContent = pinned ? `Зафиксированный якорь (offset 0): ${pinned.source.id}\nСеанс: ${pinned.sessionId} · поток: ${pinned.streamId}\nUTC: ${interval(pinned.source.interval)}\nЗаписанные настройки: ${JSON.stringify(pinned.source.settings)}` : "Якорь не закреплён.";
  }
  function clear() {
    cancel(); discard(); pinned = null;
    dom.status.textContent = "Контекст только по кнопке. Новые свипы не меняют зафиксированный якорь или окно.";
    controls();
  }
  function update(next) {
    const nextScope = JSON.stringify([window.location.origin, next.sessionId, next.streamId]), previous = dom.anchor.value;
    const previousSource = candidate();
    snapshot = { sessionId: next.sessionId, streamId: next.streamId };
    sweeps = (next.sweeps || []).filter(item => item.stream_id === next.streamId && item.level_unit === "dBm" &&
      (!Object.hasOwn(item, "session_id") || item.session_id === next.sessionId));
    if (scope !== nextScope) chosen = null;
    else if (!chosen && previousSource && !sweeps.some(item => item.id === previous)) {
      try { chosen = clone(previousSource); } catch (_) { chosen = null; }
    }
    // A selected full source survives rolling pages so the user can explicitly reload
    // its now-larger after-window. It is not a persistent reference or an automatic capture.
    const choices = sweeps.map(item => chosen?.id === item.id ? chosen : item);
    if (chosen && !choices.some(item => item.id === chosen.id)) choices.push(chosen);
    const options = choices.map(item => {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${item.id} · ${item.interval?.started_at || "UTC неизвестно"}${chosen === item ? " · сохранённый выбор" : ""}`; return option;
    });
    dom.anchor.replaceChildren(...options);
    dom.anchor.value = scope === nextScope && choices.some(item => item.id === previous) ? previous : (sweeps.at(-1)?.id || "");
    if (scope !== nextScope) { scope = nextScope; clear(); }
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) { chosen = null; clear(); dom.status.textContent = "Нет соединения с ядром. После подключения загрузите контекст по кнопке."; }
    controls();
  }
  function draw(data, item) {
    const canvas = dom.canvas, ctx = canvas.getContext("2d"), width = canvas.width, height = canvas.height;
    ctx.fillStyle = "#07131c"; ctx.fillRect(0, 0, width, height);
    const left = 82, right = width - 24, top = 40, bottom = height - 40;
    const low = Math.min(...item.levels), high = Math.max(...item.levels), scale = Math.max(1, Math.abs(low), Math.abs(high));
    const pad = Math.max(0.08, (high / scale - low / scale) * 0.08), minimum = low / scale - pad, maximum = high / scale + pad;
    const first = data.frequencies_hz[0], last = data.frequencies_hz.at(-1);
    const x = frequency => left + (last === first ? 0.5 : (frequency - first) / (last - first)) * (right - left);
    const y = level => bottom - (level / scale - minimum) / (maximum - minimum) * (bottom - top);
    ctx.font = "12px monospace"; ctx.fillStyle = "#a3bac8";
    ctx.fillText(`${high} dBm`, 4, top); ctx.fillText(`${low} dBm`, 4, bottom);
    ctx.fillText(`Исходный спектр · offset ${item.offset} · dBm`, left, 20);
    ctx.fillText(`${first} Гц`, left, height - 12); ctx.textAlign = "right"; ctx.fillText(`${last} Гц`, right, height - 12); ctx.textAlign = "left";
    ctx.strokeStyle = "#60e4ef"; ctx.fillStyle = "#60e4ef"; ctx.lineWidth = 2; ctx.setLineDash([]); ctx.beginPath();
    // Every raw bin, including repeated frequencies. A single bin remains visible.
    item.levels.forEach((level, index) => {
      if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level)); else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
    });
    ctx.stroke(); ctx.fillRect(x(first) - 2, y(item.levels[0]) - 2, 4, 4); canvas.hidden = false;
    canvas.setAttribute("aria-label", `Исходный спектр ${item.id}, offset ${item.offset}, все ${data.points} точек dBm. Якорь: ${data.anchor_id}.`);
  }
  function select(index) {
    if (!result || !Number.isSafeInteger(index) || index < 0 || index >= result.items.length) return;
    const item = result.items[index];
    dom.selected.textContent = `Показан свип: ${item.id} · offset ${item.offset}${item.offset === 0 ? " · ЯКОРЬ" : " · контекст, не якорь"}\nUTC: ${interval(item.interval)}\nЗаписанные настройки: ${JSON.stringify(item.settings)}\nВсе ${result.points} исходных точек · dBm, частоты в Гц.`;
    Array.from(dom.items.children).forEach((row, i) => row.children[0].setAttribute("aria-pressed", String(i === index)));
    draw(result, item);
  }
  function render(data) {
    result = data;
    dom.summary.textContent = `Якорь: ${data.anchor_id} · offset 0\nЗапрошено N1=${data.requested.before}, N2=${data.requested.after}. Доступно до: ${data.available.before}, после: ${data.available.after}. Недостаёт до: ${data.missing.before}, после: ${data.missing.after}.\n${data.complete ? "Полное окно" : "Неполное окно архива"}: ${data.items.length} свипов × ${data.points} точек = ${data.items.length * data.points} исходных точек dBm.\nПорядок: начало UTC, затем точный ID (UTF-8). Общая сетка без интерполяции; совместимость записанных настроек, кроме sequence.`;
    dom.items.replaceChildren(...data.items.map((item, index) => {
      const row = document.createElement("li"), button = document.createElement("button"), text = document.createElement("p");
      button.type = "button"; button.className = "button";
      button.textContent = `Показать offset ${item.offset}: ${item.id}${item.offset === 0 ? " · ЯКОРЬ" : ""}`;
      button.addEventListener("click", () => { if (result === data) select(index); });
      text.textContent = `ID: ${item.id}\nUTC: ${interval(item.interval)}\nЗаписанные настройки: ${JSON.stringify(item.settings)} · dBm`;
      row.replaceChildren(button, text); return row;
    }));
    select(data.available.before);
  }
  function validateOccupancy(data, threshold) {
    const value = data.occupancy, samples = data.items.length;
    if (!object(value) || value.method !== "sample_fraction" || value.comparison !== "strictly_greater" ||
        value.threshold_dbm !== threshold || value.sample_count !== samples || value.rf_time_occupancy !== null ||
        !Array.isArray(value.exceedance_counts) || value.exceedance_counts.length !== data.points ||
        !Array.isArray(value.sample_fractions) || value.sample_fractions.length !== data.points ||
        !value.exceedance_counts.every((count, i) => Number.isSafeInteger(count) && count >= 0 && count <= samples &&
          finite(value.sample_fractions[i]) && Math.abs(value.sample_fractions[i] - count / samples) < 1e-12) ||
        !["acquisition_span_seconds", "acquisition_covered_seconds", "acquisition_gap_seconds"].every(key => finite(value[key]) && value[key] >= 0) ||
        value.acquisition_covered_seconds > value.acquisition_span_seconds ||
        Math.abs(value.acquisition_span_seconds - value.acquisition_covered_seconds - value.acquisition_gap_seconds) > 1e-6 ||
        !Number.isSafeInteger(value.acquisition_gap_count) || value.acquisition_gap_count < 0 || value.acquisition_gap_count >= samples) {
      throw new Error("Некорректный результат занятости или полноты наблюдения");
    }
    return value;
  }
  function renderOccupancy(data, value) {
    // Ranking is presentation only; measurements and fractions come from the core.
    const ranked = data.frequencies_hz.map((hz, index) => ({ hz, index, count: value.exceedance_counts[index] }))
      .sort((a, b) => b.count - a.count || a.index - b.index).slice(0, 20);
    dom.occupancy.textContent = `Порог: > ${value.threshold_dbm} dBm · ${value.sample_count} свипов\n` +
      `Интервал архива: ${value.acquisition_span_seconds} с; объединение интервалов сканирования: ${value.acquisition_covered_seconds} с\n` +
      `Разрывы: ${value.acquisition_gap_count}, всего ${value.acquisition_gap_seconds} с. Занятость по времени RF неизвестна.\n` +
      `Наиболее активные точки: ${ranked.length} из ${data.points} (одинаковые частоты сохраняются отдельно)\n` +
      ranked.map(row => `${row.hz} Гц [${row.index}] · ${row.count}/${value.sample_count} · ${(100 * value.sample_fractions[row.index]).toFixed(1)}%`).join("\n");
  }
  async function load(withOccupancy = false) {
    if (suspended || !connected || pending || !candidate() || !snapshot.sessionId || !snapshot.streamId) return;
    cancel(); discard(); pinned = null;
    let request, body;
    try {
      const source = candidate();
      sourceValid(source, source.frequencies_hz, source.levels?.length);
      if (!scopeIdentity(snapshot.sessionId) || !scopeIdentity(snapshot.streamId) ||
          (Object.hasOwn(source, "session_id") && source.session_id !== snapshot.sessionId) ||
          sweeps.filter(item => item.id === source.id).length > 1) throw new Error("Некорректная или неоднозначная область/ID якоря");
      request = { ...snapshot, source: clone(source), before: count("before"), after: count("after") };
      const parameters = { sweep_id: request.source.id, before: request.before, after: request.after };
      if (withOccupancy) {
        const raw = dom.threshold.value.trim();
        if (!raw || !finite(Number(raw))) throw new Error("Введите конечный порог dBm");
        request.threshold = Number(raw); parameters.threshold_dbm = request.threshold;
      }
      body = JSON.stringify(parameters);
      if (new TextEncoder().encode(body).length > 8192) throw new Error("Запрос превышает предел 8192 байт UTF-8");
    } catch (error) { dom.status.textContent = error.message; controls(); return; }
    pinned = request; chosen = clone(request.source);
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      clear(); dom.status.textContent = "Контекст недоступен: таймаут 8 секунд. Повторите по кнопке.";
    }, 8000);
    pending = { controller, timer }; controls(); dom.status.textContent = `Загрузка контекста якоря ${request.source.id}…`;
    try {
      const route = withOccupancy ? "sweep-occupancy" : "sweep-context";
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/${route}`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, cache: "no-store", signal: controller.signal, body,
      });
      if (current !== generation) { if (response.body?.cancel) cancelReader(response.body); return; }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      const data = validate(payload.data, request);
      const occupancy = withOccupancy ? validateOccupancy(data, request.threshold) : null;
      render(data);
      if (occupancy) renderOccupancy(data, occupancy);
      dom.status.textContent = "Окно ядра зафиксировано как снимок архива. Автоматический захват N1/N2 при обнаружении не реализован. Нет привязки к кандидатам, записи или команд прибору.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Контекст недоступен: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer); if (current === generation) { pending = null; controls(); }
    }
  }
  dom.run.addEventListener("click", () => load()); dom.clear.addEventListener("click", clear);
  dom["occupancy-run"].addEventListener("click", () => load(true));
  for (const name of ["before", "after", "anchor", "threshold"]) dom[name].addEventListener(name === "anchor" ? "change" : "input", () => {
    if (name === "anchor") {
      const source = sweeps.find(item => item.id === dom.anchor.value);
      try { chosen = source ? clone(source) : (chosen?.id === dom.anchor.value ? chosen : null); } catch (_) { chosen = null; }
    }
    clear(); dom.status.textContent = "Выбор якоря или N1/N2 изменён. Загрузите новый контекст по кнопке.";
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; chosen = null; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepContext = { update, setConnected };
  clear();
})();
