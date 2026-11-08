(() => {
  "use strict";

  // Presentation and consistency only. Independent threshold analysis lives in the core.
  const dom = Object.fromEntries(["level-1", "level-2", "level-3", "run", "clear", "source", "status", "summary", "canvas",
    "line-1-summary", "line-2-summary", "line-3-summary", "line-1-regions", "line-2-regions", "line-3-regions"]
    .map(name => [name, document.getElementById(`three-thresholds-${name}`)]));
  if (!dom.status) return;
  const MAX_RESPONSE_BYTES = 2000000;
  let scope = "", snapshot = {}, sweeps = [], pending = null, generation = 0;
  let connected = true, suspended = false;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const identity = value => typeof value === "string" && value.trim().length > 0 && Array.from(value).length <= 128 &&
    !Array.from(value).some(character => character.length === 1 && /[\uD800-\uDFFF]/.test(character));
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
  // Compare UTC at microsecond precision, while displaying the original timestamp unchanged.
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
  function sourceValid(source, frequencies, points) {
    if (!object(source) || !identity(source.id) || source.level_unit !== "dBm" ||
        !Number.isSafeInteger(points) || points < 1 || points > 16384 ||
        !Array.isArray(frequencies) || frequencies.length !== points ||
        !frequencies.every((v, i) => Number.isSafeInteger(v) && v >= 0 && (!i || v >= frequencies[i - 1])) ||
        !Array.isArray(source.levels) || source.levels.length !== points || !source.levels.every(finite) ||
        !settingsValid(source.settings)) throw new Error("Некорректный dBm свип, сетка или записанные настройки");
    interval(source.interval);
  }
  function parameters() {
    return [1, 2, 3].map(index => {
      const raw = dom[`level-${index}`].value.trim(), value = Number(raw);
      if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw) || !finite(value)) {
        throw new Error(`Линия ${index}: задайте конечное число dBm`);
      }
      return value;
    });
  }
  function validate(data, request) {
    if (!object(data) || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        data.sweep?.id !== request.source.id || data.method !== "three_horizontal" || data.comparison !== "strictly_greater" ||
        data.level_unit !== "dBm" || data.delta_unit !== "dB" || !Array.isArray(data.lines) || data.lines.length !== 3) {
      throw new Error("Ответ не соответствует зафиксированному свипу, области или методу трёх линий");
    }
    sourceValid(data.sweep, data.frequencies_hz, data.points);
    if (!equal(data.frequencies_hz, request.source.frequencies_hz) || !equal(data.sweep.levels, request.source.levels) ||
        !equal(data.sweep.settings, request.source.settings) || !sameInterval(data.sweep.interval, request.source.interval)) {
      throw new Error("Исходные данные ответа не совпадают с зафиксированным архивным свипом");
    }
    data.lines.forEach((line, index) => {
      if (!object(line) || line.id !== `line-${index + 1}` || !finite(line.level_dbm) || line.level_dbm !== request.levels[index] ||
          !Array.isArray(line.excess_levels) || line.excess_levels.length !== data.points || !line.excess_levels.every(finite) ||
          !Array.isArray(line.regions) || !Number.isSafeInteger(line.region_count) || line.region_count !== line.regions.length ||
          !Number.isSafeInteger(line.exceedance_points) || line.exceedance_points < 0 || line.exceedance_points > data.points) {
        throw new Error("Некорректная линия, уровень или сводка превышений");
      }
      // Check server arithmetic and region consistency; never run a separate detector.
      for (let i = 0; i < data.points; i++) {
        if (line.excess_levels[i] !== data.sweep.levels[i] - line.level_dbm) throw new Error("Несогласованное превышение линии");
      }
      let exceedance = 0, regionIndex = 0;
      for (let i = 0; i < data.points; i++) {
        if (line.excess_levels[i] <= 0) continue;
        const start = i; let peak = i;
        while (i + 1 < data.points && line.excess_levels[i + 1] > 0) {
          i++; if (line.excess_levels[i] > line.excess_levels[peak]) peak = i;
        }
        const region = line.regions[regionIndex++], count = i - start + 1;
        exceedance += count;
        if (!object(region) || region.start_index !== start || region.end_index !== i || region.peak_index !== peak ||
            region.start_frequency_hz !== data.frequencies_hz[start] || region.end_frequency_hz !== data.frequencies_hz[i] ||
            region.peak_frequency_hz !== data.frequencies_hz[peak] || region.peak_excess_db !== line.excess_levels[peak] || region.points !== count) {
          throw new Error("Некорректная область превышения или её первый пик");
        }
      }
      if (regionIndex !== line.regions.length || exceedance !== line.exceedance_points) throw new Error("Некорректное число превышений линии");
    });
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
      // Every status has the same streaming bound, and only HTTP 200 can become a result.
      if (response.status !== 200) {
        const detail = response.status === 404 ? "сервер не поддерживает три порога либо сеанс/свип не найден" :
          response.status === 409 ? "свип или уровни несовместимы с анализом трёх порогов" :
          response.status === 400 ? "сервер отклонил параметры" : response.status === 413 ? "превышен размер запроса" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const body = new Uint8Array(bytes); let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body));
    } finally {
      if (!complete) { controller.abort(); cancelReader(reader); }
      reader.releaseLock();
      if (pending?.reader === reader) pending.reader = null;
    }
  }
  function discard() {
    dom.summary.textContent = "";
    for (let i = 1; i <= 3; i++) {
      dom[`line-${i}-summary`].textContent = ""; dom[`line-${i}-regions`].replaceChildren();
    }
    dom.canvas.hidden = true;
    dom.canvas.getContext("2d").clearRect(0, 0, dom.canvas.width, dom.canvas.height);
  }
  function latest() { return sweeps.filter(item => item.level_unit === "dBm").at(-1); }
  function controls() {
    const source = latest();
    dom.run.disabled = suspended || !connected || Boolean(pending) || !source || !snapshot.sessionId || !snapshot.streamId;
    dom.source.textContent = source ? `Для следующего анализа: ${source.id} · последний архивный dBm свип выбранного потока.` : "Нет архивного dBm свипа выбранного потока.";
  }
  function clear() {
    cancel(); discard();
    dom.status.textContent = "Анализ только по кнопке. Новые свипы не пересчитывают зафиксированный результат.";
    controls();
  }
  function update(next) {
    const nextScope = JSON.stringify([window.location.origin, next.sessionId, next.streamId]);
    snapshot = { sessionId: next.sessionId, streamId: next.streamId };
    sweeps = (next.sweeps || []).filter(item => item.stream_id === next.streamId);
    if (scope !== nextScope) { scope = nextScope; clear(); }
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) {
      cancel(); discard();
      dom.status.textContent = "Нет соединения с ядром. После подключения повторите анализ по кнопке.";
    }
    controls();
  }
  const colors = ["#ffd36e", "#f59a7a", "#b99aff"];
  const dashes = [[10, 4], [3, 4], [10, 3, 2, 3]];
  function draw(data) {
    const canvas = dom.canvas, ctx = canvas.getContext("2d"), width = canvas.width, height = canvas.height;
    ctx.fillStyle = "#07131c"; ctx.fillRect(0, 0, width, height);
    const left = 82, right = width - 24, top = 65, bottom = height - 40;
    const levels = data.lines.map(line => line.level_dbm);
    const low = Math.min(...data.sweep.levels, ...levels), high = Math.max(...data.sweep.levels, ...levels);
    const scale = Math.max(1, Math.abs(low), Math.abs(high));
    const pad = Math.max(0.08, (high / scale - low / scale) * 0.08), minimum = low / scale - pad, maximum = high / scale + pad;
    const first = data.frequencies_hz[0], last = data.frequencies_hz.at(-1);
    const x = frequency => left + (last === first ? 0.5 : (frequency - first) / (last - first)) * (right - left);
    const y = level => bottom - (level / scale - minimum) / (maximum - minimum) * (bottom - top);
    ctx.font = "12px monospace"; ctx.fillStyle = "#a3bac8";
    ctx.fillText(`${high} dBm`, 4, top); ctx.fillText(`${low} dBm`, 4, bottom);
    ctx.fillText(`${first} Гц`, left, height - 12); ctx.textAlign = "right"; ctx.fillText(`${last} Гц`, right, height - 12); ctx.textAlign = "left";
    // All raw bins are drawn, including repeated frequencies; no client resampling.
    ctx.strokeStyle = "#60e4ef"; ctx.fillStyle = "#60e4ef"; ctx.lineWidth = 2; ctx.setLineDash([]);
    ctx.fillText("Исходный свип · dBm · квадрат", left, 20); ctx.beginPath();
    data.sweep.levels.forEach((level, index) => {
      if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level)); else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
    });
    ctx.stroke();
    // A small source square and nested rings keep all four identities visible even at equal levels.
    ctx.strokeRect(x(first) - 2, y(data.sweep.levels[0]) - 2, 4, 4);
    data.lines.forEach((line, index) => {
      ctx.strokeStyle = colors[index]; ctx.fillStyle = colors[index]; ctx.lineWidth = 2; ctx.setLineDash(dashes[index]);
      ctx.fillText(`${line.id} · dBm · кольцо ${3 - index}`, left + index * 280, 42);
      ctx.beginPath(); ctx.moveTo(left, y(line.level_dbm)); ctx.lineTo(right, y(line.level_dbm)); ctx.stroke();
      ctx.setLineDash([]); ctx.beginPath(); ctx.arc(x(first), y(line.level_dbm), 11 - index * 3, 0, Math.PI * 2); ctx.stroke();
    });
    ctx.setLineDash([]); canvas.hidden = false;
    canvas.setAttribute("aria-label", `Исходный свип ${data.sweep.id}, все ${data.points} точек и три горизонтальные линии, dBm. Совпадающие линии обозначены вложенными кольцами, исходный свип квадратом.`);
  }
  function render(data) {
    dom.summary.textContent = `Зафиксирован свип: ${data.sweep.id}\nСеанс: ${data.session_id} · поток: ${data.stream_id}\nUTC: ${interval(data.sweep.interval)}\nЗаписанные настройки: ${JSON.stringify(data.sweep.settings)}\nВсе ${data.points} точек; исходный свип и три независимых горизонтальных порога: dBm. Сравнение строго >; равенство не является превышением.`;
    const coincident = data.lines.filter((line, index, all) => all.some((other, j) => j !== index && other.level_dbm === line.level_dbm));
    if (coincident.length) dom.summary.textContent += `\nСовпадающие линии: ${coincident.map(line => line.id).join(", ")}. Цвета, штрихи и вложенные кольца сохраняют идентичность линий.`;
    data.lines.forEach(line => {
      dom[`${line.id}-summary`].textContent = `${line.id}: ${line.level_dbm} dBm · превышение Δ = исходный уровень − ${line.id}, dB.\nПревышений (> линии): ${line.exceedance_points}; непрерывных областей по индексам: ${line.region_count}. Показаны первые ${Math.min(200, line.region_count)} из ${line.region_count} областей.`;
      dom[`${line.id}-regions`].replaceChildren(...line.regions.slice(0, 200).map(region => {
        const item = document.createElement("li");
        item.textContent = `Индексы ${region.start_index}–${region.end_index} (включительно, с 0) · ${region.start_frequency_hz}–${region.end_frequency_hz} Гц · ${region.points} точек · пик превышения ${region.peak_excess_db} dB @ ${region.peak_frequency_hz} Гц (первый индекс ${region.peak_index}).`;
        return item;
      }));
    });
    draw(data);
  }
  async function analyze() {
    if (suspended || !connected || pending || !latest() || !snapshot.sessionId || !snapshot.streamId) return;
    cancel(); discard();
    let request, body;
    try {
      const source = latest();
      sourceValid(source, source.frequencies_hz, source.levels?.length);
      request = { ...snapshot, source: clone(source), levels: parameters() };
      body = JSON.stringify({ sweep_id: request.source.id, levels_dbm: request.levels });
      if (new TextEncoder().encode(body).length > 8192) throw new Error("Запрос превышает предел 8192 байт UTF-8");
    } catch (error) { dom.status.textContent = error.message; controls(); return; }
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      cancel(); dom.status.textContent = "Три порога недоступны: таймаут 8 секунд. Повторите по кнопке."; controls();
    }, 8000);
    pending = { controller, timer }; controls(); dom.status.textContent = `Анализ свипа ${request.source.id}…`;
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-three-thresholds`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, cache: "no-store", signal: controller.signal, body,
      });
      if (current !== generation) { if (response.body?.cancel) cancelReader(response.body); return; }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      render(validate(payload.data, request));
      dom.status.textContent = "Результат ядра зафиксирован. Три независимых ручных порога, без общего OR-счётчика, автоматического детектора или записи измерений. Это не нормативные пределы и не калибровка.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Три порога недоступны: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer); if (current === generation) { pending = null; controls(); }
    }
  }
  dom.run.addEventListener("click", analyze); dom.clear.addEventListener("click", clear);
  for (let i = 1; i <= 3; i++) dom[`level-${i}`].addEventListener("input", () => {
    clear(); dom.status.textContent = "Уровни изменены. Запустите новый анализ по кнопке.";
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepThreeThresholds = { update, setConnected };
  clear();
})();
