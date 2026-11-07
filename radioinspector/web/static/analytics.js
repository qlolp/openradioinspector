(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const byId = (id) => document.getElementById(id);
  const dom = {
    session: byId("session-select"),
    refresh: byId("analytics-refresh"),
    sweeps: byId("analytics-sweeps"),
    observations: byId("analytics-observations"),
    squelch: byId("analytics-squelch"),
    latest: byId("analytics-latest"),
  };
  const state = { sessionId: "", summary: null, error: null, pending: false, generation: 0 };
  const integerFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  const levelFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });

  class ApiError extends Error {
    constructor(message, code = "request_failed", status = 0) {
      super(message);
      this.name = "ApiError";
      this.code = code;
      this.status = status;
    }
  }

  async function api(path) {
    const response = await fetch(`${API_ROOT}${path}`, { headers: { Accept: "application/json" } });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload.error || {};
      throw new ApiError(detail.message || `HTTP ${response.status}`, detail.code || "http_error", response.status);
    }
    if (!Object.prototype.hasOwnProperty.call(payload, "data")) {
      throw new ApiError("В ответе ядра отсутствует data", "invalid_response", response.status);
    }
    return payload.data;
  }

  function formatHz(value) {
    return Number.isSafeInteger(value) ? `${integerFormatter.format(value)} Гц` : "—";
  }

  function render() {
    dom.refresh.disabled = !state.sessionId || state.pending;
    if (state.pending) {
      dom.sweeps.textContent = "—";
      dom.observations.textContent = "—";
      dom.squelch.textContent = "—";
      dom.latest.textContent = "Сводка всего архива запрашивается у ядра…";
      return;
    }
    if (state.error) {
      dom.sweeps.textContent = "—";
      dom.observations.textContent = "—";
      dom.squelch.textContent = "—";
      dom.latest.textContent = `Сводка недоступна: ${state.error}. Live-измерения продолжаются.`;
      return;
    }
    if (!state.summary) {
      dom.sweeps.textContent = "—";
      dom.observations.textContent = "—";
      dom.squelch.textContent = "—";
      dom.latest.textContent = state.sessionId
        ? "Сводка не входит в обычное обновление; нажмите кнопку для одного запроса."
        : "Сводка появится после выбора сеанса.";
      return;
    }
    const sweeps = state.summary.sweeps || {};
    const observations = state.summary.observations || {};
    dom.sweeps.textContent = `${integerFormatter.format(sweeps.count || 0)} · ${(sweeps.level_units || []).join(", ") || "нет данных"}`;
    dom.observations.textContent = `${integerFormatter.format(observations.count || 0)} · ${(observations.level_units || []).join(", ") || "нет данных"}`;
    const ratio = observations.squelch_open_ratio;
    dom.squelch.textContent = ratio === null || ratio === undefined
      ? `нет данных · известно ${observations.squelch_known || 0}`
      : `${levelFormatter.format(ratio * 100)}% · ${observations.squelch_open || 0} из ${observations.squelch_known || 0}`;
    const peak = sweeps.latest_peak;
    dom.latest.textContent = peak
      ? `Пик последнего sweep: ${formatHz(peak.frequency_hz)} · ${levelFormatter.format(peak.level)} ${peak.level_unit} · ${peak.sweep_id}`
      : "Архив выбранного сеанса пока не содержит спектральных свипов.";
  }

  function setSession(sessionId) {
    if (state.sessionId === sessionId) return;
    state.sessionId = sessionId;
    state.summary = null;
    state.error = null;
    state.generation += 1;
    render();
  }

  async function refreshAnalytics() {
    const sessionId = state.sessionId || dom.session.value;
    if (!sessionId || state.pending) return;
    setSession(sessionId);
    state.pending = true;
    state.error = null;
    state.generation += 1;
    const generation = state.generation;
    render();
    try {
      const summary = await api(`/sessions/${encodeURIComponent(sessionId)}/analytics/summary`);
      if (generation !== state.generation) return;
      state.summary = summary;
    } catch (error) {
      if (generation !== state.generation) return;
      state.summary = null;
      state.error = error.message;
    } finally {
      if (generation === state.generation) {
        state.pending = false;
        render();
      }
    }
  }

  dom.refresh.addEventListener("click", () => { refreshAnalytics(); });
  dom.session.addEventListener("change", () => setSession(dom.session.value));
  const sessionObserver = new MutationObserver(() => setSession(dom.session.value));
  sessionObserver.observe(dom.session, { childList: true, subtree: true });
  setSession(dom.session.value);
  render();
})();
