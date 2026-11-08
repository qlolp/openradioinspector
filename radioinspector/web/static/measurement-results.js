(() => {
  "use strict";

  // Presentation and consistency checks only. The methodology catalog and the
  // result shape are validated by the core; values stay apart from the code.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "save", "methodology", "code",
    "frequency", "started", "ended", "values", "source", "status", "summary", "items"]
    .map(name => [name, byId(`meas-${name}`)]));
  if (!dom.status) return;
  let scope = "", stream = "", snapshot = "", connected = true, suspended = false, result = null;
  let catalog = [], results = [], generation = 0, pending = null, catalogLoading = null, listing = null;
  const MAX_RESPONSE_BYTES = 2000000;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  };
  const utc = value => {
    if (typeof value !== "string" || !/T.*(?:Z|\+00:00)$/.test(value) || !Number.isFinite(Date.parse(value))) {
      throw new Error("Некорректное время UTC");
    }
    return new Date(value).toISOString();
  };
  function entry(id) {
    return catalog.find(item => item.methodology === id) || null;
  }
  function chosenValues(spec, code) {
    const source = dom.values.value.trim();
    if (code === "not_measured") {
      // The code alone decides "no values": a non-empty draft is refused, not dropped.
      if (!source || source === "{}") return {};
      throw new Error("Код not_measured не допускает значений результата");
    }
    if (!source) throw new Error("Укажите значения результата или код not_measured");
    let parsed;
    try { parsed = JSON.parse(source); } catch { throw new Error("Значения должны быть JSON-объектом"); }
    if (!object(parsed)) throw new Error("Значения должны быть JSON-объектом");
    const names = spec.fields;
    if (!equal(Object.keys(parsed).sort(), names.slice().sort())) {
      throw new Error(`Методика требует ровно поля: ${names.join(", ")}`);
    }
    for (const name of names) {
      if (!finite(parsed[name])) throw new Error(`Значение ${name} должно быть конечным числом`);
    }
    return parsed;
  }
  function request() {
    const spec = entry(dom.methodology.value);
    if (!spec) throw new Error("Выберите методику из каталога");
    const code = dom.code.value;
    if (!spec.result_codes.includes(code)) throw new Error("Некорректный код результата");
    const frequency = Number(dom.frequency.value);
    if (!Number.isSafeInteger(frequency) || frequency < 0) throw new Error("Частота: целое число Гц");
    utc(dom.started.value); utc(dom.ended.value);
    if (utc(dom.started.value) > utc(dom.ended.value)) throw new Error("Интервал: начало позже окончания");
    return {
      stream_id: stream, methodology: spec.methodology, result_code: code, frequency_hz: frequency,
      interval: { started_at: dom.started.value, ended_at: dom.ended.value },
      values: chosenValues(spec, code),
    };
  }
  function validate(record) {
    const spec = entry(record.methodology);
    if (!object(record) || record.session_id !== scope || !spec || record.stream_id == null) {
      throw new Error("Ответ не соответствует выбранной методике или сеансу");
    }
    if (!spec.result_codes.includes(record.result_code) || !Number.isSafeInteger(record.frequency_hz)) {
      throw new Error("Некорректный код результата или частота ответа");
    }
    utc(record.interval.started_at); utc(record.interval.ended_at);
    if (record.result_code === "not_measured") {
      if (!equal(record.values, {})) throw new Error("not_measured не должен нести значения");
    } else {
      if (!equal(Object.keys(record.values).sort(), spec.fields.slice().sort()) ||
          !Object.values(record.values).every(finite)) {
        throw new Error("Значения ответа не совпадают с методикой");
      }
    }
  }
  function controls() {
    const ready = Boolean(scope && stream) && connected && !suspended && catalog.length > 0;
    dom.methodology.disabled = !ready || Boolean(pending);
    dom.run.disabled = !ready || Boolean(pending);
    dom.save.disabled = !result || suspended;
  }
  function clear() {
    pending?.abort(); pending = null; generation += 1; result = null;
    results = []; catalogLoading = null; listing = null;
    dom.summary.textContent = ""; dom.status.textContent = "";
    dom.items.replaceChildren();
    controls();
  }
  function render() {
    const rows = results.map(record => {
      const line = document.createElement("li");
      const spec = entry(record.methodology);
      const values = Object.entries(record.values)
        .map(([name, value]) => `${name}=${value}`).join(", ") || "без значений";
      line.textContent = `[${record.methodology}] ${record.result_code} · ${record.frequency_hz} Гц · ${values}`
        + (spec ? ` · ${spec.title}` : "");
      return line;
    });
    dom.items.replaceChildren(...rows);
    dom.summary.textContent = results.length
      ? `Результатов в сеансе: ${results.length}. Методика указана явно; код результата отделён от значений.`
      : "Результатов измерений в сеансе нет.";
  }
  async function loadCatalog() {
    const current = generation;
    try {
      const response = await fetch("/api/v1/measurement-catalog", { cache: "no-store" });
      const envelope = await response.json();
      if (!response.ok) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      if (!Array.isArray(envelope.data) || !envelope.data.length) throw new Error("Пустой каталог методик");
      for (const item of envelope.data) {
        if (!object(item) || typeof item.methodology !== "string" || !Array.isArray(item.fields) ||
            !Array.isArray(item.result_codes)) throw new Error("Некорректный каталог методик");
      }
      if (current !== generation || suspended || !connected) return false;
      catalog = envelope.data;
      const options = catalog.map(item => {
        const option = document.createElement("option");
        option.value = item.methodology;
        option.textContent = `${item.methodology} · ${item.title}`;
        return option;
      });
      dom.methodology.replaceChildren(...options);
      syncCodes(); controls();
      return true;
    } catch (error) {
      if (current !== generation) return false;
      dom.status.textContent = `Каталог методик недоступен: ${error.message}`;
      return false;
    }
  }
  function syncCodes() {
    const spec = entry(dom.methodology.value);
    const codes = spec ? spec.result_codes : [];
    const options = codes.map(code => {
      const option = document.createElement("option");
      option.value = code;
      option.textContent = code;
      return option;
    });
    dom.code.replaceChildren(...options);
  }
  async function analyze() {
    if (pending || dom.run.disabled) return;
    let body;
    try { body = request(); } catch (error) { clear(); dom.status.textContent = error.message; return; }
    const controller = new AbortController(); pending = controller; const current = ++generation;
    dom.run.disabled = true;
    dom.status.textContent = "Запись результата выполняется ядром…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/measurement-results`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body), cache: "no-store", signal: controller.signal,
      });
      const raw = await response.text();
      if (raw.length > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает безопасный размер");
      const envelope = JSON.parse(raw);
      if (response.status !== 201) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      validate(envelope.data);
      if (current !== generation || suspended) return;
      result = envelope.data;
      results = [...results.filter(item => item.id !== result.id), result];
      render();
      dom.status.textContent = `Результат ${result.id} записан: ${result.methodology} / ${result.result_code}. Значения хранятся отдельно от кода результата.`;
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Не удалось записать результат: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  async function refresh() {
    if (!scope || suspended || !connected || listing || pending || !catalog.length) return;
    const current = generation, token = {}; listing = token;
    const session = scope;
    try {
      const response = await fetch(
        `/api/v1/sessions/${encodeURIComponent(session)}/measurement-results?limit=100`, { cache: "no-store" });
      const envelope = await response.json();
      if (current !== generation || suspended || !connected) return;
      if (!response.ok) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform" || !Array.isArray(envelope.data)) throw new Error("Некорректный список результатов");
      for (const record of envelope.data) validate(record);
      results = envelope.data;
      render();
    } catch (error) {
      if (current === generation) dom.status.textContent = `Список результатов недоступен: ${error.message}`;
    } finally { if (listing === token) listing = null; }
  }
  async function ensureCatalogAndRefresh() {
    if (!scope || !connected || suspended) return;
    const current = generation;
    if (!catalog.length) {
      if (!catalogLoading) catalogLoading = loadCatalog();
      if (!await catalogLoading || current !== generation) return;
      catalogLoading = null;
    }
    return refresh();
  }
  function update(next) {
    const sessionId = next.sessionId || "", streamId = next.streamId || "";
    const signature = JSON.stringify([sessionId, streamId]);
    if (signature === snapshot) { controls(); ensureCatalogAndRefresh(); return; }
    const sessionChanged = sessionId !== scope;
    clear();
    scope = sessionId; stream = streamId; snapshot = signature;
    if (sessionChanged) catalog = [];
    dom.source.textContent = scope
      ? `Сеанс: ${scope}. Результаты привязаны к потоку и методике.`
      : "Нет выбранного сеанса.";
    controls(); ensureCatalogAndRefresh();
  }
  function setConnected(value) {
    const wasConnected = connected;
    connected = Boolean(value);
    if (!connected) clear();
    controls();
    if (connected && !wasConnected) ensureCatalogAndRefresh();
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  dom.methodology.addEventListener("change", syncCodes);
  dom.save.addEventListener("click", () => {
    if (!result || suspended) return;
    let url, link;
    try {
      const canonical = JSON.stringify(result, null, 2) + "\n";
      url = URL.createObjectURL(new Blob([canonical], { type: "application/json;charset=utf-8" }));
      link = document.createElement("a"); link.href = url; link.download = "measurement-result.json";
      document.body.appendChild(link); link.click();
      dom.status.textContent = `Передан браузеру результат ${result.id}. Завершение сохранения определяется браузером.`;
    } catch (error) { dom.status.textContent = `Не удалось сохранить результат: ${error.message}`; }
    finally { link?.remove(); if (url) window.setTimeout(() => URL.revokeObjectURL(url), 0); }
  });
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioMeasurementResults = { update, setConnected };
  clear();
})();
