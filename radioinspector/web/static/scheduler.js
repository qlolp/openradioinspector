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
    list: byId("task-scheduler-list"),
    enable: byId("task-scheduler-enable"),
    disable: byId("task-scheduler-disable"),
  };
  const state = {
    status: null,
    tasks: [],
    error: null,
    tasksError: null,
    suspended: false,
    actionPending: false,
    actionRequest: null,
    pendingReads: {},
    readRequests: new Set(),
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
    // Only read-only polling has a deadline. Never cancel or retry a POST whose
    // outcome might already have changed server state.
    if (options.method && options.method !== "GET") return requestApi(path, options);
    const read = { controller: new AbortController(), timer: null };
    state.readRequests.add(read);
    const deadline = new Promise((_, reject) => {
      read.timer = window.setTimeout(() => {
        read.controller.abort();
        reject(new ApiError("Таймаут запроса (6 с)", "request_timeout"));
      }, 6000);
    });
    try {
      return await Promise.race([
        requestApi(path, { ...options, signal: read.controller.signal }), deadline,
      ]);
    } finally {
      window.clearTimeout(read.timer);
      state.readRequests.delete(read);
    }
  }

  async function requestApi(path, options = {}) {
    const request = { method: options.method || "GET", headers: { Accept: "application/json" } };
    if (options.signal) request.signal = options.signal;
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

  function invalidateReads() {
    state.generation += 1;
    state.pendingReads = {};
    for (const read of state.readRequests) read.controller.abort();
  }

  function refreshPart(key, load, apply) {
    if (state.pendingReads[key]) return state.pendingReads[key].promise;
    const generation = state.generation;
    const pending = { promise: null };
    state.pendingReads[key] = pending;
    const current = () => generation === state.generation && !state.actionPending && !state.suspended;
    pending.promise = load().then((value) => {
      if (current()) apply(value, null);
    }, (error) => {
      if (current()) apply(null, error.message);
    }).finally(() => {
      if (state.pendingReads[key] === pending) delete state.pendingReads[key];
    });
    return pending.promise;
  }

  function refreshScheduler() {
    if (state.actionPending || state.suspended) return Promise.resolve();
    // Each endpoint coalesces independently and renders immediately on completion.
    // A slow task list cannot starve status polling, or vice versa.
    return Promise.allSettled([
      refreshPart("status", () => api("/task-scheduler").then((value) => {
        if (!value || typeof value.enabled !== "boolean") {
          throw new ApiError("Некорректный статус scheduler", "invalid_response");
        }
        return value;
      }), (value, error) => {
        state.status = value;
        state.error = error;
        renderScheduler();
      }),
      refreshPart("tasks", () => api("/task-scheduler/tasks").then((value) => {
        if (!value || !Array.isArray(value.items) || value.count !== value.items.length ||
            value.items.some((task) => !task || typeof task.name !== "string" || typeof task.status !== "string")) {
          throw new ApiError("Некорректный список ControlTask", "invalid_response");
        }
        return value.items;
      }), (value, error) => {
        state.tasks = value || [];
        state.tasksError = error;
        renderScheduler();
      }),
    ]);
  }

  function finishAction(action) {
    if (state.actionRequest !== action) return;
    state.actionRequest = null;
    state.actionPending = false;
    if (!state.suspended) {
      renderScheduler();
      if (action.generation !== state.generation) refreshScheduler();
    }
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
    renderTasks();
  }

  function renderTasks() {
    dom.list.replaceChildren();
    if (state.tasksError) {
      const error = document.createElement("p");
      error.className = "empty-state";
      error.textContent = `Список ControlTask недоступен: ${state.tasksError}. Статус scheduler обновляется независимо.`;
      dom.list.append(error);
      return;
    }
    if (!state.tasks.length) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "В TaskBook нет задач.";
      dom.list.append(empty);
      return;
    }
    for (const task of state.tasks) {
      const article = document.createElement("article");
      article.className = "record";
      const title = document.createElement("strong");
      title.textContent = task.name;
      const details = document.createElement("small");
      const outcome = task.outcome ? ` · ${task.outcome}` : "";
      details.textContent = `${task.status} · ${task.start_at || "время не задано"}${outcome}`;
      const equipment = document.createElement("small");
      equipment.textContent = task.equipment || "Оборудование не указано";
      article.append(title, details, equipment);
      if (task.status === "В очереди на выполнение") {
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "button small danger-quiet";
        cancel.textContent = "Отменить";
        cancel.disabled = state.actionPending;
        cancel.addEventListener("click", () => cancelTask(task.name));
        article.append(cancel);
      }
      dom.list.append(article);
    }
  }

  async function cancelTask(name) {
    if (state.actionPending || state.suspended) return;
    state.actionPending = true;
    invalidateReads();
    const generation = state.generation;
    const action = { generation };
    state.actionRequest = action;
    state.error = null;
    renderScheduler();
    try {
      await api(`/task-scheduler/tasks/${encodeURIComponent(name)}/cancel`, {
        method: "POST", body: {},
      });
      if (generation !== state.generation) return;
      state.actionRequest = null;
      state.actionPending = false;
      await refreshScheduler();
    } catch (error) {
      if (generation !== state.generation) return;
      state.error = error.message;
      renderScheduler();
    } finally {
      finishAction(action);
    }
  }

  async function configureScheduler(enabled) {
    if (state.actionPending || state.suspended) return;
    state.actionPending = true;
    invalidateReads();
    const generation = state.generation;
    const action = { generation };
    state.actionRequest = action;
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
      finishAction(action);
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
  window.addEventListener("ori:tasks-changed", () => refreshScheduler());
  state.timer = window.setInterval(refreshScheduler, 2000);
  window.addEventListener("pagehide", () => {
    state.suspended = true;
    invalidateReads();
    window.clearInterval(state.timer);
  });
  window.addEventListener("pageshow", () => {
    if (!state.suspended) return;
    state.suspended = false;
    state.timer = window.setInterval(refreshScheduler, 2000);
    refreshScheduler();
  });
  refreshScheduler();
})();
