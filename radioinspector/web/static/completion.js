(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const COMPLETION_EVENT_TYPES = [
    "automatic_completion.policy_changed", "automatic_completion.completed",
  ];
  const byId = (id) => document.getElementById(id);
  const dom = {
    session: byId("session-select"), editor: byId("completion-editor"),
    form: byId("completion-form"), status: byId("completion-status"),
    interval: byId("completion-interval"), listening: byId("completion-listening"),
    observations: byId("completion-observations"), receiver: byId("completion-receiver"),
    save: byId("completion-save"), enable: byId("completion-enable"),
    disable: byId("completion-disable"),
  };
  const state = {
    automaticCompletion: null,
    completionFormDirty: false,
    actionPending: false,
    generation: 0,
    timer: null,
  };

  class ApiError extends Error {
    constructor(message, code = "request_failed", status = 0) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
    }
  }

  async function api(path, options = {}) {
    const request = { method: options.method || "GET", headers: { Accept: "application/json" } };
    if (options.body !== undefined) {
      request.headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(`${API_ROOT}${path}`, request);
    } catch (error) {
      throw new ApiError(`Ядро недоступно: ${error.message}`, "network_error");
    }
    const raw = await response.text();
    let payload;
    try {
      payload = raw ? JSON.parse(raw) : {};
    } catch (_error) {
      throw new ApiError(`Ядро вернуло не JSON (HTTP ${response.status})`, "invalid_response", response.status);
    }
    if (!response.ok) {
      const detail = payload.error || {};
      throw new ApiError(detail.message || `HTTP ${response.status}`, detail.code || "http_error", response.status);
    }
    if (!Object.prototype.hasOwnProperty.call(payload, "data")) {
      throw new ApiError("В ответе ядра отсутствует data", "invalid_response", response.status);
    }
    return payload.data;
  }

  const encode = (value) => encodeURIComponent(String(value));
  const sessionPath = (suffix = "") => `/sessions/${encode(dom.session.value)}${suffix}`;
  const formatTime = (value) => value ? new Date(value).toISOString() : "—";

  function setOptions(select, items, selectedValue) {
    const options = [new Option("Любой receiver сеанса", "")];
    for (const item of items) options.push(new Option(item.label, item.value));
    select.replaceChildren(...options);
    select.value = selectedValue ?? "";
    if (select.value !== (selectedValue ?? "") && options.length) select.selectedIndex = 0;
  }

  function renderAutomaticCompletion() {
    const status = state.automaticCompletion;
    const policy = status?.policy;
    const runtime = status?.runtime;
    const enabled = Boolean(policy?.enabled);
    dom.status.dataset.enabled = String(enabled);
    if (!status) {
      dom.status.textContent = dom.session.value
        ? "Состояние автоматического завершения ещё не получено от ядра."
        : "Выберите сеанс, чтобы просмотреть политику завершения.";
    } else if (status.error) {
      dom.status.textContent = `Политика завершения недоступна: ${status.error}. Live-измерения и другие политики продолжаются.`;
    } else {
      const receiver = policy.receiver_stream_id || "любой receiver";
      const last = runtime.last_investigation_id ? ` · исследование ${runtime.last_investigation_id}` : "";
      const error = runtime.last_error ? ` · ${runtime.last_error}` : "";
      dom.status.textContent = `${enabled ? "Включено" : "Выключено"} · ${runtime.last_result} · ${receiver} · завершено ${runtime.completed_investigations}, пропущено ${runtime.skipped_investigations} · проход ${formatTime(runtime.last_run_at)}${last}${error}`;
    }
    const fields = [dom.interval, dom.listening, dom.observations, dom.receiver];
    for (const field of fields) field.disabled = !status || Boolean(status.error) || state.actionPending;
    dom.save.disabled = !status || Boolean(status.error) || state.actionPending;
    dom.enable.disabled = !status || Boolean(status.error) || state.actionPending;
    dom.disable.disabled = !status || Boolean(status.error) || !enabled || state.actionPending;
    if (!status || status.error || state.completionFormDirty) return;
    dom.interval.value = String(policy.interval_seconds);
    dom.listening.value = String(policy.minimum_listening_seconds);
    dom.observations.value = String(policy.minimum_observations);
    dom.receiver.value = policy.receiver_stream_id || "";
  }

  async function loadReceivers(policy) {
    const session = await api(sessionPath());
    const items = (session.streams || [])
      .filter((stream) => stream.role === "receiver")
      .map((stream) => ({ value: stream.id, label: `${stream.device} · ${stream.level_unit}` }));
    setOptions(dom.receiver, items, policy.receiver_stream_id);
  }

  async function refreshCompletion() {
    const generation = state.generation;
    if (!dom.session.value) {
      state.automaticCompletion = null;
      renderAutomaticCompletion();
      return;
    }
    try {
      const [status, session] = await Promise.all([
        api(sessionPath("/automatic-completion"), apiOptionsWithNull()),
        api(sessionPath()),
      ]);
      if (generation !== state.generation) return;
      state.automaticCompletion = status;
      const policy = status.policy || {};
      if (!state.completionFormDirty) {
        await loadReceivers(policy);
      }
    } catch (error) {
      if (generation !== state.generation) return;
      state.automaticCompletion = { error: error.message };
    }
    renderAutomaticCompletion();
  }

  function apiOptionsWithNull() {
    return { method: "GET" };
  }

  function requiredNumber(input, label, minimum, maximum) {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw new ApiError(`${label}: требуется число в диапазоне ${minimum}…${maximum}.`, "invalid_form");
    }
    return value;
  }

  function requiredInteger(input, label, minimum) {
    const value = input.valueAsNumber;
    if (!Number.isSafeInteger(value) || value < minimum) {
      throw new ApiError(`${label}: требуется целое число ≥ ${minimum}.`, "invalid_form");
    }
    return value;
  }

  async function configureAutomaticCompletion(enabled, saveOnly = false) {
    if (!dom.session.value) throw new ApiError("Сначала выберите сеанс.", "invalid_form");
    const payload = {};
    if (!saveOnly) payload.enabled = enabled;
    if (enabled || saveOnly) {
      if (enabled && !saveOnly) {
        const session = await api(sessionPath());
        if (session.state !== "running") {
          throw new ApiError("Автоматическое завершение можно включить только в работающем сеансе.", "invalid_form");
        }
      }
      payload.interval_seconds = requiredNumber(dom.interval, "Период проверки", 0.1, 3600);
      payload.minimum_listening_seconds = requiredNumber(dom.listening, "Минимум прослушивания", 0, 86400);
      payload.minimum_observations = requiredInteger(dom.observations, "Минимум наблюдений", 1);
      payload.receiver_stream_id = dom.receiver.value || null;
    }
    state.actionPending = true;
    renderAutomaticCompletion();
    try {
      const status = await api(sessionPath("/automatic-completion"), { method: "POST", body: payload });
      state.automaticCompletion = status;
      state.completionFormDirty = false;
      renderAutomaticCompletion();
    } finally {
      state.actionPending = false;
      renderAutomaticCompletion();
    }
  }

  function run(action) {
    Promise.resolve().then(action).catch((error) => {
      state.automaticCompletion = state.automaticCompletion || { error: error.message };
      if (state.automaticCompletion.policy) {
        state.automaticCompletion.runtime.last_error = error.message;
      } else {
        state.automaticCompletion.error = error.message;
      }
      renderAutomaticCompletion();
    });
  }

  dom.form.addEventListener("input", () => { state.completionFormDirty = true; });
  dom.form.addEventListener("change", () => { state.completionFormDirty = true; });
  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    run(() => configureAutomaticCompletion(true));
  });
  dom.save.addEventListener("click", () => {
    const enabled = state.automaticCompletion?.policy?.enabled ?? false;
    run(() => configureAutomaticCompletion(enabled, true));
  });
  dom.disable.addEventListener("click", () => {
    run(() => configureAutomaticCompletion(false));
  });
  dom.session.addEventListener("change", () => {
    state.generation += 1;
    state.automaticCompletion = null;
    state.completionFormDirty = false;
    refreshCompletion();
  });
  dom.editor.addEventListener("toggle", () => {
    if (dom.editor.open) refreshCompletion();
  });
  state.timer = window.setInterval(refreshCompletion, 2000);
  window.addEventListener("pagehide", () => window.clearInterval(state.timer));
  refreshCompletion();
})();
