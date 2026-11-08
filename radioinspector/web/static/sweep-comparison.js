(() => {
  "use strict";

  const byId = (id) => document.getElementById(id);
  const dom = {
    select: byId("comparison-reference-select"), pin: byId("comparison-pin"),
    compare: byId("comparison-run"), clear: byId("comparison-clear"),
    reference: byId("comparison-reference"), status: byId("comparison-status"),
    summary: byId("comparison-summary"), canvas: byId("comparison-canvas"),
    name: byId("comparison-name"), save: byId("comparison-save"), catalog: byId("comparison-catalog"),
    refresh: byId("comparison-refresh"), use: byId("comparison-use"), storage: byId("comparison-storage"),
    catalogStatus: byId("comparison-catalog-status"), catalogNotice: byId("comparison-catalog-notice"),
  };
  if (!dom.status) return;
  let scope = "", snapshot = {}, sweeps = [], reference = null;
  let generation = 0, pending = null, result = null, suspended = false;
  let catalogGeneration = 0, catalogPending = null, catalogItems = [], catalogKnown = false, uncertainSave = false;
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const utc = (value) => {
    const date = new Date(value);
    if (typeof value !== "string" || !/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || Number.isNaN(date.getTime())) throw new Error("Некорректное время UTC");
    return date.toISOString();
  };
  const interval = (sweep) => {
    const started = utc(sweep.interval?.started_at), ended = utc(sweep.interval?.ended_at);
    if (started > ended) throw new Error("Некорректный интервал UTC");
    return `${started} … ${ended}`;
  };
  const description = (sweep) => `${sweep.id} · ${interval(sweep)} · dBm`;

  function cancel() {
    generation += 1;
    if (pending) {
      window.clearTimeout(pending.timer);
      pending.controller.abort();
      pending = null;
    }
  }

  function discardResult() {
    result = null;
    dom.summary.textContent = "";
    dom.canvas.hidden = true;
    dom.canvas.getContext("2d").clearRect(0, 0, dom.canvas.width, dom.canvas.height);
  }

  function controls() {
    dom.pin.disabled = suspended || !sweeps.some((sweep) => sweep.level_unit === "dBm");
    dom.clear.disabled = !reference;
    dom.compare.disabled = suspended || Boolean(pending) || !reference || !sweeps.length;
    dom.select.disabled = dom.pin.disabled;
    catalogControls();
    if (!reference) dom.reference.textContent = "Опорный свип не закреплён.";
    else {
      const outside = !sweeps.some((sweep) => sweep.id === reference.id);
      dom.reference.textContent = `Опорный: ${reference.name ? reference.name + " · " : ""}${description(reference)}${outside ? " · вне окна последних 40; сохранён по ID в архиве" : ""}`;
    }
  }

  function clear() {
    cancel();
    reference = null;
    discardResult();
    dom.status.textContent = "Закрепите архивный свип, затем явно сравните его с последним свипом потока.";
    controls();
  }

  // The ordinary snapshot lifecycle only supplies choices. It never starts a comparison.
  function update(next) {
    const nextScope = JSON.stringify([window.location.origin, next.sessionId, next.streamId]);
    snapshot = { sessionId: next.sessionId, streamId: next.streamId };
    if (scope !== nextScope) {
      scope = nextScope;
      clear();
      resetCatalog();
      if (!suspended && snapshot.sessionId) refreshCatalog();
    }
    sweeps = (next.sweeps || []).filter((sweep) => sweep.stream_id === next.streamId);
    const selected = dom.select.value;
    const choices = sweeps.filter((sweep) => sweep.level_unit === "dBm");
    const options = choices.slice().reverse().map((sweep) => new Option(description(sweep), sweep.id));
    if (!options.length) options.push(new Option("Нет архивных dBm свипов", ""));
    dom.select.replaceChildren(...options);
    dom.select.value = choices.some((sweep) => sweep.id === selected) ? selected : (choices.at(-1)?.id || "");
    controls();
  }

  function pin() {
    if (suspended) return;
    const sweep = sweeps.find((item) => item.id === dom.select.value && item.level_unit === "dBm");
    if (!sweep) return;
    cancel();
    // Keep identity and UTC interval even after the bounded snapshot window moves on.
    reference = { id: sweep.id, interval: { ...sweep.interval } };
    discardResult();
    dom.status.textContent = "Опорный закреплён. Сравнение выполняется только по кнопке; исходные измерения не меняются.";
    controls();
  }

  // The catalog has an independent lifecycle: local pin/clear never causes a POST.
  // Generations invalidate presentation only. Aborting a save cannot undo its server write.
  function cancelCatalog(notify = true) {
    catalogGeneration += 1;
    if (catalogPending) {
      if (notify && catalogPending.method === "POST" && dom.catalogNotice) {
        dom.catalogNotice.textContent = "Ожидание сохранения прекращено. Запись могла сохраниться на прежнем сервере в прежнем сеансе; вернитесь к нему и обновите каталог.";
      }
      window.clearTimeout(catalogPending.timer);
      catalogPending.controller.abort();
      catalogPending = null;
    }
  }

  function invalidateCatalog() {
    catalogKnown = false; catalogItems = [];
    dom.storage.textContent = "Хранилище каталога: ещё не проверено.";
    renderCatalog();
  }

  function resetCatalog() {
    cancelCatalog();
    catalogItems = []; catalogKnown = false; uncertainSave = false;
    if (!dom.catalog) return;
    dom.name.value = "";
    dom.storage.textContent = "Хранилище каталога: ещё не проверено.";
    dom.catalogStatus.textContent = "Выберите сеанс. Каталог обновляется при смене области и по кнопке.";
    renderCatalog();
  }

  function normalizedName(value) {
    if (typeof value !== "string") throw new Error("Имя должно быть строкой");
    const normalized = value.normalize("NFC");
    if (/\p{C}/u.test(normalized)) throw new Error("Имя не должно содержать управляющие и служебные символы");
    const name = normalized.replace(/^[\p{Z}]+|[\p{Z}]+$/gu, "");
    if (!name || Array.from(name).length > 80) {
      throw new Error("Имя: 1–80 символов, без управляющих и служебных символов");
    }
    return name;
  }

  function validateReference(record, request) {
    if (!record || typeof record !== "object" || Array.isArray(record) ||
        record.session_id !== request.sessionId ||
        (request.streamId != null && record.stream_id !== request.streamId) ||
        ![record.id, record.stream_id, record.sweep_id].every(value => typeof value === "string" && value.length > 0) ||
        record.level_unit !== "dBm" || !Number.isSafeInteger(record.points) || record.points < 1 || record.points > 16384 ||
        normalizedName(record.name) !== record.name) throw new Error("Некорректная запись каталога");
    for (const value of [record.created_at, record.interval?.started_at, record.interval?.ended_at]) {
      if (typeof value !== "string" || !/(?:Z|\+00:00)$/.test(value)) throw new Error("Некорректное время UTC каталога");
      utc(value);
    }
    interval(record);
    return record;
  }

  function validateCatalog(data, request) {
    if (!data || data.session_id !== request.sessionId || data.stream_id !== (request.streamId ?? null) ||
        !["sqlite", "memory"].includes(data.storage_mode) || typeof data.persistent !== "boolean" ||
        (data.storage_mode === "memory" && data.persistent) ||
        data.limit !== 100 || !Array.isArray(data.items) || data.items.length > 100) {
      throw new Error("Некорректный ответ каталога");
    }
    const ids = new Set(), names = new Set();
    for (const item of data.items) {
      validateReference(item, request);
      if (ids.has(item.id) || names.has(item.name)) throw new Error("Повторяющиеся записи каталога");
      ids.add(item.id); names.add(item.name);
    }
    return data;
  }

  function catalogControls() {
    if (!dom.catalog) return;
    const busy = suspended || Boolean(catalogPending), selected = catalogItems.find(item => item.id === dom.catalog.value);
    dom.refresh.disabled = busy || !snapshot.sessionId;
    dom.catalog.disabled = busy || !catalogItems.length;
    dom.use.disabled = busy || !selected || selected.stream_id !== snapshot.streamId;
    dom.name.disabled = busy;
    dom.save.disabled = busy || !catalogKnown || uncertainSave || !reference || !snapshot.sessionId;
  }

  function renderCatalog(selectedId = dom.catalog?.value) {
    if (!dom.catalog) return;
    const options = catalogItems.map(item => new Option(`${item.name} · ${item.sweep_id} · ${interval(item)} · dBm · ${item.points} точек`, item.id));
    if (!options.length) options.push(new Option(catalogKnown ? "Нет сохранённых опорных в этой области" : "Каталог не загружен", ""));
    dom.catalog.replaceChildren(...options);
    dom.catalog.value = catalogItems.some(item => item.id === selectedId) ? selectedId : (catalogItems[0]?.id || "");
    catalogControls();
  }

  function useSaved() {
    if (suspended || catalogPending) return;
    const record = catalogItems.find(item => item.id === dom.catalog.value && item.stream_id === snapshot.streamId);
    if (!record) return;
    cancel(); discardResult();
    reference = { id: record.sweep_id, interval: { ...record.interval }, name: record.name };
    dom.status.textContent = "Сохранённый опорный закреплён по ID свипа из архива. Сравнение выполняется по кнопке.";
    controls();
  }

  async function catalogRequest(method, body = null) {
    if (!dom.catalog || suspended || catalogPending || !snapshot.sessionId) return;
    const request = { ...snapshot }, current = ++catalogGeneration;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== catalogGeneration) return;
      // Timeout is not evidence that a POST failed to commit.
      if (method === "POST") uncertainSave = true;
      cancelCatalog(false);
      if (method === "GET") invalidateCatalog();
      dom.catalogStatus.textContent = method === "POST" ?
        "Результат сохранения неизвестен: таймаут 8 секунд. Автоповтора нет; обновите каталог перед новым сохранением." :
        "Каталог недоступен: таймаут 8 секунд. Повторите обновление по кнопке.";
      catalogControls();
    }, 8000);
    catalogPending = { controller, timer, method };
    dom.catalogStatus.textContent = method === "POST" ? "Сохранение закреплённого опорного…" : "Загрузка каталога…";
    catalogControls();
    let definiteRejection = false;
    try {
      const query = method !== "GET" || request.streamId == null ? "" : `?${new URLSearchParams({ stream_id: request.streamId })}`;
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-references${query}`, {
        method, headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}), cache: "no-store", signal: controller.signal,
      });
      if (current !== catalogGeneration) return;
      if (!response.ok || (method === "GET" ? response.status !== 200 : ![200, 201].includes(response.status))) {
        definiteRejection = [400, 404, 409].includes(response.status);
        let code = "";
        try { const error = await response.json(); code = error?.error?.code || ""; } catch (_) { /* HTTP status remains useful. */ }
        if (current !== catalogGeneration) return;
        const details = { reference_name_conflict: "это имя уже относится к другому свипу; выберите другое имя",
          reference_limit_reached: "достигнут предел 100 записей на сеанс", comparison_incompatible: "свип несовместим с сохранением опорного" };
        const message = details[code] || (response.status === 404 ? "сервер не поддерживает каталог либо сеанс/свип не найден; локальное закрепление доступно" : `HTTP ${response.status}`);
        throw new Error(message);
      }
      const payload = await response.json();
      if (current !== catalogGeneration) return;
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API каталога");
      if (method === "GET") {
        const data = validateCatalog(payload.data, request);
        catalogItems = data.items; catalogKnown = true; uncertainSave = false;
        dom.storage.textContent = data.persistent ? "SQLite: каталог сохраняется после перезапуска сервера." :
          `${data.storage_mode === "sqlite" ? "SQLite в памяти" : "Память"}: временный каталог, теряется после перезапуска сервера.`;
        renderCatalog();
        dom.catalogStatus.textContent = `Каталог загружен: ${catalogItems.length} в выбранной области; не более 100 на сеанс.`;
      } else {
        const record = validateReference(payload.data, request);
        if (record.name !== body.name || record.sweep_id !== body.sweep_id) throw new Error("Сохранённая запись не соответствует запросу");
        if (catalogItems.some(item => item.id !== record.id && item.name === record.name)) throw new Error("Конфликт ответа каталога");
        const next = catalogItems.filter(item => item.id !== record.id).concat(record);
        if (next.length > 100) throw new Error("Каталог превышает предел");
        catalogItems = next;
        renderCatalog(record.id);
        dom.catalogStatus.textContent = `Сохранено: ${record.name}. Исходный свип не изменён.`;
      }
    } catch (error) {
      if (current !== catalogGeneration) return;
      if (method === "POST" && !definiteRejection) {
        uncertainSave = true;
        dom.catalogStatus.textContent = `Результат сохранения неизвестен: ${error.message}. Автоповтора нет; обновите каталог перед новым сохранением.`;
      } else {
        if (method === "GET") invalidateCatalog();
        dom.catalogStatus.textContent = `Каталог недоступен: ${error.message}. Локальное закрепление и сравнение доступны.`;
      }
    } finally {
      window.clearTimeout(timer);
      if (current === catalogGeneration) { catalogPending = null; catalogControls(); }
    }
  }

  function refreshCatalog() { return catalogRequest("GET"); }

  function saveReference() {
    if (suspended || catalogPending || !catalogKnown || uncertainSave || !reference) return;
    let name;
    try { name = normalizedName(dom.name.value); }
    catch (error) { dom.catalogStatus.textContent = error.message; return; }
    const existing = catalogItems.find(item => item.name === name);
    if (existing) {
      dom.catalogStatus.textContent = existing.sweep_id === reference.id ? `Уже сохранено: ${name}.` : "Это имя уже относится к другому свипу; выберите другое имя.";
      renderCatalog(existing.id);
      return;
    }
    return catalogRequest("POST", { name, sweep_id: reference.id });
  }

  function validSetting(value, allowNull = false) {
    if (value === null) return allowNull;
    if (typeof value === "number") return finite(value);
    if (["string", "boolean"].includes(typeof value)) return true;
    if (Array.isArray(value)) return value.every((item) => validSetting(item, allowNull));
    return value && typeof value === "object" && Object.values(value).every((item) => validSetting(item, allowNull));
  }

  function validate(data, request) {
    if (!data || data.session_id !== request.sessionId || data.stream_id !== request.streamId ||
        data.reference?.id !== request.referenceId || data.target?.id !== request.targetId || data.delta_unit !== "dB") {
      throw new Error("Ответ сравнения не соответствует выбранным свипам");
    }
    if (!Number.isSafeInteger(data.points) || data.points < 1 || data.points > 16384 ||
        !Array.isArray(data.frequencies_hz) || data.frequencies_hz.length !== data.points ||
        !data.frequencies_hz.every((value, i, values) => Number.isSafeInteger(value) && value >= 0 && (!i || value >= values[i - 1])) ||
        !Array.isArray(data.delta_levels) || data.delta_levels.length !== data.points || !data.delta_levels.every(finite)) {
      throw new Error("Некорректная сетка или дельта сравнения");
    }
    for (const sweep of [data.reference, data.target]) {
      if (sweep.level_unit !== "dBm" || !Array.isArray(sweep.levels) || sweep.levels.length !== data.points || !sweep.levels.every(finite)) {
        throw new Error("Некорректные исходные уровни сравнения");
      }
      interval(sweep);
      const settings = sweep.settings;
      if (!settings || typeof settings !== "object" || Array.isArray(settings) ||
          !Object.keys(settings).some((key) => key !== "sequence") ||
          !Object.entries(settings).every(([key, value]) => validSetting(value, key === "sequence"))) {
        throw new Error("Некорректные записанные настройки сравнения");
      }
    }
    // Check presentation consistency against the server's delta, never subtract source levels.
    for (const [point, expected] of [[data.max_increase, Math.max(...data.delta_levels)],
                                    [data.min_change, Math.min(...data.delta_levels)]]) {
      const index = data.delta_levels.indexOf(expected);
      if (!point || point.delta_db !== expected || point.frequency_hz !== data.frequencies_hz[index]) {
        throw new Error("Некорректная сводка сравнения");
      }
    }
    return data;
  }

  function draw(data) {
    const canvas = dom.canvas, ctx = canvas.getContext("2d");
    const width = canvas.width, height = canvas.height;
    ctx.fillStyle = "#07131c";
    ctx.fillRect(0, 0, width, height);
    const left = 74, right = width - 24, top = 24, bottom = height - 40;
    const low = Math.min(0, ...data.delta_levels), high = Math.max(0, ...data.delta_levels);
    // Normalize before subtraction: finite ±1e308 values must not overflow the range.
    const scale = Math.max(1, Math.abs(low), Math.abs(high));
    const padding = Math.max(0.08, (high / scale - low / scale) * 0.08);
    const minimum = low / scale - padding, maximum = high / scale + padding;
    const first = data.frequencies_hz[0], last = data.frequencies_hz.at(-1);
    const x = (frequency) => left + (last === first ? 0.5 : (frequency - first) / (last - first)) * (right - left);
    const y = (value) => bottom - (value / scale - minimum) / (maximum - minimum) * (bottom - top);
    ctx.font = "12px monospace";
    ctx.fillStyle = "#a3bac8";
    ctx.fillText(`${high.toFixed(2)} dB`, 4, y(high));
    if (low !== high) ctx.fillText(`${low.toFixed(2)} dB`, 4, y(low));
    ctx.fillText(`${first} Гц`, left, height - 12);
    ctx.textAlign = "right";
    ctx.fillText(`${last} Гц`, right, height - 12);
    ctx.textAlign = "left";
    ctx.strokeStyle = "#68818f";
    ctx.beginPath(); ctx.moveTo(left, y(0)); ctx.lineTo(right, y(0)); ctx.stroke();
    ctx.fillText("0 dB", 4, y(0));
    ctx.strokeStyle = "#ffd36e";
    ctx.lineWidth = 2;
    ctx.beginPath();
    data.delta_levels.forEach((level, index) => {
      if (index) ctx.lineTo(x(data.frequencies_hz[index]), y(level));
      else ctx.moveTo(x(data.frequencies_hz[index]), y(level));
    });
    ctx.stroke();
    if (data.points === 1) {
      ctx.fillStyle = "#ffd36e"; ctx.fillRect(x(first) - 3, y(data.delta_levels[0]) - 3, 6, 6);
    }
    canvas.hidden = false;
    canvas.setAttribute("aria-label", `Изменение target − reference, dB: ${data.reference.id} → ${data.target.id}`);
  }

  async function compare() {
    if (suspended || pending || !reference || !sweeps.length) return;
    const target = sweeps.at(-1);
    const request = { ...snapshot, referenceId: reference.id, targetId: target.id };
    cancel();
    discardResult();
    const current = generation;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (current !== generation) return;
      cancel(); // Invalidates even a transport that completes after abort.
      dom.status.textContent = "Сравнение недоступно: таймаут запроса. Повторите по кнопке.";
      controls();
    }, 8000);
    pending = { controller, timer };
    dom.status.textContent = `Сравнение ${request.referenceId} → ${request.targetId}…`;
    controls();
    try {
      const query = new URLSearchParams({ reference_id: request.referenceId, target_id: request.targetId });
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(request.sessionId)}/sweep-comparison?${query}`, {
        method: "GET", headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) {
        const detail = response.status === 409 ? "свипы несовместимы (сетка, единицы или настройки)" :
          response.status === 404 ? "сеанс или свип больше не найден в архиве" : `HTTP ${response.status}`;
        throw new Error(detail);
      }
      const payload = await response.json();
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) throw new Error("Неподдерживаемый контракт API");
      const data = validate(payload.data, request);
      if (current !== generation) return;
      result = data;
      const maximum = data.max_increase, minimum = data.min_change;
      dom.summary.textContent = `Опорный: ${description(data.reference)}\nСравниваемый: ${description(data.target)}\nΔ = сравниваемый − опорный · ${data.points} точек · dB\nМаксимальное изменение: ${maximum.delta_db} dB @ ${maximum.frequency_hz} Гц; минимальное: ${minimum.delta_db} dB @ ${minimum.frequency_hz} Гц.`;
      dom.status.textContent = "Сравнение получено от ядра. Это зафиксированная пара, не автоматическое сравнение с новыми свипами.";
      draw(result);
    } catch (error) {
      if (current === generation) dom.status.textContent = `Сравнение недоступно: ${error.message}. Остальные панели продолжают работать.`;
    } finally {
      window.clearTimeout(timer);
      if (current === generation) { pending = null; controls(); }
    }
  }

  dom.pin.addEventListener("click", pin);
  dom.clear.addEventListener("click", clear);
  dom.compare.addEventListener("click", compare);
  if (dom.catalog) {
    dom.save.addEventListener("click", saveReference);
    dom.refresh.addEventListener("click", refreshCatalog);
    dom.use.addEventListener("click", useSaved);
    dom.catalog.addEventListener("change", catalogControls);
  }
  window.addEventListener("pagehide", () => { suspended = true; clear(); resetCatalog(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); if (snapshot.sessionId) refreshCatalog(); });
  window.OpenRadioSweepComparison = { update };
  clear();
})();
