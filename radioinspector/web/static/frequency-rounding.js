(() => {
  "use strict";

  // Read-only presentation and verification of a core result. Never tune a device here.
  const dom = Object.fromEntries(["candidate", "run", "clear", "editor", "file", "template", "save", "draft", "source", "status", "summary"]
    .map(name => [name, document.getElementById(`rounding-${name}`)]));
  if (!dom.status) return;
  const MAX_RESPONSE_BYTES = 2000000, MAX_PROFILE_BYTES = 65536;
  let scope = "", snapshot = {}, candidates = [], chosen = null, pending = null, result = null, resultDraft = null;
  let generation = 0, fileGeneration = 0, connected = true, ready = false, suspended = false;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const clone = value => JSON.parse(JSON.stringify(value));
  const unicode = value => typeof value === "string" &&
    !Array.from(value).some(character => character.length === 1 && /[\uD800-\uDFFF]/.test(character));
  // Exact Python str.strip() whitespace set. JS \s additionally contains FEFF,
  // which is a valid exact ID/unit character here (but remains forbidden in names).
  const scopeIdentity = value => unicode(value) && !/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(value);
  const identity = value => scopeIdentity(value) && [...value].length <= 128;
  const integer = value => Number.isSafeInteger(value) && value >= 0;
  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  }
  const keys = (value, expected) => object(value) && equal(Object.keys(value).sort(), expected.slice().sort());
  function utc(value) {
    const match = typeof value === "string" && /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(?:Z|\+00:00)$/.exec(value);
    const date = match && new Date(`${match[1]}Z`);
    if (!match || match[0] !== value || match[1].startsWith("0000-") || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 19) !== match[1]) {
      throw new Error("Некорректное время UTC");
    }
    return `${match[1]}.${(match[2] || "").padEnd(6, "0")}`;
  }
  function sourceSubset(source, exact = false) {
    const fields = ["id", "source_stream_id", "interval", "center_frequency_hz", "level_unit"];
    if (!object(source) || (exact && !keys(source, fields)) || !identity(source.id) || !scopeIdentity(source.source_stream_id) ||
        !integer(source.center_frequency_hz) || source.center_frequency_hz < 1 || !scopeIdentity(source.level_unit) ||
        !keys(source.interval, ["started_at", "ended_at"]) || utc(source.interval.started_at) > utc(source.interval.ended_at)) {
      throw new Error("Некорректное происхождение частоты кандидата");
    }
    return clone(Object.fromEntries(fields.map(key => [key, source[key]])));
  }
  function sameSource(a, b) {
    return ["id", "source_stream_id", "center_frequency_hz", "level_unit"].every(key => a[key] === b[key]) &&
      utc(a.interval.started_at) === utc(b.interval.started_at) && utc(a.interval.ended_at) === utc(b.interval.ended_at);
  }
  // JSON.parse checks grammar; the string-aware scan rejects duplicate keys and
  // non-integer number spellings before JS can lose their original JSON type.
  function strictJSON(raw) {
    const parsed = JSON.parse(raw), stack = [];
    for (const [token] of raw.matchAll(/"(?:\\[\s\S]|[^"\\])*"|[{}\[\],:]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g)) {
      if (token === "{") stack.push({ keys: new Set(), key: true });
      else if (token === "[") stack.push(null);
      else if (token === "}" || token === "]") stack.pop();
      else if (token === "," && stack.at(-1)) stack.at(-1).key = true;
      else if (token[0] === '"' && stack.at(-1)?.key) {
        const frame = stack.at(-1), key = JSON.parse(token);
        if (frame.keys.has(key)) throw new Error("Повторный ключ JSON");
        frame.keys.add(key); frame.key = false;
      } else if (/^-?\d/.test(token) && (!/^-?(?:0|[1-9]\d*)$/.test(token) || !Number.isSafeInteger(Number(token)))) {
        throw new Error("Числа профиля и ответа должны быть безопасными целыми JSON");
      }
    }
    return parsed;
  }
  // Mathematical floor and ceiling, including negative indices. Floating point
  // division near MAX_SAFE_INTEGER can otherwise choose the wrong grid point.
  const floorDiv = (a, b) => a / b - (a % b < 0n ? 1n : 0n);
  const ceilDiv = (a, b) => -floorDiv(-a, b);
  function bounds(rule) {
    const start = BigInt(rule.start_frequency_hz), end = BigInt(rule.end_frequency_hz);
    const origin = BigInt(rule.origin_frequency_hz), step = BigInt(rule.step_hz);
    return { start, end, origin, step, low: ceilDiv(start - origin, step), high: floorDiv(end - origin, step) };
  }
  function artifact(profile, canonical = true) {
    if (!keys(profile, ["format", "format_version", "name", "frequency_unit", "method", "tie_break", "rules"]) ||
        profile.format !== "openradioinspector-frequency-rounding" || profile.format_version !== 1 ||
        profile.frequency_unit !== "Hz" || profile.method !== "nearest_grid" || profile.tie_break !== "lower" ||
        !unicode(profile.name) || /\p{C}/u.test(profile.name)) throw new Error("Некорректный профиль округления");
    // Python's strip treats these Unicode whitespace characters as whitespace;
    // BOM and C* are rejected above rather than silently stripped.
    const name = profile.name.normalize("NFC").trim();
    if ([...name].length < 1 || [...name].length > 80 || (canonical && name !== profile.name) ||
        !Array.isArray(profile.rules) || profile.rules.length < 1 || profile.rules.length > 128) throw new Error("Некорректное имя или число правил");
    for (const [i, rule] of profile.rules.entries()) {
      if (!keys(rule, ["start_frequency_hz", "end_frequency_hz", "step_hz", "origin_frequency_hz", "max_offset_hz"]) ||
          !Object.values(rule).every(integer) || rule.step_hz < 1 || rule.start_frequency_hz > rule.end_frequency_hz ||
          (i && rule.start_frequency_hz <= profile.rules[i - 1].end_frequency_hz)) throw new Error("Некорректные или пересекающиеся правила профиля");
      const grid = bounds(rule);
      if (grid.low > grid.high) throw new Error("В диапазоне нет точки сетки");
    }
    return { ...profile, name };
  }
  function validate(data, request) {
    if (!keys(data, ["session_id", "candidate", "profile", "frequency_unit", "method", "tie_break", "result"]) ||
        data.session_id !== request.sessionId || data.frequency_unit !== "Hz" || data.method !== "nearest_grid" || data.tie_break !== "lower") {
      throw new Error("Ответ округления не соответствует выбранному сеансу или методу");
    }
    const source = sourceSubset(data.candidate, true);
    if (!sameSource(source, request.source)) throw new Error("Исходное происхождение частоты не совпадает с выбранным кандидатом");
    const profile = artifact(data.profile), requested = artifact(strictJSON(request.profileJson), false);
    if (!equal(profile, requested)) throw new Error("Профиль ответа не совпадает с отправленным черновиком");
    const value = data.result;
    if (!keys(value, ["status", "rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"])) throw new Error("Некорректные поля результата округления");
    const frequency = source.center_frequency_hz;
    const index = profile.rules.findIndex(rule => rule.start_frequency_hz <= frequency && frequency <= rule.end_frequency_hz);
    if (index < 0) {
      if (value.status !== "outside_ranges" || ["rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"].some(key => value[key] !== null)) {
        throw new Error("Неверный результат вне диапазонов");
      }
    } else {
      const rule = profile.rules[index], { origin, step, low, high } = bounds(rule), f = BigInt(frequency);
      const clamp = k => k < low ? low : k > high ? high : k, below = floorDiv(f - origin, step);
      const left = origin + clamp(below) * step, right = origin + clamp(below + 1n) * step;
      const abs = n => n < 0n ? -n : n, nearest = abs(left - f) <= abs(right - f) ? left : right, offset = nearest - f;
      const status = abs(offset) > BigInt(rule.max_offset_hz) ? "outside_tolerance" : offset === 0n ? "unchanged" : "rounded";
      if (!integer(value.rule_index) || value.rule_index !== index || !integer(value.nearest_frequency_hz) ||
          value.nearest_frequency_hz !== Number(nearest) || !Number.isSafeInteger(value.offset_hz) || value.offset_hz !== Number(offset) ||
          value.status !== status || value.rounded_frequency_hz !== (status === "outside_tolerance" ? null : Number(nearest))) {
        throw new Error("Несогласованные точка сетки, допуск или решение ядра");
      }
    }
    return data;
  }
  function cancelReader(reader) {
    try { Promise.resolve(reader.cancel()).catch(() => {}); } catch (_) { /* Already closed. */ }
  }
  function cancel() {
    generation++; fileGeneration++;
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
      if (length != null && Number(length) > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает предел 2 000 000 байт");
      const chunks = []; let bytes = 0;
      while (true) {
        const part = await reader.read();
        if (current !== generation) return null;
        if (part.done) { complete = true; break; }
        bytes += part.value.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает предел 2 000 000 байт");
        chunks.push(part.value);
      }
      // Bound all statuses before any decoding. No partial success or retries.
      if (response.status !== 200) {
        const detail = response.status === 404 ? "сервер не поддерживает округление либо сеанс/кандидат не найден" :
          response.status === 409 ? "источник несовместим" : response.status === 400 ? "сервер отклонил JSON профиля или ID" :
          response.status === 413 ? "превышен размер запроса" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const body = new Uint8Array(bytes); let offset = 0;
      for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength; }
      return strictJSON(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(body));
    } finally {
      if (!complete) { controller.abort(); cancelReader(reader); }
      reader.releaseLock();
      if (pending?.reader === reader) pending.reader = null;
    }
  }
  function discard() {
    result = null; resultDraft = null; dom.save.disabled = true; dom.summary.textContent = "";
    dom.draft.textContent = "Черновик не проверен ядром. Импорт и редактирование остаются локальными.";
  }
  function controls() {
    dom.run.disabled = suspended || !connected || !ready || Boolean(pending) || !chosen || !snapshot.sessionId;
    dom.candidate.disabled = suspended || !connected || !ready || (!candidates.length && !chosen);
    dom.save.disabled = !result || suspended || !connected || !ready;
    dom.template.disabled = suspended;
    dom.file.disabled = suspended;
    dom.source.textContent = chosen ? `Выбран: ${chosen.id}\nПоток: ${chosen.source_stream_id} · исходная частота ${chosen.center_frequency_hz} Гц · исходные единицы уровня ${chosen.level_unit}\nUTC: ${chosen.interval.started_at} … ${chosen.interval.ended_at}\nВыбор сохранён локально для явной повторной загрузки.` : "Явно выберите кандидата текущего сеанса, независимо от спектрального потока.";
  }
  function clear() {
    cancel(); discard();
    dom.status.textContent = "Расчёт только по кнопке. Исходный кандидат, handoff и прибор не изменяются.";
    controls();
  }
  function update(next) {
    const nextScope = JSON.stringify([window.location.origin, next.sessionId]);
    if (scope !== nextScope) { scope = nextScope; chosen = null; ready = false; clear(); }
    snapshot = { sessionId: next.sessionId };
    candidates = (next.candidates || []).filter(item => object(item) && (!Object.hasOwn(item, "session_id") || item.session_id === next.sessionId));
    // Only an actual completed snapshot may restore readiness after a failure.
    // Rendering an old cached snapshot and reconnecting an SSE are not success.
    if (next.fresh === true) ready = true;
    const choices = candidates.map(item => chosen?.id === item.id ? chosen : item);
    if (chosen && !choices.some(item => item.id === chosen.id)) choices.push(chosen);
    const placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "Выберите кандидата";
    dom.candidate.replaceChildren(placeholder, ...choices.map(item => {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${item.id} · ${item.center_frequency_hz} Гц · ${item.level_unit} · ${item.source_stream_id}${item === chosen ? " · сохранённый выбор" : ""}`;
      return option;
    }));
    dom.candidate.value = chosen?.id || "";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) {
      ready = false; chosen = null; clear(); dom.candidate.value = "";
      dom.status.textContent = "Нет актуального снимка ядра. Дождитесь успешного обновления и выберите кандидата заново.";
    }
    controls();
  }
  function render(data, raw) {
    result = data; resultDraft = raw;
    const value = data.result, rule = value.rule_index === null ? null : data.profile.rules[value.rule_index];
    const shown = frequency => frequency === null ? "—" : `${frequency} Гц`;
    const decisions = { rounded: "Округлено (производное предложение)", unchanged: "Без изменения", outside_tolerance: "Отклонено: вне допуска", outside_ranges: "Вне диапазонов профиля" };
    dom.draft.textContent = `Проверен ядром профиль «${data.profile.name}» для кандидата ${data.candidate.id}. Экспорт относится только к этому результату.`;
    dom.summary.textContent = `Зафиксирован кандидат: ${data.candidate.id}\nСеанс: ${data.session_id} · поток: ${data.candidate.source_stream_id}\nUTC: ${data.candidate.interval.started_at} … ${data.candidate.interval.ended_at}\nПрофиль: ${data.profile.name}\nИсходная частота: ${shown(data.candidate.center_frequency_hz)}\nБлижайшая точка сетки: ${shown(value.nearest_frequency_hz)}\nОтклонение (ближайшая − исходная): ${value.offset_hz === null ? "—" : `${value.offset_hz >= 0 ? "+" : ""}${value.offset_hz} Гц`}\nДопуск: ${rule ? `±${rule.max_offset_hz} Гц (включительно), правило ${value.rule_index + 1}` : "—"}\nРешение: ${decisions[value.status]} (${value.status})\nПринятое производное значение: ${shown(value.rounded_frequency_hz)}\nИсходные единицы уровня: ${data.candidate.level_unit}. Уровень не преобразуется; кандидат и прибор не изменены.`;
  }
  async function analyze() {
    if (suspended || !connected || !ready || pending || !chosen || !snapshot.sessionId) return;
    cancel(); discard(); let request;
    try {
      if (!scopeIdentity(snapshot.sessionId) || candidates.filter(item => item.id === chosen.id).length > 1) throw new Error("Некорректная или неоднозначная область/ID кандидата");
      const profileJson = dom.editor.value;
      if (!profileJson.trim()) throw new Error("Введите или загрузите JSON профиля");
      if (!unicode(profileJson) || new TextEncoder().encode(profileJson).length > MAX_PROFILE_BYTES) throw new Error("Профиль превышает 65 536 байт UTF-8 либо содержит некорректный Unicode");
      request = { ...snapshot, profileJson, source: sourceSubset(chosen) };
    } catch (error) { dom.status.textContent = error.message; controls(); return; }
    const current = generation, controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      clear(); dom.status.textContent = "Округление недоступно: таймаут 8 секунд. Повторите по кнопке.";
    }, 8000);
    pending = { controller, timer }; controls(); dom.status.textContent = `Расчёт кандидата ${request.source.id}…`;
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/candidate-frequency-rounding`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" }, cache: "no-store", signal: controller.signal,
        body: JSON.stringify({ candidate_id: request.source.id, profile_json: request.profileJson }),
      });
      if (current !== generation) { if (response.body?.cancel) cancelReader(response.body); return; }
      const payload = await readPayload(response, controller, current);
      if (current !== generation) return;
      if (!keys(payload, ["contract", "contract_version", "data"]) || payload.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      if (dom.editor.value !== request.profileJson) throw new Error("Черновик изменён во время запроса");
      render(validate(payload.data, request), request.profileJson);
      dom.status.textContent = "Результат ядра зафиксирован. Профиль не является нормативом или разрешением использовать частоту. Нет записи или команды прибору.";
    } catch (error) {
      if (current === generation) { discard(); dom.status.textContent = `Округление недоступно: ${error.message}. Остальные панели продолжают работать.`; }
    } finally {
      window.clearTimeout(timer); if (current === generation) { pending = null; controls(); }
    }
  }
  function edited() { clear(); dom.status.textContent = "Черновик изменён. Проверка и расчёт только по кнопке."; }
  dom.run.addEventListener("click", analyze); dom.clear.addEventListener("click", clear); dom.editor.addEventListener("input", edited);
  dom.candidate.addEventListener("change", () => {
    const id = dom.candidate.value, source = candidates.find(item => item.id === id);
    try {
      chosen = !suspended && connected && ready && source ? sourceSubset(source) : chosen?.id === id && ready && connected ? chosen : null;
    } catch (_) { chosen = null; }
    clear(); dom.status.textContent = "Выбор кандидата изменён. Рассчитайте предложение по кнопке.";
  });
  dom.template.addEventListener("click", () => {
    if (suspended) return;
    dom.editor.value = JSON.stringify({ format: "openradioinspector-frequency-rounding", format_version: 1,
      name: "НЕ НОРМАТИВ: пример округления", frequency_unit: "Hz", method: "nearest_grid", tie_break: "lower", rules: [
        { start_frequency_hz: 100000000, end_frequency_hz: 110000000, step_hz: 100000, origin_frequency_hz: 0, max_offset_hz: 30000 },
        { start_frequency_hz: 140000000, end_frequency_hz: 150000000, step_hz: 12500, origin_frequency_hz: 0, max_offset_hz: 6250 },
      ] }, null, 2);
    edited();
  });
  dom.file.addEventListener("change", async () => {
    const file = dom.file.files?.[0]; dom.file.value = "";
    if (!file || suspended) return;
    clear(); const current = fileGeneration;
    dom.status.textContent = "Чтение локального UTF-8 файла…";
    try {
      if (!Number.isSafeInteger(file.size) || file.size < 0 || file.size > MAX_PROFILE_BYTES) throw new Error("Файл превышает 65 536 байт");
      const bytes = await file.slice(0, MAX_PROFILE_BYTES + 1).arrayBuffer();
      if (current !== fileGeneration || suspended) return;
      if (bytes.byteLength !== file.size || bytes.byteLength > MAX_PROFILE_BYTES) throw new Error("Файл прочитан не полностью или превышает 65 536 байт");
      dom.editor.value = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      dom.status.textContent = "Локальный файл загружен как непроверенный черновик. Расчёт только по кнопке.";
    } catch (error) {
      if (current === fileGeneration) dom.status.textContent = `Не удалось загрузить профиль: ${error.message}. Прежний черновик сохранён.`;
    }
  });
  dom.save.addEventListener("click", () => {
    if (!result || suspended || !connected || !ready) return;
    if (dom.editor.value !== resultDraft) { edited(); return; }
    const current = generation, saved = result; let url, link;
    try {
      const canonical = JSON.stringify(saved.profile, null, 2) + "\n";
      url = URL.createObjectURL(new Blob([canonical], { type: "application/json;charset=utf-8" }));
      link = document.createElement("a"); link.href = url; link.download = "frequency-rounding-profile.json";
      document.body.appendChild(link);
      if (current !== generation || result !== saved || suspended || !connected || !ready) return;
      if (dom.editor.value !== resultDraft) { edited(); return; }
      link.click();
      if (current === generation && result === saved) dom.status.textContent = `Передан браузеру проверенный профиль «${saved.profile.name}». Завершение сохранения определяется браузером.`;
    } catch (error) { if (current === generation) dom.status.textContent = `Не удалось сохранить профиль: ${error.message}`; }
    finally { link?.remove(); if (url) window.setTimeout(() => URL.revokeObjectURL(url), 0); }
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; setConnected(false); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioFrequencyRounding = { update, setConnected };
  clear();
})();
