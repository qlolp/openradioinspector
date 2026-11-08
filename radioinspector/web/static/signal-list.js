(() => {
  "use strict";

  // Export serializes archived candidates; import creates new candidates
  // through the normal lifecycle. Levels are never converted between units.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["export", "import", "stream", "name", "editor",
    "source", "status", "summary", "items"]
    .map(name => [name, byId(`siglist-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = "", connected = true, suspended = false;
  let streams = [], generation = 0, pending = null;
  const MAX_RESPONSE_BYTES = 2000000, MAX_LIST_BYTES = 262144;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  function selectedStream() {
    const id = dom.stream.value;
    if (!id) throw new Error("Выберите поток для импорта");
    return id;
  }
  function validateExport(data) {
    if (!object(data) || data.session_id !== scope || !Number.isSafeInteger(data.exported) ||
        !object(data.list) || !Array.isArray(data.list.entries)) {
      throw new Error("Ответ экспорта не соответствует сеансу");
    }
    return data;
  }
  function validateImport(data, sentCount) {
    if (!object(data) || data.session_id !== scope ||
        !Number.isSafeInteger(data.imported) || data.imported !== sentCount ||
        !Array.isArray(data.candidate_ids) || data.candidate_ids.length !== sentCount) {
      throw new Error("Ответ импорта не соответствует запросу");
    }
    return data;
  }
  function controls() {
    const ready = Boolean(scope) && connected && !suspended && streams.length > 0;
    dom.stream.disabled = !ready || Boolean(pending);
    dom.export.disabled = !ready || Boolean(pending);
    dom.import.disabled = Boolean(pending) || !ready;
  }
  async function call(path, body) {
    if (pending) return null;
    const controller = new AbortController(); pending = controller; const current = ++generation;
    controls();
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/signal-list/${path}`, {
        method: body ? "POST" : "GET",
        headers: body ? { Accept: "application/json", "Content-Type": "application/json" } : { Accept: "application/json" },
        body: body ? JSON.stringify(body) : undefined,
        cache: "no-store", signal: controller.signal,
      });
      const raw = await response.text();
      if (raw.length > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает безопасный размер");
      const envelope = JSON.parse(raw);
      if (!response.ok) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      if (current !== generation || suspended) return null;
      return envelope.data;
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return null;
      dom.status.textContent = `Ошибка: ${error.message}`;
      return null;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  async function exportList() {
    if (dom.export.disabled) return;
    const name = dom.name.value.trim() || "Список сигналов";
    const data = await call(`export?name=${encodeURIComponent(name)}`, null);
    if (!data) return;
    try {
      const validated = validateExport(data);
      dom.editor.value = JSON.stringify(validated.list, null, 2);
      const line = document.createElement("li");
      line.textContent = `Экспортировано сигналов: ${validated.exported} («${validated.list.name}»). Черновик в поле ниже — тот же артефакт, что и в файле.`;
      dom.items.replaceChildren(line);
      dom.summary.textContent = "Экспорт — сериализация архивных кандидатов; исходные записи не изменяются.";
      dom.status.textContent = "Список сигналов выгружен в черновик.";
    } catch (error) {
      dom.status.textContent = `Ошибка: ${error.message}`;
    }
  }
  async function importList() {
    if (dom.import.disabled) return;
    let raw, stream;
    try {
      stream = selectedStream();
      raw = dom.editor.value;
      if (!raw.trim()) throw new Error("Вставьте список сигналов в черновик");
      if (raw.length > MAX_LIST_BYTES) throw new Error("Список превышает 262 144 байт");
    } catch (error) { dom.status.textContent = error.message; return; }
    const data = await call("import", { stream_id: stream, list_json: raw });
    if (!data) return;
    try {
      const sentCount = JSON.parse(raw).entries.length;
      const validated = validateImport(data, sentCount);
      const line = document.createElement("li");
      line.textContent = `Импортировано сигналов: ${validated.imported} («${validated.name}») в поток ${validated.stream_id}. Импортированные сигналы начинаются с состояния new.`;
      dom.items.replaceChildren(line);
      dom.summary.textContent = "Импорт создаёт новых кандидатов через обычный жизненный цикл; единицы измерения не конвертируются.";
      dom.status.textContent = "Список сигналов загружен.";
    } catch (error) {
      dom.status.textContent = `Ошибка: ${error.message}`;
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const incoming = Array.isArray(next.streams) ? next.streams : [];
    const signature = JSON.stringify(incoming.map(item => item.id));
    const sessionChanged = sessionId !== scope;
    if (!sessionChanged && signature === snapshot) { controls(); return; }
    scope = sessionId; snapshot = signature;
    if (sessionChanged) {
      pending?.abort(); pending = null; generation += 1;
      dom.items.replaceChildren();
      dom.summary.textContent = "";
    }
    streams = incoming;
    const previous = dom.stream.value;
    const options = streams.map(item => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.id} · ${item.level_unit || item.device || ""}`;
      return option;
    });
    dom.stream.replaceChildren(...options);
    if (options.some(option => option.value === previous)) dom.stream.value = previous;
    dom.source.textContent = streams.length
      ? `Потоков сеанса: ${streams.length}.`
      : "Нет потоков выбранного сеанса.";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) { pending?.abort(); pending = null; generation += 1; dom.items.replaceChildren(); dom.summary.textContent = ""; }
    controls();
  }
  dom.export.addEventListener("click", exportList);
  dom.import.addEventListener("click", importList);
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; setConnected(false); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioSignalList = { update, setConnected };
  controls();
})();
