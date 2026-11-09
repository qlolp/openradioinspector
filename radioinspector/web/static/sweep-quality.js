(() => {
  "use strict";
  // Display archived evidence only; the core owns the quality heuristic.
  const dom = Object.fromEntries(["anchor", "margin", "high", "run", "clear", "status", "summary"]
    .map(name => [name, document.getElementById(`quality-${name}`)]));
  if (!dom.status) return;
  const MAX_RESPONSE_BYTES = 2000000;
  const DISCLAIMER = "Эвристический обзор архива: не диагностирует перегрузку или помехи, не отбрасывает и не изменяет отсчёты.";
  let scope = "", snapshot = {}, sweeps = [], chosen = null, pending = null, generation = 0;
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
    if (typeof value === "boolean") return true;
    if (typeof value === "string") return !Array.from(value).some(c => c.length === 1 && /[\uD800-\uDFFF]/.test(c));
    if (Array.isArray(value)) return value.every(item => setting(item, allowNull));
    return object(value) && Object.entries(value).every(([key, item]) => setting(key, allowNull) && setting(item, allowNull));
  }
  function settingsValid(settings) {
    return setting(settings, true);
  }
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
  function require(ok) { if (!ok) throw new Error("Некорректный отчёт качества или несовпадение с выбранным архивным свипом"); }
  function sourceValid(source) {
    require(object(source) && identity(source.id) && source.level_unit === "dBm" &&
      Array.isArray(source.levels) && source.levels.length >= 2 && source.levels.length <= 16384 && source.levels.every(finite) &&
      Array.isArray(source.frequencies_hz) && source.frequencies_hz.length === source.levels.length &&
      source.frequencies_hz.every((v,i,a) => Number.isSafeInteger(v) && v >= 0 && (!i || v > a[i-1])) &&
      Object.hasOwn(source, "settings") && settingsValid(source.settings) &&
      new TextEncoder().encode(JSON.stringify({id:source.id, interval:source.interval, level_unit:source.level_unit, settings:source.settings})).length <= 65536);
    interval(source.interval);
  }
  function parameters() {
    const edge_margin_db = Number(dom.margin.value), high_level_dbm = Number(dom.high.value);
    require(dom.margin.value.trim() && dom.high.value.trim() && finite(edge_margin_db) && edge_margin_db > 0 && edge_margin_db <= 200 &&
      finite(high_level_dbm) && high_level_dbm >= -200 && high_level_dbm <= 100);
    return {edge_margin_db, high_level_dbm};
  }
  function exactKeys(value, keys) {
    return object(value) && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
  }
  function validate(data, request) {
    const source = request.source, p = request.parameters, n = source.levels.length;
    require(exactKeys(data, ["session_id", "stream_id", "sweep", "parameters", "points", "peak_index", "peak_frequency_hz", "peak_level_dbm", "high_level_count", "edge_assessments", "flags", "assessment", "method", "hardware_diagnosis", "raw_modified"]));
    require(exactKeys(data.sweep, ["id", "interval", "level_unit", "settings"]) &&
      exactKeys(data.sweep.interval, ["started_at", "ended_at"]));
    require(object(data) && data.session_id === request.sessionId && data.stream_id === request.streamId &&
      data.method === "archived_edge_level_review_v1" && data.hardware_diagnosis === false && data.raw_modified === false &&
      Number.isSafeInteger(data.points) && data.points === n && equal(data.parameters, p) &&
      object(data.sweep) && data.sweep.id === source.id && data.sweep.level_unit === "dBm" &&
      equal(data.sweep.settings, source.settings) && sameInterval(data.sweep.interval, source.interval));
    require(Number.isSafeInteger(data.peak_index) && data.peak_index >= 0 && data.peak_index < n &&
      Number.isSafeInteger(data.peak_frequency_hz) && data.peak_frequency_hz === source.frequencies_hz[data.peak_index] &&
      finite(data.peak_level_dbm) && data.peak_level_dbm === source.levels[data.peak_index] &&
      source.levels.every(v => v <= data.peak_level_dbm) && source.levels.indexOf(data.peak_level_dbm) === data.peak_index &&
      Number.isSafeInteger(data.high_level_count) && data.high_level_count === source.levels.filter(v => v >= p.high_level_dbm).length &&
      Array.isArray(data.edge_assessments) && data.edge_assessments.length === 2);
    const flags = [];
    for (const [position, edge, index, neighbor] of [[0,"first",0,1],[1,"last",n-1,n-2]]) {
      const item = data.edge_assessments[position], delta = source.levels[index] - source.levels[neighbor];
      require(exactKeys(item, ["edge", "index", "frequency_hz", "level_dbm", "neighbor_index", "neighbor_level_dbm", "delta_db", "flagged"]));
      require(object(item) && item.edge === edge && Number.isSafeInteger(item.index) && item.index === index &&
        Number.isSafeInteger(item.neighbor_index) && item.neighbor_index === neighbor &&
        Number.isSafeInteger(item.frequency_hz) && item.frequency_hz === source.frequencies_hz[index] &&
        finite(item.level_dbm) && item.level_dbm === source.levels[index] &&
        finite(item.neighbor_level_dbm) && item.neighbor_level_dbm === source.levels[neighbor] &&
        finite(item.delta_db) && item.delta_db === delta && item.flagged === (delta >= p.edge_margin_db));
      if (item.flagged) flags.push(`${edge}_edge_jump`);
    }
    if (data.high_level_count) flags.push("level_threshold");
    require(equal(data.flags, flags) && data.assessment === (flags.length ? "review" : "no_flags"));
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
    let position = 0, dataJSON = null;
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
    function visit(depth = 0) {
      space();
      if (text[position] === '"') { string(); return; }
      const opening = text[position];
      if (opening === "{" || opening === "[") {
        const closing = opening === "{" ? "}" : "]", keys = new Set(); position++; space();
        if (text[position] === closing) { position++; return; }
        while (position < text.length) {
          space();
          let key;
          if (opening === "{") {
            key = string();
            if (keys.has(key)) throw new Error("Повторяющийся ключ JSON");
            keys.add(key); space();
            if (text[position++] !== ":") throw new Error("Некорректный JSON");
          }
          space(); const start = position;
          visit(depth + 1);
          if (depth === 0 && key === "data") dataJSON = text.slice(start, position);
          space();
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
    return { payload: JSON.parse(text), dataJSON };
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
        const detail = response.status === 404 ? "сервер не поддерживает отчёт либо сеанс/свип не найден" :
          response.status === 409 ? "свип несовместим с отчётом" :
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
  function candidate() { return chosen?.id === dom.anchor.value ? chosen : sweeps.find(s => s.id === dom.anchor.value); }
  function controls() { dom.run.disabled = suspended || !connected || Boolean(pending) || !candidate() || !snapshot.sessionId || !snapshot.streamId; }
  function clear() { cancel(); dom.summary.textContent = ""; dom.status.textContent = DISCLAIMER; controls(); }
  function update(next) {
    const nextScope = JSON.stringify([window.location.origin, next.sessionId, next.streamId]), previous = dom.anchor.value;
    if (scope !== nextScope) chosen = null;
    snapshot = {sessionId: next.sessionId, streamId: next.streamId};
    sweeps = (next.sweeps || []).filter(source => {
      try { sourceValid(source); return source.stream_id === next.streamId && (!Object.hasOwn(source,"session_id") || source.session_id === next.sessionId); }
      catch (_) { return false; }
    });
    const choices = [...sweeps];
    if (chosen && !choices.some(s => s.id === chosen.id)) choices.push(chosen);
    dom.anchor.replaceChildren(...choices.map(source => {
      const option = document.createElement("option"); option.value = source.id;
      option.textContent = `${source.id} · ${source.interval.started_at}`; return option;
    }));
    dom.anchor.value = scope === nextScope && choices.some(s => s.id === previous) ? previous : (choices.at(-1)?.id || "");
    if (scope !== nextScope) { scope = nextScope; clear(); }
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) { chosen = null; sweeps = []; dom.anchor.replaceChildren(); dom.anchor.value = ""; clear(); dom.status.textContent = "Нет свежего соединения с ядром. Повторите после обновления."; }
    controls();
  }
  function render(data) {
    dom.summary.textContent = `Свип: ${data.sweep.id} · сеанс ${data.session_id} · поток ${data.stream_id}\nUTC: ${interval(data.sweep.interval)}\nНастройки: ${JSON.stringify(data.sweep.settings)}\n${data.points} точек · пик ${data.peak_level_dbm} dBm @ ${data.peak_frequency_hz} Hz (индекс ${data.peak_index})\nПорог уровня ≥ ${data.parameters.high_level_dbm} dBm: ${data.high_level_count} точек; край ≥ ${data.parameters.edge_margin_db} dB\n` +
      data.edge_assessments.map(e => `${e.edge}: индекс ${e.index}, ${e.frequency_hz} Hz, ${e.level_dbm} dBm; сосед ${e.neighbor_index}: ${e.neighbor_level_dbm} dBm; Δ ${e.delta_db} dB; флаг ${e.flagged}`).join("\n") +
      `\nОценка: ${data.assessment}; флаги: ${data.flags.join(", ") || "нет"}\n${DISCLAIMER}`;
  }
  async function analyze() {
    if (suspended || !connected || pending || !candidate() || !snapshot.sessionId || !snapshot.streamId) return;
    clear();
    let request;
    try {
      const source = candidate(); sourceValid(source);
      require(scopeIdentity(snapshot.sessionId) && scopeIdentity(snapshot.streamId) && source.stream_id === snapshot.streamId &&
        (!Object.hasOwn(source,"session_id") || source.session_id === snapshot.sessionId) && sweeps.filter(s => s.id === source.id).length <= 1);
      request = {...snapshot, source: clone(source), parameters: parameters()};
    } catch (error) { dom.status.textContent = error.message; return; }
    chosen = clone(request.source);
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => { if (current === generation) { clear(); dom.status.textContent = "Таймаут 8 секунд. Повторите по кнопке."; } }, 8000);
    pending = {controller, timer}; controls(); dom.status.textContent = "Загрузка отчёта архива…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-quality`, {
        method: "POST", headers: {Accept:"application/json", "Content-Type":"application/json"}, cache:"no-store", signal:controller.signal,
        body:JSON.stringify({sweep_id:request.source.id,...request.parameters}),
      });
      if (current !== generation) { if (response.body?.cancel) cancelReader(response.body); return; }
      const received = await readPayload(response, controller, current);
      if (current !== generation) return;
      const payload = received?.payload;
      require(payload?.contract === "openradioinspector-platform" && payload.contract_version === 1);
      render(validate(payload.data, request)); dom.status.textContent = DISCLAIMER;
    } catch (error) {
      if (current === generation) { dom.summary.textContent = ""; dom.status.textContent = `Отчёт недоступен: ${error.message}. Остальные панели доступны.`; }
    } finally { window.clearTimeout(timer); if (current === generation) { pending = null; controls(); } }
  }
  dom.run.addEventListener("click", analyze); dom.clear.addEventListener("click", clear);
  for (const name of ["anchor","margin","high"]) dom[name].addEventListener(name === "anchor" ? "change" : "input", () => {
    if (name === "anchor") chosen = null;
    clear();
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; chosen = null; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepQuality = {update, setConnected};
  clear();
})();
