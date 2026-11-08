(() => {
  "use strict";

  // Presentation/consistency only. Equal-weight linear-power averaging lives in the core.
  const dom = Object.fromEntries(["count", "threshold", "minimumBins", "pin", "run", "clear", "source", "reference", "status", "summary", "overlay", "delta"]
    .map(name => [name, document.getElementById(`average-${name}`)]));
  dom.threshold = document.getElementById("average-detect-threshold");
  dom.minimumBins = document.getElementById("average-detect-bins");
  if (!dom.status) return;
  const MAX_RESPONSE_BYTES = 2000000;
  let scope = "", snapshot = {}, sweeps = [], reference = null, pending = null, generation = 0;
  let connected = true, suspended = false;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  // Match Python str.strip() exactly without changing the ID. JS trim() wrongly
  // rejects FEFF-only IDs and misses Python's U+001C..U+001F and U+0085 blanks.
  const identity = value => typeof value === "string" &&
    !/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(value) &&
    Array.from(value).length <= 128 &&
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
    if (!match || match[1].startsWith("0000-") || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== match[1]) {
      throw new Error("Некорректное время UTC");
    }
    return `${match[1]}.${(match[2] || "").padEnd(6, "0")}`;
  }
  function interval(value) {
    if (!object(value) || !equal(Object.keys(value).sort(), ["ended_at", "started_at"]) ||
        utc(value.started_at) > utc(value.ended_at)) throw new Error("Некорректный интервал UTC");
    return `${value.started_at} … ${value.ended_at}`;
  }
  function sameInterval(a, b) {
    interval(a); interval(b);
    return utc(a.started_at) === utc(b.started_at) && utc(a.ended_at) === utc(b.ended_at);
  }
  function envelope(sources) {
    return sources.reduce((range, source) => ({
      started_at: utc(source.interval.started_at) < utc(range.started_at) ? source.interval.started_at : range.started_at,
      ended_at: utc(source.interval.ended_at) > utc(range.ended_at) ? source.interval.ended_at : range.ended_at,
    }), { ...sources[0].interval });
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
  function compatible(sources) {
    const base = sources[0], comparable = settings => Object.fromEntries(Object.entries(settings).filter(([key]) => key !== "sequence"));
    for (const source of sources) {
      if (source.stream_id !== snapshot.streamId || !equal(source.frequencies_hz, base.frequencies_hz) ||
          !equal(comparable(source.settings), comparable(base.settings))) throw new Error("Свипы несовместимы: поток, сетка или записанные настройки");
    }
  }
  function count() {
    const value = Number(dom.count.value);
    if (!dom.count.value.trim() || !Number.isSafeInteger(value) || value < 2 || value > 16) throw new Error("N: целое число от 2 до 16");
    return value;
  }
  function validate(data, request) {
    const r = data?.reference, target = data?.target, ids = request.sources.map(item => item.id);
    if (!object(data) || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        !object(r) || r.method !== "mean_power" || r.count !== ids.length || !equal(r.source_ids, ids) ||
        !Array.isArray(r.sources) || r.sources.length !== ids.length || r.level_unit !== "dBm" ||
        !object(target) || target.id !== request.target.id || data.delta_unit !== "dB") {
      throw new Error("Ответ не соответствует закреплённым свипам, области или методу mean_power");
    }
    sourceValid(target, data.frequencies_hz, data.points);
    if (!equal(data.frequencies_hz, request.target.frequencies_hz) || !equal(target.levels, request.target.levels) ||
        !equal(target.settings, request.target.settings) || !sameInterval(target.interval, request.target.interval)) {
      throw new Error("Целевой свип ответа не совпадает с зафиксированным исходным свипом");
    }
    for (let i = 0; i < ids.length; i++) {
      const source = r.sources[i], frozen = request.sources[i];
      if (!object(source) || source.id !== frozen.id || !settingsValid(source.settings) ||
          !equal(source.settings, frozen.settings) || !sameInterval(source.interval, frozen.interval)) {
        throw new Error("Происхождение опорного не совпадает с закреплёнными свипами");
      }
    }
    if (!sameInterval(r.interval, envelope(request.sources))) throw new Error("Некорректный UTC интервал усреднённого опорного");
    for (const levels of [r.levels, data.delta_levels]) {
      if (!Array.isArray(levels) || levels.length !== data.points || !levels.every(finite)) throw new Error("Некорректные производные уровни");
    }
    // Bounds and subtraction are consistency checks, not a client-side measurement average.
    for (let i = 0; i < data.points; i++) {
      const levels = request.sources.map(source => source.levels[i]);
      if (r.levels[i] < Math.min(...levels) || r.levels[i] > Math.max(...levels) ||
          data.delta_levels[i] !== target.levels[i] - r.levels[i]) throw new Error("Несогласованные средние уровни или разность");
    }
    for (const [point, expected] of [[data.max_increase, Math.max(...data.delta_levels)], [data.min_change, Math.min(...data.delta_levels)]]) {
      const index = data.delta_levels.indexOf(expected);
      if (!object(point) || point.delta_db !== expected || point.frequency_hz !== data.frequencies_hz[index]) throw new Error("Некорректная сводка экстремумов");
    }
    return data;
  }
  function validateDetection(result, comparison, request) {
    const detection = result?.detection, keys = ["minimum_increase_db", "minimum_bins", "regions", "count"];
    if (!object(result) || !equal(Object.keys(result).sort(), ["comparison", "detection"]) ||
        !object(detection) || !equal(Object.keys(detection).sort(), keys.sort()) ||
        detection.minimum_increase_db !== request.minimumIncreaseDb ||
        detection.minimum_bins !== request.minimumBins || !Array.isArray(detection.regions) ||
        !Number.isSafeInteger(detection.count) || detection.count !== detection.regions.length) {
      throw new Error("Параметры или сводка обнаружения не совпадают с запросом");
    }
    const expected = []; let start = null;
    function append(end) {
      if (start === null || end - start + 1 < request.minimumBins) return;
      let peak = start;
      for (let i = start + 1; i <= end; i++) if (comparison.delta_levels[i] > comparison.delta_levels[peak]) peak = i;
      expected.push({
        start_index: start, end_index: end,
        start_frequency_hz: comparison.frequencies_hz[start],
        end_frequency_hz: comparison.frequencies_hz[end],
        peak_index: peak, peak_frequency_hz: comparison.frequencies_hz[peak],
        peak_increase_db: comparison.delta_levels[peak], bin_count: end - start + 1,
      });
    }
    for (let i = 0; i < comparison.points; i++) {
      if (comparison.delta_levels[i] >= request.minimumIncreaseDb) {
        if (start === null) start = i;
      } else if (start !== null) { append(i - 1); start = null; }
    }
    if (start !== null) append(comparison.points - 1);
    if (!equal(detection.regions, expected)) throw new Error("Области обнаружения не согласованы с разностью свипов");
    return detection;
  }
  function strictJSON(raw) {
    // The body is byte-bounded before decoding. JSON.parse checks grammar; the
    // iterative string-aware scan preserves wire integer types and key uniqueness
    // without the newer reviver context.source API (unavailable on some Pi browsers).
    const parsed = JSON.parse(raw), stack = [];
    const roles = new Map([
      ["envelope.data", "result"], ["result.comparison", "comparison"], ["result.detection", "detection"],
      ["comparison.frequencies_hz", "frequencies"], ["comparison.reference", "reference"],
      ["comparison.target", "source"], ["comparison.max_increase", "extremum"], ["comparison.min_change", "extremum"],
      ["reference.sources", "sources"], ["detection.regions", "regions"],
    ]);
    const integerFields = new Set([
      "envelope.contract_version", "comparison.points", "reference.count", "extremum.frequency_hz",
      "detection.minimum_bins", "detection.count", "region.start_index", "region.end_index", "region.peak_index",
      "region.start_frequency_hz", "region.end_frequency_hz", "region.peak_frequency_hz", "region.bin_count",
    ]);
    function valueRole(frame) {
      if (!frame) return "envelope";
      if (frame.object) return roles.get(`${frame.role}.${frame.member}`) || "";
      return frame.role === "sources" ? "source" : frame.role === "regions" ? "region" : "";
    }
    for (const [token] of raw.matchAll(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\],:]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null/g)) {
      const frame = stack.at(-1);
      if (token === "{" || token === "[") {
        stack.push({ role: valueRole(frame), object: token === "{", keys: new Set(), key: token === "{", member: null });
      } else if (token === "}" || token === "]") stack.pop();
      else if (token === "," && frame?.object) frame.key = true;
      else if (token[0] === '"' && frame?.key) {
        const key = JSON.parse(token);
        if (frame.keys.has(key)) throw new Error("Повторяющийся ключ JSON");
        frame.keys.add(key); frame.member = key; frame.key = false;
      } else if (/^-?\d/.test(token)) {
        const value = Number(token);
        if (!finite(value)) throw new Error("Неконечное число JSON");
        const integer = frame && ((!frame.object && frame.role === "frequencies") ||
          (frame.object && integerFields.has(`${frame.role}.${frame.member}`)));
        if (integer && (!/^-?(?:0|[1-9]\d*)$/.test(token) || !Number.isSafeInteger(value))) {
          throw new Error("Счётчики, индексы и частоты должны быть безопасными целыми JSON");
        }
      }
    }
    return parsed;
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
        const detail = response.status === 404 ? "сервер не поддерживает обнаружение изменений либо сеанс/свип не найден" :
          response.status === 409 ? "свипы несовместимы для усреднённого опорного" :
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
    dom.summary.textContent = "";
    for (const canvas of [dom.overlay, dom.delta]) {
      canvas.hidden = true; canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
    }
  }
  function choices() { return sweeps.filter(item => item.level_unit === "dBm"); }
  function latest() { return choices().at(-1); }
  function controls() {
    const source = latest(), available = choices().length;
    let n; try { n = count(); } catch (_) { /* Invalid input cannot enable pinning. */ }
    const disabled = suspended || !connected || !snapshot.sessionId || !snapshot.streamId;
    dom.pin.disabled = disabled || Boolean(pending) || !n || available < n + 1;
    dom.run.disabled = disabled || Boolean(pending) || !reference || !source || reference.sources.some(item => item.id === source.id);
    dom.source.textContent = `В полученном окне: ${available} dBm свипов. Для N=${n ?? "?"} нужно N+1; N не уменьшается автоматически. ` +
      (source ? `Следующая цель: ${source.id} · последний dBm свип выбранного потока.` : "Нет цели для сравнения.");
    dom.reference.textContent = reference ? `Локально закреплено N=${reference.sources.length} · mean_power · равные веса\nUTC охват: ${interval(reference.interval)}\n` +
      reference.sources.map(item => `${item.id} · UTC: ${interval(item.interval)}`).join("\n") : "Усреднённый опорный не закреплён.";
  }
  function clear() {
    cancel(); reference = null; discard();
    dom.status.textContent = "Закрепите N свипов перед последним, затем сравните по отдельной кнопке.";
    controls();
  }
  function pin() {
    if (suspended || !connected || pending || !snapshot.sessionId || !snapshot.streamId) return;
    cancel(); reference = null; discard();
    try {
      const n = count(), available = choices();
      if (available.length < n + 1) throw new Error(`Недостаточно dBm свипов: для N=${n} нужно не менее ${n + 1}; N не уменьшен`);
      const sources = available.slice(-n - 1, -1);
      sources.forEach(source => sourceValid(source, source.frequencies_hz, source.levels?.length));
      if (new Set([...sources.map(item => item.id), available.at(-1).id]).size !== n + 1) throw new Error("Повторяющиеся ID опорных или цели");
      compatible(sources);
      reference = { sources: clone(sources), interval: envelope(sources) };
      dom.status.textContent = "Свипы закреплены локально, без запроса и записи. Сравнение только по кнопке.";
    } catch (error) { dom.status.textContent = error.message; }
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
    if (!connected) { clear(); dom.status.textContent = "Нет соединения с ядром. После подключения заново закрепите опорный."; }
    controls();
  }
  function draw(canvas, data, traces, unit, label, includeZero = false, regions = []) {
    const ctx = canvas.getContext("2d"), width = canvas.width, height = canvas.height;
    ctx.fillStyle = "#07131c"; ctx.fillRect(0, 0, width, height);
    const left = 82, right = width - 24, top = 40, bottom = height - 40;
    const values = traces.flatMap(trace => trace.levels), low = Math.min(...values, ...(includeZero ? [0] : [])), high = Math.max(...values, ...(includeZero ? [0] : []));
    const scale = Math.max(1, Math.abs(low), Math.abs(high)), pad = Math.max(0.08, (high / scale - low / scale) * 0.08);
    const minimum = low / scale - pad, maximum = high / scale + pad;
    const first = data.frequencies_hz[0], last = data.frequencies_hz.at(-1);
    const x = frequency => left + (last === first ? 0.5 : (frequency - first) / (last - first)) * (right - left);
    const y = level => bottom - (level / scale - minimum) / (maximum - minimum) * (bottom - top);
    if (includeZero) {
      ctx.fillStyle = "rgba(140, 233, 154, 0.13)";
      for (const region of regions) {
        const x1 = x(data.frequencies_hz[region.start_index]), x2 = x(data.frequencies_hz[region.end_index]);
        ctx.fillRect(Math.min(x1, x2) - 2, top, Math.max(4, Math.abs(x2 - x1) + 4), bottom - top);
      }
    }
    ctx.font = "12px monospace"; ctx.fillStyle = "#a3bac8";
    ctx.fillText(`${high} ${unit}`, 4, top); ctx.fillText(`${low} ${unit}`, 4, bottom);
    ctx.fillText(`${first} Гц`, left, height - 12); ctx.textAlign = "right"; ctx.fillText(`${last} Гц`, right, height - 12); ctx.textAlign = "left";
    if (includeZero) {
      ctx.strokeStyle = "#68818f"; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(left, y(0)); ctx.lineTo(right, y(0)); ctx.stroke();
      ctx.fillText("0 dB", 4, y(0));
    }
    traces.forEach((trace, i) => {
      ctx.strokeStyle = trace.color; ctx.fillStyle = trace.color; ctx.lineWidth = 2; ctx.setLineDash(trace.dash);
      ctx.fillText(trace.label, left + i * 300, 20); ctx.beginPath();
      trace.levels.forEach((level, index) => {
        if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level)); else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
      });
      ctx.stroke();
      // Coincident single-bin/collapsed traces remain distinguishable: hollow square and smaller disc.
      if (data.points === 1 || (first === last && trace.levels.every(level => level === trace.levels[0]))) {
        ctx.setLineDash([]);
        if (i === 0) ctx.strokeRect(x(first) - 5, y(trace.levels[0]) - 5, 10, 10);
        else { ctx.beginPath(); ctx.arc(x(first), y(trace.levels[0]), 3, 0, Math.PI * 2); ctx.fill(); }
      }
    });
    ctx.setLineDash([]); canvas.hidden = false; canvas.setAttribute("aria-label", label);
  }
  const signed = value => `${value > 0 ? "+" : ""}${value}`;
  function render(result) {
    const data = result.comparison, detection = result.detection;
    const r = data.reference;
    dom.summary.textContent = `Сеанс: ${data.session_id} · поток: ${data.stream_id}\nОпорный: mean_power · N=${r.count} · равные веса\nUTC охват: ${interval(r.interval)}\n` +
      r.sources.map(source => `${source.id} · UTC: ${interval(source.interval)}`).join("\n") +
      `\nЦель: ${data.target.id} · UTC: ${interval(data.target.interval)}\nВсе ${data.points} точек: средняя мощность и цель в dBm; Δ = цель − опорный в dB.\n` +
      `Максимальное изменение: ${signed(data.max_increase.delta_db)} dB @ ${data.max_increase.frequency_hz} Гц; минимальное: ${signed(data.min_change.delta_db)} dB @ ${data.min_change.frequency_hz} Гц.\n` +
      `Обнаружение: прирост ≥ ${detection.minimum_increase_db} dB, ширина ≥ ${detection.minimum_bins} точек; областей: ${detection.count}.\n` +
      detection.regions.map(region => `${region.start_frequency_hz}–${region.end_frequency_hz} Гц · пик +${region.peak_increase_db} dB @ ${region.peak_frequency_hz} Гц · ${region.bin_count} точек`).join("\n");
    draw(dom.overlay, data, [
      { levels: r.levels, color: "#ffd36e", dash: [7, 4], label: "Средняя мощность · dBm" },
      { levels: data.target.levels, color: "#60e4ef", dash: [], label: "Цель · dBm" },
    ], "dBm", `Средняя мощность N=${r.count} и цель ${data.target.id}, dBm; все ${data.points} точек`);
    draw(dom.delta, data, [{ levels: data.delta_levels, color: "#f59a7a", dash: [], label: "Δ = цель − опорный · dB" }],
      "dB", `Разность цели ${data.target.id} и среднего опорного, dB; области обнаружения выделены; все ${data.points} точек`, true, detection.regions);
  }
  async function compare() {
    if (suspended || !connected || pending || !reference || !latest() || !snapshot.sessionId || !snapshot.streamId) return;
    cancel(); discard();
    let request, body;
    try {
      const target = latest();
      const minimumIncreaseDb = Number(dom.threshold.value), minimumBins = Number(dom.minimumBins.value);
      if (!dom.threshold.value.trim() || !finite(minimumIncreaseDb) || minimumIncreaseDb < 0 || minimumIncreaseDb > 120)
        throw new Error("Минимальный прирост должен быть конечным числом от 0 до 120 dB");
      if (!dom.minimumBins.value.trim() || !Number.isSafeInteger(minimumBins) || minimumBins < 1 || minimumBins > 16384)
        throw new Error("Минимальная ширина должна быть целым числом от 1 до 16384 точек");
      sourceValid(target, target.frequencies_hz, target.levels?.length);
      if (reference.sources.some(source => source.id === target.id)) throw new Error("Цель входит в закреплённые исходные свипы; выберите новый последний свип");
      compatible([...reference.sources, target]);
      request = { ...snapshot, sources: clone(reference.sources), target: clone(target), minimumIncreaseDb, minimumBins };
      body = JSON.stringify({ reference_ids: request.sources.map(source => source.id), target_id: request.target.id,
        minimum_increase_db: minimumIncreaseDb, minimum_bins: minimumBins });
      if (new TextEncoder().encode(body).length > 8192) throw new Error("Запрос превышает предел 8192 байт UTF-8");
    } catch (error) { dom.status.textContent = error.message; controls(); return; }
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      cancel(); dom.status.textContent = "Сравнение недоступно: таймаут 8 секунд. Повторите по кнопке."; controls();
    }, 8000);
    pending = { controller, timer }; controls(); dom.status.textContent = `Сравнение с целью ${request.target.id}…`;
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-average-change-detection`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, cache: "no-store", signal: controller.signal,
        body,
      });
      if (current !== generation) { if (response.body?.cancel) cancelReader(response.body); return; }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (!object(payload) || !equal(Object.keys(payload).sort(), ["contract", "contract_version", "data"]) ||
          payload.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      const comparison = validate(payload.data?.comparison, request);
      const detection = validateDetection(payload.data, comparison, request);
      render({ comparison, detection });
      dom.status.textContent = "Результат ядра зафиксирован. Области — read-only анализ по заданным параметрам; кандидаты и архив не изменяются.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Сравнение недоступно: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer); if (current === generation) { pending = null; controls(); }
    }
  }
  dom.pin.addEventListener("click", pin); dom.run.addEventListener("click", compare); dom.clear.addEventListener("click", clear);
  dom.count.addEventListener("input", () => { clear(); dom.status.textContent = "N изменено. Заново закрепите ровно N исходных свипов."; });
  for (const input of [dom.threshold, dom.minimumBins]) input.addEventListener("input", () => {
    cancel(); discard(); dom.status.textContent = "Параметры обнаружения изменены. Повторите анализ по кнопке."; controls();
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSweepAverage = { update, setConnected };
  clear();
})();
