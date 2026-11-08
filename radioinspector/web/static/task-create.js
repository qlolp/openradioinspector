(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const byId = (id) => document.getElementById(id);
  const dom = {
    editor: byId("task-create-editor"),
    form: byId("task-create-form"),
    name: byId("task-create-name"),
    taskFile: byId("task-create-file"),
    startAt: byId("task-create-start"),
    duration: byId("task-create-duration"),
    equipment: byId("task-create-equipment"),
    submit: byId("task-create-submit"),
    status: byId("task-create-status"),
  };
  const state = { pending: false };

  class ApiError extends Error {
    constructor(message, code = "request_failed", status = 0) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
    }
  }

  async function createTask(payload) {
    let response;
    try {
      response = await fetch(`${API_ROOT}/task-scheduler/tasks`, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (error) {
      throw new ApiError(`Ядро недоступно: ${error.message}`, "network_error");
    }
    const raw = await response.text();
    let body;
    try {
      body = raw ? JSON.parse(raw) : {};
    } catch (_error) {
      throw new ApiError("Ядро вернуло не JSON", "invalid_response", response.status);
    }
    if (!response.ok) {
      const detail = body.error || {};
      throw new ApiError(detail.message || `HTTP ${response.status}`, detail.code || "http_error", response.status);
    }
    if (!Object.prototype.hasOwnProperty.call(body, "data")) {
      throw new ApiError("В ответе ядра отсутствует data", "invalid_response", response.status);
    }
    return body.data;
  }

  function render() {
    for (const field of [dom.name, dom.taskFile, dom.startAt, dom.duration, dom.equipment]) {
      field.disabled = state.pending;
    }
    dom.submit.disabled = state.pending;
  }

  async function submit() {
    if (state.pending) return;
    const name = dom.name.value.trim();
    const taskFile = dom.taskFile.value.trim();
    const durationText = dom.duration.value.trim();
    const duration = Number(durationText);
    if (!name || !taskFile || !dom.startAt.value || !Number.isFinite(duration) || duration <= 0) {
      dom.status.textContent = "Заполните name, task_file, start_at и положительное duration_s.";
      dom.status.dataset.tone = "error";
      return;
    }
    const started = new Date(dom.startAt.value);
    if (Number.isNaN(started.getTime())) {
      dom.status.textContent = "start_at должен быть корректной датой и временем.";
      dom.status.dataset.tone = "error";
      return;
    }
    state.pending = true;
    dom.status.textContent = "Задача создаётся через API…";
    dom.status.dataset.tone = "active";
    render();
    try {
      const created = await createTask({
        name,
        task_file: taskFile,
        start_at: started.toISOString(),
        duration_s: duration,
        equipment: dom.equipment.value.trim(),
      });
      dom.status.textContent = `Создано: ${created.name} · ${created.status} · ${created.start_at}`;
      dom.status.dataset.tone = "completed";
      dom.form.reset();
      window.dispatchEvent(new CustomEvent("ori:tasks-changed"));
    } catch (error) {
      dom.status.textContent = `Не создано: ${error.message}. Файл TaskBook и live-измерения не изменены этой ошибкой.`;
      dom.status.dataset.tone = "error";
    } finally {
      state.pending = false;
      render();
    }
  }

  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submit();
  });
  render();
})();
