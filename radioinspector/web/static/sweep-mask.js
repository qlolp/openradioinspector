(() => {
  "use strict";

  // Presentation and consistency checks only. Mask parsing and interpolation live in the core.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "editor", "file", "template", "save", "draft", "source", "status", "summary", "regions", "canvas"]
    .map(name => [name, byId(`mask-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = {}, sweeps = [], pending = null, generation = 0;
  let suspended = false, connected = true, result = null, fileGeneration = 0;
  const MAX_RESPONSE_BYTES = 2000000, MAX_MASK_BYTES = 65536;
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
        !Number.isSafeInteger(points) || points < 1 || points > 16384 ||
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
  const keys = (value, expected) => object(value) && equal(Object.keys(value).sort(), expected.slice().sort());
  function artifact(mask, canonical = true) {
    if (!keys(mask, ["format", "format_version", "name", "frequency_unit", "level_unit", "interpolation", "vertices"]) ||
        mask.format !== "openradioinspector-spectral-mask" || mask.format_version !== 1 ||
        mask.frequency_unit !== "Hz" || mask.level_unit !== "dBm" || mask.interpolation !== "linear_dbm" ||
        typeof mask.name !== "string" || /\p{C}/u.test(mask.name)) throw new Error("Некорректный артефакт маски");
    const name = mask.name.normalize("NFC").trim();
    if ([...name].length < 1 || [...name].length > 80 || (canonical && name !== mask.name) ||
        !Array.isArray(mask.vertices) || mask.vertices.length < 2 || mask.vertices.length > 128 ||
        !mask.vertices.every((vertex, i) => keys(vertex, ["frequency_hz", "limit_dbm"]) &&
          Number.isSafeInteger(vertex.frequency_hz) && vertex.frequency_hz >= 0 && finite(vertex.limit_dbm) &&
          (!i || vertex.frequency_hz > mask.vertices[i - 1].frequency_hz))) throw new Error("Некорректные имя или вершины маски");
    return { ...mask, name };
  }
  function acceptedDraft(raw) {
    // Do not give duplicate members last-wins meaning even if a faulty server accepts them.
    // JSON.parse checks the grammar; this string-aware scan only checks member uniqueness.
    const parsed = JSON.parse(raw), stack = [];
    for (const [token] of raw.matchAll(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\],:]/g)) {
      if (token === "{") stack.push({ keys: new Set(), key: true });
      else if (token === "[") stack.push(null);
      else if (token === "}" || token === "]") stack.pop();
      else if (token === "," && stack.at(-1)) stack.at(-1).key = true;
      else if (token[0] === '"' && stack.at(-1)?.key) {
        const frame = stack.at(-1), key = JSON.parse(token);
        if (frame.keys.has(key)) throw new Error("Повторный ключ в черновике маски");
        frame.keys.add(key); frame.key = false;
      }
    }
    return artifact(parsed, false);
  }
  function validate(data, request) {
    if (!object(data) || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        data.sweep?.id !== request.source.id || data.level_unit !== "dBm" || data.delta_unit !== "dB" ||
        data.comparison !== "strictly_greater") {
      throw new Error("Ответ маски не соответствует выбранному свипу или области");
    }
    sourceValid(data.sweep, data.frequencies_hz, data.points);
    // Parse only AFTER the server has accepted the raw draft, never before transmission.
    const mask = artifact(data.mask), requested = acceptedDraft(request.maskJson);
    if (!equal(mask, requested) || mask.vertices[0].frequency_hz > data.frequencies_hz[0] ||
        mask.vertices.at(-1).frequency_hz < data.frequencies_hz.at(-1)) throw new Error("Маска ответа не совпадает с отправленным черновиком или не покрывает свип");
    if (!equal(data.frequencies_hz, request.source.frequencies_hz) || !equal(data.sweep.levels, request.source.levels) ||
        !equal(data.sweep.settings, request.source.settings) || interval(data.sweep) !== interval(request.source)) {
      throw new Error("Исходные данные ответа не совпадают с запрошенным архивным свипом");
    }
    for (const name of ["threshold_levels", "excess_levels"]) {
      if (!Array.isArray(data[name]) || data[name].length !== data.points || !data[name].every(finite)) {
        throw new Error("Некорректные производные уровни порога");
      }
    }
    // Validate arithmetic and region summaries, but never interpolate the mask in the client.
    let exceedance = 0, regionIndex = 0;
    if (!Array.isArray(data.regions) || !Number.isSafeInteger(data.region_count) || data.region_count !== data.regions.length ||
        !Number.isSafeInteger(data.exceedance_points) || data.exceedance_points < 0 || data.exceedance_points > data.points) {
      throw new Error("Некорректная сводка превышений");
    }
    for (let i = 0; i < data.points; i++) {
      if (data.excess_levels[i] !== data.sweep.levels[i] - data.threshold_levels[i]) {
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
    result = null; dom.save.disabled = true;
    dom.draft.textContent = "Черновик не проверен ядром. Импорт и редактирование остаются локальными.";
    dom.summary.textContent = "";
    dom.regions.replaceChildren();
    dom.canvas.hidden = true;
    dom.canvas.getContext("2d").clearRect(0, 0, dom.canvas.width, dom.canvas.height);
  }
  function latest() { return sweeps.filter(item => item.level_unit === "dBm").at(-1); }
  function controls() {
    const source = latest();
    dom.save.disabled = !result || suspended;
    dom.template.disabled = !source || source.frequencies_hz?.length < 2 || source.frequencies_hz?.[0] === source.frequencies_hz?.at(-1);
    dom.run.disabled = suspended || !connected || Boolean(pending) || !source || !snapshot.sessionId || !snapshot.streamId;
    dom.source.textContent = source ? `Для следующего анализа: ${source.id} · последний архивный dBm свип выбранного потока.` : "Нет архивного dBm свипа выбранного потока.";
  }
  function clear() {
    fileGeneration++; cancel(); discard();
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
      fileGeneration++; cancel(); discard();
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
      [data.threshold_levels, "#ffd36e", [7, 4], "Маска ядра · dBm", left + 240],
    ]) {
      ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2; ctx.setLineDash(dash);
      ctx.fillText(label, labelX, 20); ctx.beginPath();
      levels.forEach((level, index) => {
        if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level));
        else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
      });
      ctx.stroke();
      if (levels.length === 1) { ctx.beginPath(); ctx.arc(x(first), y(levels[0]), 3, 0, Math.PI * 2); ctx.fill(); }
    }
    ctx.setLineDash([]);
    canvas.hidden = false;
    canvas.setAttribute("aria-label", `Исходный свип и табличная маска, dBm: ${data.sweep.id}; ${data.points} точек; превышений ${data.exceedance_points}`);
  }
  function render(data) {
    result = data;
    dom.draft.textContent = `Проверена ядром маска «${data.mask.name}» для свипа ${data.sweep.id}. Сохранение относится к этому результату.`;
    dom.summary.textContent = `Зафиксирован свип: ${data.sweep.id}\nМаска: ${data.mask.name} · ${data.mask.vertices.length} вершин · linear_dbm\nСеанс: ${data.session_id} · поток: ${data.stream_id}\nUTC: ${interval(data.sweep)}\n${data.points} точек · исходные уровни и маска: dBm\nПревышений (> маски): ${data.exceedance_points}; непрерывных областей по индексам: ${data.region_count}.\nПоказаны первые ${Math.min(200, data.region_count)} из ${data.region_count} областей; график содержит все ${data.points} точек.`;
    dom.regions.replaceChildren(...data.regions.slice(0, 200).map(region => {
      const item = document.createElement("li");
      item.textContent = `Индексы ${region.start_index}–${region.end_index} (включительно, с 0) · ${region.start_frequency_hz}–${region.end_frequency_hz} Гц · ${region.points} точек · пик превышения ${region.peak_excess_db} dB @ ${region.peak_frequency_hz} Гц (индекс ${region.peak_index}).`;
      return item;
    }));
    draw(data);
  }
  async function analyze() {
    if (suspended || !connected || pending || !latest() || !snapshot.sessionId || !snapshot.streamId) return;
    fileGeneration++; cancel(); discard();
    let request;
    try {
      const source = latest();
      sourceValid(source, source.frequencies_hz, source.levels?.length);
      const maskJson = dom.editor.value;
      if (!maskJson.trim()) throw new Error("Введите или загрузите JSON маски");
      if (new TextEncoder().encode(maskJson).length > MAX_MASK_BYTES) throw new Error("Маска превышает 65 536 байт UTF-8");
      request = { ...snapshot, maskJson, source: JSON.parse(JSON.stringify(source)) };
    } catch (error) { dom.status.textContent = error.message; return; }
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      cancel();
      dom.status.textContent = "Маска недоступна: таймаут 8 секунд. Повторите по кнопке.";
      controls();
    }, 8000);
    pending = { controller, timer };
    dom.status.textContent = `Анализ свипа ${request.source.id}…`;
    controls();
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-mask-analysis`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ sweep_id: request.source.id, mask_json: request.maskJson }), cache: "no-store", signal: controller.signal,
      });
      if (current !== generation) return;
      if (response.status !== 200) {
        controller.abort(); // Do not consume an unbounded error/status response body.
        const detail = response.status === 404 ? "сервер не поддерживает анализ либо сеанс/свип не найден" :
          response.status === 409 ? "исходный свип несовместим с маской" :
          response.status === 400 ? "сервер отклонил JSON маски" : response.status === 413 ? "превышен размер запроса" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      const data = validate(payload.data, request);
      render(data);
      dom.status.textContent = "Результат ядра зафиксирован для указанного свипа. Это сравнение с заданной таблицей, не подтверждение соответствия нормативам.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Маска недоступна: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer);
      if (current === generation) { pending = null; controls(); }
    }
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  function edited() {
    clear(); dom.status.textContent = "Черновик изменён. Проверка и анализ только по кнопке.";
  }
  dom.editor.addEventListener("input", edited);
  dom.template.addEventListener("click", () => {
    const source = latest();
    try {
      sourceValid(source, source?.frequencies_hz, source?.levels?.length);
      const first = source.frequencies_hz[0], last = source.frequencies_hz.at(-1);
      if (first === last) throw new Error("Для примера нужны две различные частоты");
      dom.editor.value = JSON.stringify({ format: "openradioinspector-spectral-mask", format_version: 1,
        name: "НЕНОРМАТИВНЫЙ пример −80 dBm", frequency_unit: "Hz", level_unit: "dBm", interpolation: "linear_dbm",
        vertices: [{ frequency_hz: first, limit_dbm: -80 }, { frequency_hz: last, limit_dbm: -80 }] }, null, 2);
      edited();
    } catch (error) { dom.status.textContent = error.message; }
  });
  dom.file.addEventListener("change", async () => {
    const file = dom.file.files?.[0]; dom.file.value = "";
    if (!file || suspended) return; // File-picker cancellation leaves both draft and result intact.
    clear(); const current = fileGeneration;
    dom.status.textContent = "Чтение локального UTF-8 файла…";
    try {
      if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > MAX_MASK_BYTES) throw new Error("Файл превышает 65 536 байт");
      // Bounded local read; never upload or parse the draft on import.
      const bytes = await file.slice(0, MAX_MASK_BYTES + 1).arrayBuffer();
      if (current !== fileGeneration || suspended) return;
      if (bytes.byteLength !== file.size || bytes.byteLength > MAX_MASK_BYTES) throw new Error("Файл прочитан не полностью или превышает 65 536 байт");
      dom.editor.value = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      dom.status.textContent = "Локальный файл загружен как непроверенный черновик. Анализ только по кнопке.";
    } catch (error) {
      if (current === fileGeneration) dom.status.textContent = `Не удалось загрузить маску: ${error.message}. Прежний черновик сохранён.`;
    }
  });
  dom.save.addEventListener("click", () => {
    if (!result || suspended) return;
    let url, link;
    try {
      const canonical = JSON.stringify(result.mask, null, 2) + "\n";
      url = URL.createObjectURL(new Blob([canonical], { type: "application/json;charset=utf-8" }));
      link = document.createElement("a"); link.href = url; link.download = "spectral-mask.json";
      document.body.appendChild(link); link.click();
      dom.status.textContent = `Передана браузеру проверенная маска «${result.mask.name}» из результата свипа ${result.sweep.id}. Завершение сохранения определяется браузером.`;
    } catch (error) { dom.status.textContent = `Не удалось сохранить маску: ${error.message}`; }
    finally { link?.remove(); if (url) window.setTimeout(() => URL.revokeObjectURL(url), 0); }
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepMask = { update, setConnected };
  clear();
})();
