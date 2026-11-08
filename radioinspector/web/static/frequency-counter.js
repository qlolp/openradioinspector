(() => {
  "use strict";

  // Presentation and consistency checks only. The counter calculation and its
  // uncertainty bound live in the core; archived bins are never rewritten.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "sweep", "resolution", "start", "stop",
    "source", "status", "summary", "items"]
    .map(name => [name, byId(`counter-${name}`)]));
  if (!dom.status) return;
  let scope = "", snapshot = "", connected = true, suspended = false, result = null;
  let sweeps = [], generation = 0, pending = null;
  const MAX_RESPONSE_BYTES = 2000000;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const safeInt = value => Number.isSafeInteger(value) && value >= 0;
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  };
  function request() {
    const sweepId = dom.sweep.value;
    if (!sweepId) throw new Error("Выберите архивный dBm свип");
    const resolution = Number(dom.resolution.value);
    if (!Number.isSafeInteger(resolution) || resolution < 1) throw new Error("Разрешение: положительное целое число Гц");
    const start = Number(dom.start.value), stop = Number(dom.stop.value);
    if (!Number.isSafeInteger(start) || !Number.isSafeInteger(stop) || start >= stop) {
      throw new Error("Окно: целые Гц, start < stop");
    }
    return { sweep_id: sweepId, resolution_hz: resolution, start_hz: start, stop_hz: stop };
  }
  function validate(data, sent) {
    if (!object(data) || data.session_id !== scope || !object(data.sweep) || data.sweep.id !== sent.sweep_id ||
        !object(data.window) || data.window.start_hz !== sent.start_hz || data.window.stop_hz !== sent.stop_hz ||
        data.resolution_hz !== sent.resolution_hz || !object(data.values)) {
      throw new Error("Ответ частотомера не соответствует запросу или сеансу");
    }
    if (!["parabolic", "peak_bin"].includes(data.method) || !safeInt(data.peak_index) ||
        !safeInt(data.peak_frequency_hz) || !finite(data.peak_level_dbm) ||
        !finite(data.measured_frequency_hz) || !finite(data.uncertainty_hz) ||
        !safeInt(data.raster_hz)) {
      throw new Error("Некорректные значения частотомера");
    }
    if (data.measured_frequency_hz < data.window.start_hz || data.measured_frequency_hz > data.window.stop_hz) {
      throw new Error("Измеренная частота вне окна измерения");
    }
    if (data.uncertainty_hz < 0) throw new Error("Погрешность не может быть отрицательной");
    if (!equal(Object.keys(data.values).sort(), ["measured_frequency_hz", "resolution_hz", "uncertainty_hz"]) ||
        !Object.values(data.values).every(finite)) {
      throw new Error("Значения результата не совпадают с методикой frequency_counter");
    }
    return data;
  }
  function controls() {
    const ready = Boolean(scope) && connected && !suspended && sweeps.length > 0;
    dom.run.disabled = !ready;
  }
  function clear() {
    pending?.abort(); pending = null; generation += 1; result = null;
    dom.summary.textContent = ""; dom.status.textContent = "";
    dom.items.replaceChildren();
    controls();
  }
  function render() {
    if (!result) return;
    const line = document.createElement("li");
    line.textContent = `${result.measured_frequency_hz} Гц ± ${result.uncertainty_hz} Гц · метод ${result.method}`
      + ` · пик ${result.peak_frequency_hz} Гц (${result.peak_level_dbm} dBm) · сетка ${result.raster_hz} Гц`
      + ` · разрешение ${result.resolution_hz} Гц`;
    dom.items.replaceChildren(line);
    dom.summary.textContent = `Измерено по свипу ${result.sweep.id}: ${result.measured_frequency_hz} Гц ± ${result.uncertainty_hz} Гц. `
      + "Погрешность — честная граница max(разрешение, сетка)/2; исходные бины архива не изменялись. "
      + "Значения готовы для записи в результаты измерений (методика frequency_counter).";
  }
  async function analyze() {
    if (dom.run.disabled) return;
    let body;
    try { body = request(); } catch (error) { clear(); dom.status.textContent = error.message; return; }
    const controller = new AbortController(); pending = controller; const current = ++generation;
    dom.run.disabled = true;
    dom.status.textContent = "Измерение выполняется ядром…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/frequency-counter`, {
        method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body), cache: "no-store", signal: controller.signal,
      });
      const raw = await response.text();
      if (raw.length > MAX_RESPONSE_BYTES) throw new Error("Ответ превышает безопасный размер");
      const envelope = JSON.parse(raw);
      if (response.status !== 200) throw new Error(envelope?.error?.message || `Ошибка ядра ${response.status}`);
      if (envelope.contract !== "openradioinspector-platform") throw new Error("Незнакомый контракт ответа");
      const data = validate(envelope.data, body);
      if (current !== generation || suspended) return;
      result = data;
      render();
      dom.status.textContent = "Измерение завершено. Это производная оценка по записанным данным, не показание аппаратного счётчика.";
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Не удалось выполнить измерение: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const incoming = Array.isArray(next.sweeps) ? next.sweeps.filter(item => item && item.level_unit === "dBm") : [];
    const signature = JSON.stringify(incoming.map(item => item.id));
    const sessionChanged = sessionId !== scope;
    if (!sessionChanged && signature === snapshot) { controls(); return; }
    scope = sessionId; snapshot = signature;
    if (sessionChanged) clear();
    sweeps = incoming;
    const previous = dom.sweep.value;
    const options = sweeps.map(item => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${item.id} · ${item.points || "?"} точек`;
      return option;
    });
    dom.sweep.replaceChildren(...options);
    if (options.some(option => option.value === previous)) dom.sweep.value = previous;
    dom.source.textContent = sweeps.length
      ? `Архивных dBm свипов: ${sweeps.length}. Измерение только по кнопке.`
      : "Нет архивных dBm свипов выбранного потока.";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) clear();
    controls();
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  for (const name of ["sweep", "resolution", "start", "stop"]) {
    dom[name].addEventListener("change", clear);
  }
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioFrequencyCounter = { update, setConnected };
  clear();
})();
