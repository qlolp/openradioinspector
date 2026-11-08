(() => {
  "use strict";

  // Network delivery of demodulated audio. Blocks arrive as the recorder
  // accepts frames; a slow reader only loses its own queue tail. This panel
  // never generates audio and never writes the archive.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["connect", "disconnect", "refresh", "stream",
    "source", "status", "summary", "items"]
    .map(name => [name, byId(`audio-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = "", connected = true, suspended = false;
  let generation = 0;
  let streams = [], reader = null, abort = null, received = 0, blocks = 0, connecting = false;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  function selectedStream() {
    const id = dom.stream.value;
    if (!id) throw new Error("Выберите аудиопоток сеанса");
    return id;
  }
  function controls() {
    const ready = Boolean(scope) && connected && !suspended && streams.length > 0;
    dom.stream.disabled = !ready || Boolean(abort);
    dom.connect.disabled = !ready || reader !== null || connecting;
    dom.disconnect.disabled = abort === null;
    dom.refresh.disabled = !ready;
  }
  function render() {
    const line = document.createElement("li");
    line.textContent = reader
      ? `Поток открыт: получено ${received} байт PCM16LE (${blocks} блоков). Подключение удерживается ответом сервера без Content-Length.`
      : (received > 0
        ? `Трансляция завершена: получено ${received} байт PCM16LE (${blocks} блоков).`
        : "Поток не подключён.");
    dom.items.replaceChildren(line);
    dom.summary.textContent = reader
      ? "Идёт трансляция демодулированного аудио. Остановленный клиент теряет только собственную очередь; запись и другие клиенты не блокируются."
      : "Трансляция запускается кнопкой: аудио поступает из аудиопотока сеанса, клиент его не генерирует.";
  }
  async function open() {
    if (dom.connect.disabled || abort) return;
    let stream;
    try { stream = selectedStream(); } catch (error) { dom.status.textContent = error.message; return; }
    const controller = new AbortController(), current = ++generation;
    let localReader = null;
    abort = controller; connecting = true; received = 0; blocks = 0;
    controls(); dom.status.textContent = "Подключение к аудиопотоку…";
    try {
      const response = await fetch(
        `/api/v1/sessions/${encodeURIComponent(scope)}/audio/stream?stream_id=${encodeURIComponent(stream)}`,
        { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error(`Ошибка ядра ${response.status}`);
      const type = response.headers.get("Content-Type") || "";
      if (!type.includes("audio/pcm")) throw new Error("Сервер не отдаёт audio/pcm");
      localReader = response.body.getReader();
      if (current !== generation || controller.signal.aborted) return;
      reader = localReader;
      dom.status.textContent = "Поток открыт; блоки поступают по мере записи.";
      render(); controls();
      for (;;) {
        const { done, value } = await localReader.read();
        if (current !== generation || controller.signal.aborted) return;
        if (done) break;
        if (value && value.length) { received += value.length; blocks += 1; render(); }
      }
      dom.status.textContent = "Поток завершён сервером.";
    } catch (error) {
      if (current === generation) dom.status.textContent = controller.signal.aborted
        ? "Поток отключён." : `Поток прерван: ${error.message}`;
    } finally {
      if (localReader) { try { await localReader.cancel(); } catch { /* response already closed */ } }
      if (current === generation) {
        reader = null; abort = null; connecting = false; render(); controls();
      }
    }
  }
  function close() {
    generation += 1;
    abort?.abort();
    if (reader) Promise.resolve(reader.cancel()).catch(() => {});
    reader = null; abort = null; connecting = false;
    render(); controls();
  }
  async function refresh() {
    if (dom.refresh.disabled) return;
    const current = generation, session = scope;
    try {
      const stream = selectedStream();
      const response = await fetch(
        `/api/v1/sessions/${encodeURIComponent(scope)}/audio/status?stream_id=${encodeURIComponent(stream)}`,
        { cache: "no-store" });
      const envelope = await response.json();
      if (current !== generation || session !== scope || !connected || suspended) return;
      if (!response.ok) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      const data = envelope.data;
      if (!object(data) || !Number.isSafeInteger(data.subscribers)) throw new Error("Некорректный статус потока");
      const line = document.createElement("li");
      line.textContent = `Подписчиков: ${data.subscribers} из ${data.max_subscribers}; в очередях ${data.queued_blocks} блоков; потеряно ${data.dropped_blocks} блоков.`;
      dom.items.replaceChildren(line);
      dom.status.textContent = "Статус обновлён.";
    } catch (error) {
      if (current !== generation) return;
      dom.status.textContent = `Ошибка: ${error.message}`;
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const incoming = Array.isArray(next.streams) ? next.streams : [];
    const signature = JSON.stringify(incoming.map(item => item.id));
    const sessionChanged = sessionId !== scope;
    if (!sessionChanged && signature === snapshot) { controls(); return; }
    if (sessionChanged) { close(); received = 0; blocks = 0; }
    scope = sessionId; snapshot = signature;
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
      ? `Потоков сеанса: ${streams.length}.`
      : "Нет потоков выбранного сеанса.";
    render(); controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) close();
    controls();
  }
  dom.stream.addEventListener("change", () => { close(); received = 0; blocks = 0; render(); });
  dom.connect.addEventListener("click", open);
  dom.disconnect.addEventListener("click", close);
  dom.refresh.addEventListener("click", refresh);
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; close(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioAudioStream = { update, setConnected };
  controls();
})();
