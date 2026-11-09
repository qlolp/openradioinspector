(() => {
  "use strict";

  // Runtime recorder state only. Frames come from a device stream or the API;
  // this panel never fabricates audio and never writes the archive.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["start", "stop", "refresh", "stream",
    "source", "status", "summary", "items"]
    .map(name => [name, byId(`phono-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = "", connected = true, suspended = false;
  let streams = [], state = null, generation = 0, pending = null;
  const MAX_RESPONSE_BYTES = 2000000;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  function selectedStream() {
    const id = dom.stream.value;
    if (!id) throw new Error("Выберите аудиопоток сеанса");
    return id;
  }
  function validate(data) {
    if (!object(data) || data.session_id !== scope || typeof data.recording !== "boolean" ||
        !finite(data.buffered_seconds) || !finite(data.pre_history_seconds) ||
        !finite(data.recorded_seconds)) {
      throw new Error("Ответ фонограммы не соответствует сеансу");
    }
    if (data.last != null) {
      const last = data.last;
      if (!object(last) || !finite(last.pre_history_seconds) || !finite(last.recorded_seconds) ||
          !finite(last.duration_seconds) || !Number.isSafeInteger(last.pre_history_samples) ||
          !Number.isSafeInteger(last.recorded_samples)) {
        throw new Error("Некорректная последняя фонограмма");
      }
    }
    return data;
  }
  function controls() {
    const ready = Boolean(scope) && connected && !suspended && streams.length > 0;
    dom.stream.disabled = !ready || Boolean(pending);
    dom.start.disabled = !ready || Boolean(pending);
    dom.stop.disabled = !ready || Boolean(pending);
    dom.refresh.disabled = !ready || Boolean(pending);
  }
  function render() {
    if (!state) return;
    const rows = [];
    const row = document.createElement("li");
    row.textContent = `Буфер предыстории: ${state.buffered_seconds.toFixed(2)} с из ${state.pre_history_seconds} с · запись: ${state.recording ? "идёт" : "остановлена"} · записано: ${state.recorded_seconds.toFixed(2)} с`;
    rows.push(row);
    if (state.last) {
      const line = document.createElement("li");
      line.textContent = `Последняя фонограмма: ${state.last.duration_seconds.toFixed(2)} с, из них ${state.last.pre_history_seconds.toFixed(2)} с предыстории и ${state.last.recorded_seconds.toFixed(2)} с записи · ${state.last.sample_rate_hz} Гц`;
      rows.push(line);
    }
    dom.items.replaceChildren(...rows);
    dom.summary.textContent = "Предыстория фиксирована: 5 секунд звучавшего до старта. "
      + "Отсчёты поступают из аудиопотока или через API; панель их не генерирует. "
      + "Запись — runtime-состояние: в архив ничего не пишется.";
  }
  async function call(path, body) {
    if (pending || !connected || suspended || !scope) return;
    const controller = new AbortController(); pending = controller; const current = ++generation;
    controls();
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/phonogram/${path}`, {
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
      if (current !== generation || suspended) return;
      state = validate(envelope.data);
      render();
      dom.status.textContent = body ? "Состояние обновлено." : "Состояние обновлено.";
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Ошибка: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  async function refresh() {
    if (pending || dom.refresh.disabled) return;
    await call(`status?stream_id=${encodeURIComponent(selectedStream())}`, null);
  }
  async function start() {
    if (dom.start.disabled) return;
    try { await call("start", { stream_id: selectedStream() }); }
    catch (error) { dom.status.textContent = error.message; }
  }
  async function stop() {
    if (dom.stop.disabled) return;
    try { await call("stop", { stream_id: selectedStream() }); }
    catch (error) { dom.status.textContent = error.message; }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const incoming = Array.isArray(next.streams) ? next.streams : [];
    const signature = JSON.stringify(incoming.map(item => item.id));
    const sessionChanged = sessionId !== scope;
    if (!sessionChanged && signature === snapshot) { controls(); return; }
    scope = sessionId; snapshot = signature;
    if (sessionChanged) {
      invalidate();
    }
    streams = incoming;
    const previous = dom.stream.value;
    const options = streams.map(item => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.id} · ${item.device || item.level_unit || ""}`;
      return option;
    });
    dom.stream.replaceChildren(...options);
    if (options.some(option => option.value === previous)) dom.stream.value = previous;
    dom.source.textContent = streams.length
      ? `Аудиопотоков сеанса: ${streams.length}.`
      : "Нет потоков выбранного сеанса.";
    controls();
  }
  function invalidate() {
    pending?.abort(); pending = null; generation += 1;
    state = null; dom.items.replaceChildren(); dom.summary.textContent = ""; controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) invalidate();
    controls();
  }
  dom.start.addEventListener("click", start);
  dom.stop.addEventListener("click", stop);
  dom.refresh.addEventListener("click", refresh);
  dom.stream.addEventListener("change", invalidate);
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; invalidate(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioPhonogram = { update, setConnected };
  controls();
})();
