(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const byId = (id) => document.getElementById(id);
  const dom = {
    editor: byId("task-scheduler-editor"),
    status: byId("task-scheduler-status"),
    total: byId("task-scheduler-total"),
    running: byId("task-scheduler-running"),
    completed: byId("task-scheduler-completed"),
    enable: byId("task-scheduler-enable"),
    disable: byId("task-scheduler-disable"),
  };
  const state = {
    status: null,
    error: null,
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

  function renderScheduler() {
    const status = state.status;
    const enabled = Boolean(status?.enabled);
    dom.status.dataset.enabled = String(enabled);
    if (!status) {
      dom.status.textContent = state.error
        ? `Состояние scheduler недоступно: ${state.error}. Остальные панели продолжают работать.`
        : "Состояние scheduler запрашивается у ядра…";
    } else {
      const runtime = status.runtime || {};
      const taskName = runtime.last_task_name ? ` · ${runtime.last_task_name}` : "";
      const runtimeError = runtime.last_error ? ` · ${runtime.last_error}` : "";
      const requestError = state.error ? ` · ${state.error}` : "";
      dom.status.textContent = `${enabled ? "Включён" : "Выключен"} · thread ${status.scheduler_alive ? "active" : "stopped"} · ${runtime.last_result || "idle"}${taskName}${runtimeError}${requestError}`;
    }
    dom.total.textContent = status ? String(status.tasks?.total ?? 0) : "—";
    dom.running.textContent = status ? String(status.tasks?.running ?? 0) : "—";
    dom.completed.textContent = status ? String(status.tasks?.completed ?? 0) : "—";
    dom.enable.disabled = !status || enabled || state.actionPending;
    dom.disable.disabled = !status || !enabled || state.actionPending;
  }

  async function refreshScheduler() {
    if (state.actionPending) return;
    const generation = state.generation;
    try {
      const status = await api("/task-scheduler");
      if (generation !== state.generation) return;
      state.status = status;
      state.error = null;
    } catch (error) {
      if (generation !== state.generation) return;
      state.error = error.message;
    }
    renderScheduler();
  }

  async function configureScheduler(enabled) {
    if (state.actionPending) return;
    state.actionPending = true;
    state.generation += 1;
    const generation = state.generation;
    state.error = null;
    renderScheduler();
    try {
      const status = await api("/task-scheduler", {
        method: "POST", body: { enabled },
      });
      if (generation !== state.generation) return;
      state.status = status;
    } catch (error) {
      if (generation !== state.generation) return;
      state.error = error.message;
    } finally {
      if (generation === state.generation) {
        state.actionPending = false;
        renderScheduler();
      }
    }
  }

  function run(enabled) {
    configureScheduler(enabled).catch((error) => {
      state.actionPending = false;
      state.error = error.message;
      renderScheduler();
    });
  }

  dom.enable.addEventListener("click", () => run(true));
  dom.disable.addEventListener("click", () => run(false));
  dom.editor.addEventListener("toggle", () => {
    if (dom.editor.open) refreshScheduler();
  });
  state.timer = window.setInterval(refreshScheduler, 2000);
  window.addEventListener("pagehide", () => window.clearInterval(state.timer));
  refreshScheduler();
})();
