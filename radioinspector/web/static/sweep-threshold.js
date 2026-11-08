(() => {
  "use strict";

  // Presentation and consistency checks only. The rolling median lives in the core.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "window", "margin", "source", "status", "summary", "regions", "canvas"]
    .map(name => [name, byId(`threshold-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = {}, sweeps = [], pending = null, generation = 0;
  let suspended = false, connected = true;
  const MAX_RESPONSE_BYTES = 2000000;
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const identity = value => typeof value === "string" && value.length > 0;
  const utc = value => {
    if (typeof value !== "string" || !/T.*(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) {
      throw new Error("Некорректное время UTC");
    }
    return new Date(value).toISOString();
  };
  function interval(sweep) {
    const start = utc(sweep.interval?.started_at), end = utc(sweep.interval?.ended_at);
    if (start > end) throw new Error("Некорректный интервал UTC");
    return `${start} … ${end}`;
  }
  function setting(value, allowNull = false) {
    if (value === null) return allowNull;
    if (typeof value === "number") return finite(value);
    if (["boolean", "string"].includes(typeof value)) return true;
    if (Array.isArray(value)) return value.every(item => setting(item, allowNull));
    return object(value) && Object.values(value).every(item => setting(item, allowNull));
  }
  function sourceValid(source, frequencies, points) {
    if (!object(source) || !identity(source.id) || source.level_unit !== "dBm" ||
        !Number.isSafeInteger(points) || points < 3 || points > 16384 ||
        !Array.isArray(frequencies) || frequencies.length !== points ||
        !frequencies.every((v, i) => Number.isSafeInteger(v) && v >= 0 && (!i || v >= frequencies[i - 1])) ||
        !Array.isArray(source.levels) || source.levels.length !== points || !source.levels.every(finite) ||
        !object(source.settings) || !Object.keys(source.settings).some(key => key !== "sequence") ||
        !Object.entries(source.settings).every(([key, value]) => setting(value, key === "sequence"))) {
      throw new Error("Некорректный исходный dBm свип, сетка или записанные настройки");
    }
    interval(source);
  }
  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  }
  function parameters() {
    const windowPoints = Number(dom.window.value), marginDb = Number(dom.margin.value);
    if (!dom.window.value.trim() || !Number.isSafeInteger(windowPoints) || windowPoints < 3 || windowPoints > 101 || windowPoints % 2 !== 1) {
      throw new Error("Окно: нечётное целое число от 3 до 101");
    }
    if (!dom.margin.value.trim() || !finite(marginDb) || marginDb < 0 || marginDb > 120) {
      throw new Error("Запас: конечное число от 0 до 120 dB");
    }
    return { windowPoints, marginDb };
  }
  function validate(data, request) {
    const p = data?.parameters;
    if (!object(data) || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        data.sweep?.id !== request.source.id || data.level_unit !== "dBm" || data.delta_unit !== "dB" ||
        !object(p) || p.method !== "rolling_median" || p.edge_policy !== "clip" || p.comparison !== "strictly_greater" ||
        p.window_points !== request.windowPoints || p.margin_db !== request.marginDb) {
      throw new Error("Ответ порога не соответствует выбранному свипу, области или параметрам");
    }
    sourceValid(data.sweep, data.frequencies_hz, data.points);
    if (!equal(data.frequencies_hz, request.source.frequencies_hz) || !equal(data.sweep.levels, request.source.levels) ||
        !equal(data.sweep.settings, request.source.settings) || interval(data.sweep) !== interval(request.source)) {
      throw new Error("Исходные данные ответа не совпадают с запрошенным архивным свипом");
    }
    for (const name of ["baseline_levels", "threshold_levels", "excess_levels"]) {
      if (!Array.isArray(data[name]) || data[name].length !== data.points || !data[name].every(finite)) {
        throw new Error("Некорректные производные уровни порога");
      }
    }
    // Validate arithmetic and region summaries, but never recompute the median.
    let exceedance = 0, regionIndex = 0;
    if (!Array.isArray(data.regions) || !Number.isSafeInteger(data.region_count) || data.region_count !== data.regions.length ||
        !Number.isSafeInteger(data.exceedance_points) || data.exceedance_points < 0 || data.exceedance_points > data.points) {
      throw new Error("Некорректная сводка превышений");
    }
    for (let i = 0; i < data.points; i++) {
      if (data.threshold_levels[i] !== data.baseline_levels[i] + p.margin_db ||
          data.excess_levels[i] !== data.sweep.levels[i] - data.threshold_levels[i]) {
        throw new Error("Несогласованные производные уровни порога");
      }
    }
    for (let i = 0; i < data.points; i++) {
      if (data.excess_levels[i] <= 0) continue;
      const start = i;
      let peak = i;
      while (i + 1 < data.points && data.excess_levels[i + 1] > 0) {
        i++;
        if (data.excess_levels[i] > data.excess_levels[peak]) peak = i;
      }
      const region = data.regions[regionIndex++], count = i - start + 1;
      exceedance += count;
      if (!object(region) || region.start_index !== start || region.end_index !== i || region.peak_index !== peak ||
          region.start_frequency_hz !== data.frequencies_hz[start] || region.end_frequency_hz !== data.frequencies_hz[i] ||
          region.peak_frequency_hz !== data.frequencies_hz[peak] || region.peak_excess_db !== data.excess_levels[peak] || region.points !== count) {
        throw new Error("Некорректная область превышения или её пик");
      }
    }
    if (regionIndex !== data.regions.length || exceedance !== data.exceedance_points) throw new Error("Некорректное число превышений");
    return data;
  }
  function cancel() {
    generation++;
    if (pending) {
      window.clearTimeout(pending.timer); pending.controller.abort();
      if (pending.reader) cancelReader(pending.reader);
      pending = null;
    }
  }
  function cancelReader(reader) {
    // Cancellation must never block the UI, even if a transport ignores abort.
    try { Promise.resolve(reader.cancel()).catch(() => {}); } catch (_) { /* Already closed. */ }
  }
  async function readPayload(response, controller, current) {
    if (!response.body?.getReader) throw new Error("Сервер не предоставил потоковый ответ");
    const reader = response.body.getReader();
    if (pending) pending.reader = reader;
    let complete = false;
    try {
      const length = response.headers?.get("Content-Length");
      if (length !== null && length !== undefined && Number(length) > MAX_RESPONSE_BYTES) {
        throw new Error("Ответ превышает предел 2 000 000 байт; данные не усечены");
      }
      const chunks = []; let bytes = 0;
      while (true) {
        const part = await reader.read();
        if (current !== generation) return null;
        if (part.done) { complete = true; break; }
        bytes += part.value.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает предел 2 000 000 байт; данные не усечены");
        chunks.push(part.value);
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
    dom.regions.replaceChildren();
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
  function draw(data) {
    const canvas = dom.canvas, ctx = canvas.getContext("2d"), width = canvas.width, height = canvas.height;
    ctx.fillStyle = "#07131c"; ctx.fillRect(0, 0, width, height);
    const left = 82, right = width - 24, top = 40, bottom = height - 40;
    const low = Math.min(...data.sweep.levels, ...data.threshold_levels), high = Math.max(...data.sweep.levels, ...data.threshold_levels);
    // Finite extreme levels must not overflow a plot coordinate.
    const scale = Math.max(1, Math.abs(low), Math.abs(high));
    const pad = Math.max(0.08, (high / scale - low / scale) * 0.08);
    const minimum = low / scale - pad, maximum = high / scale + pad;
    const first = data.frequencies_hz[0], last = data.frequencies_hz.at(-1);
    const x = frequency => left + (last === first ? 0.5 : (frequency - first) / (last - first)) * (right - left);
    const y = level => bottom - (level / scale - minimum) / (maximum - minimum) * (bottom - top);
    ctx.font = "12px monospace"; ctx.fillStyle = "#a3bac8";
    ctx.fillText(`${high} dBm`, 4, top); ctx.fillText(`${low} dBm`, 4, bottom);
    ctx.fillText(`${first} Гц`, left, height - 12);
    ctx.textAlign = "right"; ctx.fillText(`${last} Гц`, right, height - 12); ctx.textAlign = "left";
    for (const [levels, color, dash, label, labelX] of [
      [data.sweep.levels, "#60e4ef", [], "Исходный свип · dBm", left],
      [data.threshold_levels, "#ffd36e", [7, 4], "Порог ядра · dBm", left + 240],
    ]) {
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash);
      ctx.fillText(label, labelX, 20); ctx.beginPath();
      levels.forEach((level, index) => {
        if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level));
        else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
      });
      ctx.stroke();
    }
    ctx.setLineDash([]);
    canvas.hidden = false;
    canvas.setAttribute("aria-label", `Исходный свип и адаптивный порог, dBm: ${data.sweep.id}; ${data.points} точек; превышений ${data.exceedance_points}`);
  }
  function render(data) {
    const p = data.parameters;
    dom.summary.textContent = `Зафиксирован свип: ${data.sweep.id}\nСеанс: ${data.session_id} · поток: ${data.stream_id}\nUTC: ${interval(data.sweep)}\n${data.points} точек · окно ${p.window_points} · запас ${p.margin_db} dB · исходные уровни и порог: dBm\nПревышений (> порога): ${data.exceedance_points}; непрерывных областей по индексам: ${data.region_count}.`;
    if (data.region_count > 200) dom.summary.textContent += `\nНиже показаны первые 200 из ${data.region_count} областей; график содержит все ${data.points} точек.`;
    dom.regions.replaceChildren(...data.regions.slice(0, 200).map(region => {
      const item = document.createElement("li");
      item.textContent = `Индексы ${region.start_index}–${region.end_index} (включительно, с 0) · ${region.start_frequency_hz}–${region.end_frequency_hz} Гц · ${region.points} точек · пик превышения ${region.peak_excess_db} dB @ ${region.peak_frequency_hz} Гц (индекс ${region.peak_index}).`;
      return item;
    }));
    draw(data);
  }
  async function analyze() {
    if (suspended || !connected || pending || !latest() || !snapshot.sessionId || !snapshot.streamId) return;
    cancel(); discard();
    let request;
    try {
      const source = latest();
      sourceValid(source, source.frequencies_hz, source.levels?.length);
      request = { ...snapshot, ...parameters(), source: JSON.parse(JSON.stringify(source)) };
    } catch (error) { dom.status.textContent = error.message; return; }
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      cancel();
      dom.status.textContent = "Порог недоступен: таймаут 8 секунд. Повторите по кнопке.";
      controls();
    }, 8000);
    pending = { controller, timer };
    dom.status.textContent = `Анализ свипа ${request.source.id}…`;
    controls();
    try {
      const query = new URLSearchParams({ sweep_id: request.source.id, window_points: request.windowPoints, margin_db: request.marginDb });
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-threshold?${query}`, {
        method: "GET", headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal,
      });
      if (current !== generation) return;
      if (response.status !== 200) {
        controller.abort(); // Do not consume an unbounded error/status response body.
        const detail = response.status === 404 ? "сервер не поддерживает анализ либо сеанс/свип не найден" :
          response.status === 409 ? "исходный свип несовместим с анализом порога" :
          response.status === 400 ? "сервер отклонил параметры анализа" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      const data = validate(payload.data, request);
      render(data);
      dom.status.textContent = "Результат ядра зафиксирован для указанного свипа. Это локальная медиана, не калиброванный шум и не достоверность обнаружения.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Порог недоступен: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer);
      if (current === generation) { pending = null; controls(); }
    }
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  // Edits invalidate an in-flight request; an already rendered result retains its own parameters.
  for (const input of [dom.window, dom.margin]) input.addEventListener("input", () => {
    if (pending) { clear(); dom.status.textContent = "Параметры изменены. Запустите новый анализ по кнопке."; }
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepThreshold = { update, setConnected };
  clear();
})();
