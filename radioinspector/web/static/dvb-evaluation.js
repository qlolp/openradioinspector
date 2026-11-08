(() => {
  "use strict";

  // Presentation and consistency checks only. Thresholds and verdicts live in
  // the core; this panel never demodulates IQ data and never claims to.
  const byId = id => document.getElementById(id);
  const dom = Object.fromEntries(["run", "clear", "mer", "bertv", "berrs",
    "minmer", "maxtv", "maxrs", "guard", "code", "source", "status", "summary", "items"]
    .map(name => [name, byId(`dvb-${name}`)]));
  if (!dom.status) return;
  let scope = "", connected = true, suspended = false, result = null;
  let generation = 0, pending = null;
  const MAX_RESPONSE_BYTES = 2000000;
  const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const equal = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equal(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length &&
      Object.keys(a).every(key => Object.hasOwn(b, key) && equal(a[key], b[key]));
  };
  function number(field, label) {
    const raw = field.value.trim();
    // Number("") and Number("  ") are 0: an empty field must never measure 0.
    if (!raw) throw new Error(`${label}: конечное число`);
    const value = Number(raw);
    if (!finite(value)) throw new Error(`${label}: конечное число`);
    return value;
  }
  function request() {
    return {
      parameters: {
        mer_db: number(dom.mer, "MER"),
        ber_after_viterbi: number(dom.bertv, "BER после Витерби"),
        ber_after_rs: number(dom.berrs, "BER после РС"),
      },
      thresholds: {
        min_mer_db: number(dom.minmer, "Порог MER"),
        max_ber_after_viterbi: number(dom.maxtv, "Порог BER после Витерби"),
        max_ber_after_rs: number(dom.maxrs, "Порог BER после РС"),
      },
      guard_interval: dom.guard.value,
      code_rate: dom.code.value,
    };
  }
  function validate(data, sent) {
    if (!object(data) || data.session_id !== scope || data.methodology !== "dvb_t_parameters" ||
        !["norm", "not_norm"].includes(data.result_code) || !object(data.values) ||
        !object(data.verdicts)) {
      throw new Error("Ответ DVB-T не соответствует запросу или сеансу");
    }
    if (!equal(data.parameters, sent.parameters) || !equal(data.thresholds, sent.thresholds) ||
        data.guard_interval !== sent.guard_interval || data.code_rate !== sent.code_rate) {
      throw new Error("Ответ DVB-T не совпадает с отправленными параметрами");
    }
    const names = ["mer_db", "ber_after_viterbi", "ber_after_rs"];
    if (!equal(Object.keys(data.verdicts).sort(), names.slice().sort()) ||
        !Object.values(data.verdicts).every(value => typeof value === "boolean")) {
      throw new Error("Некорректные вердикты DVB-T");
    }
    if (!equal(Object.keys(data.values).sort(),
               ["ber_after_rs", "ber_after_viterbi", "code_rate_ratio", "guard_interval_ratio", "mer_db"]) ||
        !Object.values(data.values).every(finite)) {
      throw new Error("Значения результата не совпадают с методикой dvb_t_parameters");
    }
    return data;
  }
  function controls() {
    dom.run.disabled = !(Boolean(scope) && connected && !suspended);
  }
  function clear() {
    pending?.abort(); pending = null; generation += 1; result = null;
    dom.summary.textContent = ""; dom.status.textContent = "";
    dom.items.replaceChildren();
    controls();
  }
  function render() {
    if (!result) return;
    const rows = Object.entries(result.verdicts).map(([name, ok]) => {
      const line = document.createElement("li");
      const limit = name === "mer_db" ? result.thresholds.min_mer_db
        : name === "ber_after_viterbi" ? result.thresholds.max_ber_after_viterbi
        : result.thresholds.max_ber_after_rs;
      line.textContent = `${name}: ${result.parameters[name]} (порог ${limit}) — ${ok ? "в норме" : "вне нормы"}`;
      return line;
    });
    dom.items.replaceChildren(...rows);
    dom.summary.textContent = `Код результата: ${result.result_code}. Вердикт относится только к введённым числам и порогам:`
      + " это не измерение и не нормативное заключение. Значения готовы для записи"
      + " в результаты измерений (методика dvb_t_parameters).";
  }
  async function analyze() {
    if (dom.run.disabled) return;
    let body;
    try { body = request(); } catch (error) { clear(); dom.status.textContent = error.message; return; }
    const controller = new AbortController(); pending = controller; const current = ++generation;
    dom.run.disabled = true;
    dom.status.textContent = "Оценка параметров выполняется ядром…";
    try {
      const response = await fetch(`/api/v1/sessions/${encodeURIComponent(scope)}/dvb-evaluation`, {
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
      dom.status.textContent = "Оценка завершена. Демодуляция IQ и измерение MER/BER прибором не выполняются.";
    } catch (error) {
      if (current !== generation || controller.signal.aborted) return;
      dom.status.textContent = `Не удалось выполнить оценку: ${error.message}`;
    } finally {
      if (pending === controller) pending = null;
      controls();
    }
  }
  function update(next) {
    const sessionId = next.sessionId || "";
    const sessionChanged = sessionId !== scope;
    scope = sessionId;
    if (sessionChanged) clear();
    dom.source.textContent = scope
      ? `Сеанс: ${scope}. Параметры и пороги вводятся явно.`
      : "Нет выбранного сеанса.";
    controls();
  }
  function setConnected(value) {
    connected = Boolean(value);
    if (!connected) clear();
    controls();
  }
  dom.run.addEventListener("click", analyze);
  dom.clear.addEventListener("click", clear);
  for (const name of ["mer", "bertv", "berrs", "minmer", "maxtv", "maxrs", "guard", "code"]) {
    dom[name].addEventListener("change", clear);
  }
  window.addEventListener("offline", () => setConnected(false));
  window.addEventListener("pagehide", () => { suspended = true; clear(); });
  window.addEventListener("pageshow", () => { suspended = false; controls(); });
  window.OpenRadioDvbEvaluation = { update, setConnected };
  clear();
})();
