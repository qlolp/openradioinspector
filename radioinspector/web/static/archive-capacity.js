(() => {
  "use strict";

  const status = document.getElementById("archive-capacity-status");
  if (!status) return;
  let generation = 0;
  let pending = null;
  const states = new Set(["unlimited", "ok", "warning", "full", "unavailable"]);
  const size = (value) => value === null ? "неизвестно" : `${value.toLocaleString("ru-RU")} Б`;

  function validate(data) {
    if (!data || !["sqlite", "memory"].includes(data.storage_mode) || !states.has(data.state)) {
      throw new Error("Некорректное состояние ёмкости архива");
    }
    for (const key of ["database_bytes", "max_database_bytes", "remaining_bytes"]) {
      if (data[key] !== null && (!Number.isSafeInteger(data[key]) || data[key] < 0)) {
        throw new Error(`Некорректное значение ${key}`);
      }
    }
    if (typeof data.measurement_writes_blocked !== "boolean" || data.automatic_deletion !== false || data.warning_threshold !== 0.9) {
      throw new Error("Неподдерживаемая политика ёмкости архива");
    }
    if (["ok", "warning", "full"].includes(data.state) && (
      data.storage_mode !== "sqlite" || data.database_bytes === null ||
      !data.max_database_bytes || data.remaining_bytes === null
    )) throw new Error("Неполные данные ёмкости SQLite");
    return data;
  }

  function render(data, error = null) {
    status.dataset.state = data?.state || "unavailable";
    if (error) {
      status.textContent = `Ёмкость архива недоступна: ${error}. Остальные панели продолжают работать.`;
      return;
    }
    if (data.storage_mode === "memory") {
      status.textContent = "Архив в памяти: размер SQLite недоступен; дисковый лимит не применяется.";
      return;
    }
    const limit = data.max_database_bytes === null ? "без лимита" : size(data.max_database_bytes);
    const prefix = `SQLite (логический): ${size(data.database_bytes)} / ${limit}`;
    if (data.state === "unavailable") {
      status.textContent = `${prefix} · ёмкость недоступна; состояние записи неизвестно.`;
    } else if (data.state === "unlimited") {
      status.textContent = `${prefix} · лимит не задан.`;
    } else if (data.state === "full") {
      status.textContent = `${prefix} · ЛИМИТ ДОСТИГНУТ: запись новых измерений заблокирована.`;
    } else {
      const warning = data.state === "warning" ? "ВНИМАНИЕ: занято ≥ 90% лимита" : "в пределах лимита";
      const blocked = data.measurement_writes_blocked ? " · запись новых измерений заблокирована" : "";
      status.textContent = `${prefix} · осталось ${size(data.remaining_bytes)} · ${warning}${blocked}.`;
    }
  }

  function invalidate() {
    generation += 1;
    if (pending) {
      window.clearTimeout(pending.timer);
      pending.controller.abort();
      pending = null;
    }
  }

  // Called by the existing platform polling/manual/SSE refresh lifecycle.
  // Capacity is global and never depends on the selected session or its archive.
  function refresh() {
    // Polling/SSE must not restart the timeout: a slow request keeps its own deadline.
    if (pending) return pending.promise;
    const current = generation;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 6000);
    const request = { controller, timer, promise: null };
    pending = request;
    request.promise = requestCapacity(current, controller, timer);
    return request.promise;
  }

  async function requestCapacity(current, controller, timer) {
    try {
      const response = await fetch("/api/v1/archive/capacity", {
        method: "GET", headers: { Accept: "application/json" },
        cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (payload?.contract !== "openradioinspector-platform" || payload.contract_version !== 1) {
        throw new Error("Неподдерживаемый контракт API");
      }
      const data = validate(payload.data);
      if (current === generation) render(data);
    } catch (error) {
      if (current === generation) {
        render(null, error.name === "AbortError" ? "таймаут запроса" : error.message);
      }
    } finally {
      window.clearTimeout(timer);
      if (current === generation) pending = null;
    }
  }

  window.OpenRadioArchiveCapacity = { refresh };
  window.addEventListener("pagehide", invalidate);
})();
