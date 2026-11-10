(() => {
  "use strict";

  const API_ROOT = "/api/v1";
  const MAX_SWEEPS = 40;
  const MAX_POINTS = 1000;
  const OPEN_INVESTIGATIONS = new Set(["queued", "tuning", "listening", "decoding"]);
  const EVENT_TYPES = [
    "session.created", "session.started", "session.stopped",
    "device.state", "device.error", "device.tuned",
    "sweep.received", "observation.received",
    "candidate.created", "candidate.dismissed",
    "investigation.queued", "investigation.tuning", "investigation.listening",
    "investigation.completed", "investigation.failed", "investigation.cancelled",
    "investigation.interrupted", "automatic_handoff.policy_changed",
    "automatic_handoff.started", "automatic_handoff.disabled",
    "automatic_detection.policy_changed", "automatic_detection.candidate_created",
  ];
  const STATE_LABELS = {
    draft: "Черновик", running: "Работает", stopped: "Остановлен",
    ready: "Готов", disconnected: "Отключён", degraded: "Деградация", error: "Ошибка",
    new: "Новый", queued: "В очереди", investigating: "Исследуется",
    resolved: "Завершён", dismissed: "Отклонён",
    tuning: "Настройка", listening: "Прослушивание", decoding: "Декодирование",
    completed: "Завершено", failed: "Сбой", cancelled: "Отменено",
  };
  const ROLE_LABELS = { spectrum_analyzer: "Спектральный обзор", receiver: "Receiver" };

  const byId = (id) => document.getElementById(id);
  const dom = {
    notice: byId("notice"), refresh: byId("refresh-button"),
    sessionSelect: byId("session-select"), sessionEmpty: byId("session-empty"),
    sessionMeta: byId("session-meta"), sessionStart: byId("session-start"),
    sessionStop: byId("session-stop"), workersStart: byId("workers-start"),
    workersStop: byId("workers-stop"), deviceCount: byId("device-count"),
    deviceList: byId("device-list"), spectrumStream: byId("spectrum-stream"),
    markPeak: byId("mark-peak"), spectrumCanvas: byId("spectrum-canvas"),
    spectrumEmpty: byId("spectrum-empty"), range: byId("range-readout"),
    peak: byId("peak-readout"), points: byId("points-readout"), time: byId("time-readout"),
    waterfallCanvas: byId("waterfall-canvas"), waterfallScale: byId("waterfall-scale"),
    eventList: byId("event-list"), candidateFilter: byId("candidate-filter"),
    candidateEditor: byId("candidate-editor"), candidateForm: byId("candidate-form"),
    candidateSource: byId("candidate-source"), candidateStarted: byId("candidate-started"),
    candidateEnded: byId("candidate-ended"), candidateFrequency: byId("candidate-frequency"),
    candidateBandwidth: byId("candidate-bandwidth"), candidateLevel: byId("candidate-level"),
    candidateScore: byId("candidate-score"), candidateUnit: byId("candidate-unit"),
    candidateCancel: byId("candidate-cancel"), candidateList: byId("candidate-list"),
    detectionEditor: byId("detection-editor"), detectionForm: byId("detection-form"),
    detectionStatus: byId("detection-status"), detectionInterval: byId("detection-interval"),
    detectionThreshold: byId("detection-threshold"), detectionDedup: byId("detection-dedup"),
    detectionCooldown: byId("detection-cooldown"), detectionMax: byId("detection-max"),
    detectionStream: byId("detection-stream"), detectionEnable: byId("detection-enable"),
    detectionDisable: byId("detection-disable"),
    investigationFilter: byId("investigation-filter"), handoffForm: byId("handoff-form"),
    dispatchNext: byId("dispatch-next"), nextInvestigation: byId("next-investigation"),
    automaticEditor: byId("automatic-editor"), automaticForm: byId("automatic-form"),
    automaticStatus: byId("automatic-status"), automaticInterval: byId("automatic-interval"),
    automaticPriority: byId("automatic-priority"), automaticFailures: byId("automatic-failures"),
    automaticRetry: byId("automatic-retry"), automaticReceiver: byId("automatic-receiver"),
    automaticEnable: byId("automatic-enable"), automaticDisable: byId("automatic-disable"),
    handoffCandidate: byId("handoff-candidate"), handoffLabel: byId("handoff-label"),
    handoffTarget: byId("handoff-target"), handoffFrequency: byId("handoff-frequency"),
    handoffPriority: byId("handoff-priority"), handoffMode: byId("handoff-mode"),
    handoffCancel: byId("handoff-cancel"), investigationList: byId("investigation-list"),
    sseStatus: byId("sse-status"), health: byId("health-summary"),
    hardware: byId("hardware-capability"), storage: byId("storage-capability"),
  };

  const state = {
    spectrumView: new window.OpenRadioSpectrumView.View(),
    sessions: [], sessionId: null, session: null, workers: [],
    candidates: [], investigations: [], nextInvestigation: null, automaticHandoff: null,
    automaticDetection: null,
    events: [], sweeps: [], spectrumStreamId: null, capabilities: null, health: null,
    automaticFormDirty: false, detectionFormDirty: false,
    refreshRunning: false, refreshQueued: false, actionPending: false,
    noticeTimer: null, refreshTimer: null, eventSource: null,
    lastEventId: 0, initialized: false,
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
  const sessionPath = (suffix = "") => `/sessions/${encode(state.sessionId)}${suffix}`;
  const integerFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
  const levelFormatter = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });

  function formatHz(value) {
    return Number.isSafeInteger(value) ? `${integerFormatter.format(value)} Гц` : "—";
  }

  function formatLevel(value, unit) {
    return typeof value === "number" && Number.isFinite(value)
      ? `${levelFormatter.format(value)} ${unit || ""}`.trim()
      : "—";
  }

  function formatTime(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toISOString();
  }

  function shortTime(value) {
    const full = formatTime(value);
    return full.includes("T") ? full.slice(11, 19) : full;
  }

  function stateLabel(value) {
    return STATE_LABELS[value] || value || "Неизвестно";
  }

  function streamById(streamId) {
    return state.session?.streams.find((item) => item.id === streamId) || null;
  }

  function workerById(streamId) {
    return state.workers.find((item) => item.stream_id === streamId) || null;
  }

  function candidateById(candidateId) {
    return state.candidates.find((item) => item.id === candidateId) || null;
  }

  function makeElement(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = String(text);
    return element;
  }

  function makeBadge(value) {
    const badge = makeElement("span", "state-badge", stateLabel(value));
    badge.dataset.tone = value || "muted";
    return badge;
  }

  function makeButton(label, action, id, options = {}) {
    const button = makeElement("button", `button${options.small ? " small" : ""}`, label);
    button.type = "button";
    button.dataset.action = action;
    if (id) button.dataset.id = id;
    button.disabled = Boolean(options.disabled) || state.actionPending;
    if (options.title) button.title = options.title;
    return button;
  }

  function emptyState(text) {
    return makeElement("p", "empty-state", text);
  }

  function notify(message, tone = "active") {
    window.clearTimeout(state.noticeTimer);
    dom.notice.textContent = message;
    dom.notice.dataset.tone = tone;
    dom.notice.hidden = false;
    state.noticeTimer = window.setTimeout(() => { dom.notice.hidden = true; }, tone === "error" ? 10000 : 6000);
  }

  function reportError(error) {
    const message = error instanceof Error ? error.message : String(error);
    notify(message, "error");
  }

  function setOptions(select, items, selectedValue, emptyLabel) {
    const options = [];
    if (emptyLabel !== undefined) options.push(new Option(emptyLabel, ""));
    for (const item of items) options.push(new Option(item.label, item.value));
    const wanted = selectedValue ?? "";
    select.replaceChildren(...options);
    select.value = wanted;
    if (select.value !== wanted && options.length) select.selectedIndex = 0;
  }

  function readStoredSession() {
    try { return window.localStorage.getItem("openradioinspector.session"); } catch (_error) { return null; }
  }

  function storeSession(sessionId) {
    try {
      if (sessionId) window.localStorage.setItem("openradioinspector.session", sessionId);
      else window.localStorage.removeItem("openradioinspector.session");
    } catch (_error) { /* Выбор сеанса не является архивными данными. */ }
  }

  function renderHealth() {
    if (!state.health) {
      dom.health.textContent = "служба недоступна";
      return;
    }
    dom.health.textContent = `${state.health.status} · сеансов ${state.health.sessions} · событие #${state.health.last_event_id}`;
    const features = state.capabilities?.features || {};
    dom.hardware.textContent = `hardware_control=${String(features.hardware_control)}`;
    if (state.capabilities) {
      const persistent = state.capabilities.persistent_archive;
      dom.storage.textContent = `Архив сеансов: ${state.capabilities.session_storage}; восстановление=${String(persistent)}. Автоматический handoff=${String(features.automatic_handoff)}; обнаружение=${String(features.automatic_candidate_detection)}.`;
    }
  }

  function renderSession() {
    const items = state.sessions.map((session) => ({
      value: session.id,
      label: `${session.name} · ${stateLabel(session.state)} · ${session.id.slice(0, 8)}`,
    }));
    setOptions(dom.sessionSelect, items, state.sessionId, state.sessions.length ? undefined : "Нет сохранённых сеансов");
    dom.sessionSelect.disabled = !state.sessions.length || state.actionPending;
    dom.sessionEmpty.textContent = state.sessions.length
      ? `${state.sessions.length} сеанс(ов) в архиве ядра. Идентификаторы и время показаны без сокращения ниже.`
      : "Создайте сеанс через POST /api/v1/sessions; браузер не имитирует приборы и измерения.";

    dom.sessionMeta.replaceChildren();
    if (state.session) {
      const title = makeElement("h2", "session-title", state.session.name);
      const statusLine = makeElement("div", "meta-line");
      statusLine.append(makeBadge(state.session.state));
      const id = makeElement("p", "meta-line", `ID: ${state.session.id}`);
      const started = makeElement("p", "meta-line", `Начало: ${formatTime(state.session.started_at)}`);
      const ended = makeElement("p", "meta-line", `Окончание: ${formatTime(state.session.ended_at)}`);
      dom.sessionMeta.append(title, statusLine, id, started, ended);
    }

    const running = state.session?.state === "running";
    const draft = state.session?.state === "draft";
    dom.sessionStart.disabled = !draft || state.actionPending;
    dom.sessionStop.disabled = !running || state.actionPending;
    dom.workersStart.disabled = !running || state.actionPending || !state.session?.streams.some((stream) => !workerById(stream.id)?.alive);
    dom.workersStop.disabled = !state.session || state.actionPending || !state.workers.some((worker) => worker.alive);
    dom.refresh.disabled = state.refreshRunning;
  }

  function deviceCurrent(stream, worker) {
    const current = worker?.current || {};
    if (stream.role === "receiver") {
      const frequency = formatHz(current.frequency_hz);
      const mode = current.mode || stream.settings.mode || "режим не задан";
      return `${frequency} · ${mode}`;
    }
    if (current.start_hz !== undefined && current.stop_hz !== undefined) {
      return `${formatHz(current.start_hz)} — ${formatHz(current.stop_hz)} · ${current.points || "—"} точек · output ${current.generator_output || "off"}`;
    }
    return `${formatHz(stream.settings.start_hz)} — ${formatHz(stream.settings.stop_hz)}`;
  }

  function renderDevices() {
    const streams = state.session?.streams || [];
    const alive = state.workers.filter((worker) => worker.alive).length;
    dom.deviceCount.textContent = `${alive}/${streams.length}`;
    if (!streams.length) {
      dom.deviceList.replaceChildren(emptyState(state.session ? "В сеансе нет приборных потоков." : "Потоки появятся после выбора сеанса."));
      return;
    }
    const rows = streams.map((stream) => {
      const worker = workerById(stream.id);
      const row = makeElement("article", "device-row");
      row.dataset.streamId = stream.id;
      const top = makeElement("div", "device-topline");
      top.append(makeElement("h3", "device-name", stream.device), makeBadge(worker?.state || stream.state));
      const role = makeElement("p", "device-role", `${ROLE_LABELS[stream.role] || stream.role} · ${stream.level_unit}`);
      const port = makeElement("p", "meta-line", `${stream.port} · адаптер: ${worker?.adapter || stream.settings.adapter || "не задан"}`);
      const stats = makeElement("div", "device-stats");
      const measurements = makeElement("div");
      measurements.append(document.createTextNode("Измерения"), makeElement("strong", "", worker?.measurements ?? 0));
      const commands = makeElement("div");
      commands.append(document.createTextNode("Команды"), makeElement("strong", "", worker?.commands_completed ?? 0));
      const current = makeElement("div");
      current.append(document.createTextNode("Текущая настройка"), makeElement("strong", "", deviceCurrent(stream, worker)));
      const updated = makeElement("div");
      updated.append(document.createTextNode("Последнее измерение"), makeElement("strong", "", shortTime(worker?.last_measurement_at)));
      stats.append(measurements, commands, current, updated);
      const actions = makeElement("div", "device-actions");
      actions.append(
        makeButton("Запустить", "stream-start", stream.id, { small: true, disabled: !runningSession() || Boolean(worker?.alive) }),
        makeButton("Остановить", "stream-stop", stream.id, { small: true, disabled: !worker?.alive }),
      );
      row.append(top, role, port, stats, actions);
      if (worker?.last_error) row.append(makeElement("p", "error-text", worker.last_error));
      return row;
    });
    dom.deviceList.replaceChildren(...rows);
  }

  function runningSession() {
    return state.session?.state === "running";
  }

  function renderSpectrumSelector() {
    const analyzers = (state.session?.streams || []).filter((stream) => stream.role === "spectrum_analyzer");
    const items = analyzers.map((stream) => ({ value: stream.id, label: `${stream.device} · ${stream.level_unit}` }));
    setOptions(dom.spectrumStream, items, state.spectrumStreamId, analyzers.length ? undefined : "Нет spectrum analyzer");
    dom.spectrumStream.disabled = !analyzers.length || state.actionPending;
    dom.markPeak.disabled = !state.spectrumView.display.length || state.spectrumView.paused || state.actionPending;

    const sources = (state.session?.streams || []).map((stream) => ({
      value: stream.id, label: `${stream.device} · ${stream.level_unit}`,
    }));
    const previousSource = dom.candidateSource.value || state.spectrumStreamId;
    setOptions(dom.candidateSource, sources, previousSource, sources.length ? undefined : "Нет потоков");
    updateCandidateUnit();
  }

  function renderEvents() {
    const events = state.events
      .filter((event) => !state.sessionId || event.session_id === state.sessionId)
      .slice(-30).reverse();
    if (!events.length) {
      dom.eventList.replaceChildren(makeElement("li", "empty-state", state.session ? "Для выбранного сеанса событий пока нет." : "События появятся после выбора сеанса."));
      return;
    }
    const rows = events.map((event) => {
      const row = makeElement("li", "event-row");
      const time = makeElement("time", "", `#${event.sequence} ${shortTime(event.occurred_at)}`);
      time.dateTime = event.occurred_at;
      time.title = formatTime(event.occurred_at);
      row.append(time, makeElement("span", "event-type", event.event_type));
      return row;
    });
    dom.eventList.replaceChildren(...rows);
  }

  function candidateMeta(candidate) {
    const source = streamById(candidate.source_stream_id);
    const parts = [
      `Источник: ${source?.device || candidate.source_stream_id}`,
      `Пик: ${formatLevel(candidate.peak_level, candidate.level_unit)}`,
      `Полоса: ${formatHz(candidate.bandwidth_hz)}`,
      `Начало: ${formatTime(candidate.interval?.started_at)}`,
      `Связанных измерений: ${candidate.observation_ids?.length || 0}`,
      `ID: ${candidate.id}`,
    ];
    return parts;
  }

  function renderCandidates() {
    window.OpenRadioFrequencyRaster?.update({
      sessionId: state.sessionId, candidates: state.candidates,
    });
    window.OpenRadioMeasurementResults?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId,
    });
    const filter = dom.candidateFilter.value;
    const candidates = state.candidates.filter((candidate) => !filter || candidate.state === filter).slice().reverse();
    if (!candidates.length) {
      dom.candidateList.replaceChildren(emptyState(filter ? "Нет кандидатов в выбранном состоянии." : "Кандидатов нет. Зафиксируйте пик реального свипа или создайте запись вручную."));
      return;
    }
    const records = candidates.map((candidate) => {
      const record = makeElement("article", "record");
      record.dataset.candidateId = candidate.id;
      const top = makeElement("div", "record-topline");
      top.append(makeElement("h3", "record-title", streamById(candidate.source_stream_id)?.device || "Кандидат"), makeBadge(candidate.state));
      record.append(top, makeElement("p", "record-frequency", formatHz(candidate.center_frequency_hz)));
      for (const text of candidateMeta(candidate)) record.append(makeElement("p", "record-meta", text));
      if (candidate.score !== null && candidate.score !== undefined) record.append(makeElement("p", "record-meta", `Оценка: ${levelFormatter.format(candidate.score)}`));
      const hasOpen = state.investigations.some((item) => item.candidate_id === candidate.id && OPEN_INVESTIGATIONS.has(item.state));
      const actions = makeElement("div", "record-actions");
      if (["new", "queued"].includes(candidate.state) && !hasOpen) {
        actions.append(makeButton("Назначить receiver", "candidate-handoff", candidate.id, { small: true }));
      }
      if (["new", "queued"].includes(candidate.state) && !hasOpen) {
        actions.append(makeButton("Отклонить", "candidate-dismiss", candidate.id, { small: true }));
      }
      if (actions.childElementCount) record.append(actions);
      return record;
    });
    dom.candidateList.replaceChildren(...records);
  }

  function renderAutomaticDetection() {
    const status = state.automaticDetection;
    const policy = status?.policy;
    const runtime = status?.runtime;
    const enabled = Boolean(policy?.enabled);
    dom.detectionStatus.dataset.enabled = String(enabled);
    if (!status) {
      dom.detectionStatus.textContent = state.sessionId
        ? "Состояние обнаружения ещё не получено от ядра."
        : "Выберите сеанс, чтобы просмотреть политику обнаружения.";
    } else {
      const stream = policy.stream_id
        ? streamById(policy.stream_id)?.device || policy.stream_id
        : "все spectrum_analyzer с dBm";
      const error = runtime.last_error ? ` · ${runtime.last_error}` : "";
      dom.detectionStatus.textContent = `${enabled ? "Включено" : "Выключено"} · ${runtime.last_result} · ${stream} · порог ${levelFormatter.format(policy.threshold_dbm)} dBm · sweeps ${runtime.sweeps_processed}, кандидатов ${runtime.candidates_created}, дублей ${runtime.duplicates_suppressed} · проход ${formatTime(runtime.last_run_at)}${error}`;
    }
    const fields = [dom.detectionInterval, dom.detectionThreshold, dom.detectionDedup, dom.detectionCooldown, dom.detectionMax, dom.detectionStream];
    for (const field of fields) field.disabled = !status || state.actionPending;
    dom.detectionEnable.disabled = !status || !runningSession() || state.actionPending;
    dom.detectionDisable.disabled = !status || !enabled || state.actionPending;
    if (!status || state.detectionFormDirty) return;

    const analyzers = (state.session?.streams || []).filter((stream) => stream.role === "spectrum_analyzer" && stream.level_unit === "dBm");
    const items = analyzers.map((stream) => ({ value: stream.id, label: `${stream.device} · ${stream.level_unit}` }));
    setOptions(dom.detectionStream, items, policy.stream_id, "Все spectrum_analyzer с dBm");
    dom.detectionInterval.value = String(policy.interval_seconds);
    dom.detectionThreshold.value = String(policy.threshold_dbm);
    dom.detectionDedup.value = String(policy.dedup_bandwidth_hz);
    dom.detectionCooldown.value = String(policy.cooldown_seconds);
    dom.detectionMax.value = String(policy.max_candidates_per_sweep);
  }

  function renderAutomaticHandoff() {
    const status = state.automaticHandoff;
    const policy = status?.policy;
    const runtime = status?.runtime;
    const enabled = Boolean(policy?.enabled);
    dom.automaticStatus.dataset.enabled = String(enabled);
    if (!status) {
      dom.automaticStatus.textContent = state.sessionId
        ? "Состояние политики ещё не получено от ядра."
        : "Выберите сеанс, чтобы просмотреть автоматическую политику.";
    } else {
      const receiver = policy.receiver_stream_id
        ? streamById(policy.receiver_stream_id)?.device || policy.receiver_stream_id
        : "любой receiver";
      const last = runtime.last_investigation_id ? ` · исследование ${runtime.last_investigation_id}` : "";
      const error = runtime.last_error ? ` · ${runtime.last_error}` : "";
      dom.automaticStatus.textContent = `${enabled ? "Включён" : "Выключен"} · ${runtime.last_result} · ${receiver} · успешно ${runtime.successful_dispatches}, сбоев ${runtime.failed_dispatches}, серия ${runtime.consecutive_failures} · проход ${formatTime(runtime.last_run_at)}${last}${error}`;
    }

    const fields = [dom.automaticInterval, dom.automaticPriority, dom.automaticFailures, dom.automaticRetry, dom.automaticReceiver];
    for (const field of fields) field.disabled = !status || state.actionPending;
    dom.automaticEnable.disabled = !status || !runningSession() || state.actionPending;
    dom.automaticDisable.disabled = !status || !enabled || state.actionPending;
    if (!status || state.automaticFormDirty) return;

    const receivers = (state.session?.streams || []).filter((stream) => stream.role === "receiver");
    const items = receivers.map((stream) => ({ value: stream.id, label: `${stream.device} · ${stream.level_unit}` }));
    setOptions(dom.automaticReceiver, items, policy.receiver_stream_id, "Любой receiver сеанса");
    dom.automaticInterval.value = String(policy.interval_seconds);
    dom.automaticPriority.value = String(policy.minimum_priority);
    dom.automaticFailures.value = String(policy.max_consecutive_failures);
    dom.automaticRetry.value = String(policy.retry_delay_seconds);
  }

  function renderInvestigations() {
    const next = state.nextInvestigation;
    dom.dispatchNext.disabled = !next || !runningSession() || state.actionPending;
    if (next) {
      const receiver = streamById(next.target_stream_id);
      dom.nextInvestigation.textContent = `Следующее по приоритету: ${formatHz(next.target_frequency_hz)} · приоритет ${next.priority} · ${receiver?.device || next.target_stream_id} · ${next.mode || "режим не изменять"}. Перестройка начнётся только после подтверждения.`;
      dom.dispatchNext.textContent = `Подтвердить ${formatHz(next.target_frequency_hz)}`;
    } else {
      dom.nextInvestigation.textContent = "Ожидающих исследований нет. Ручной запуск требует подтверждения; автоматический — отдельного включения политики.";
      dom.dispatchNext.textContent = "Подтвердить и запустить следующее";
    }
    const filter = dom.investigationFilter.value;
    const investigations = state.investigations.filter((item) => !filter || item.state === filter);
    if (!investigations.length) {
      dom.investigationList.replaceChildren(emptyState(filter ? "Нет исследований в выбранном состоянии." : "Очередь пуста. Выберите кандидата и назначьте receiver."));
      return;
    }
    const records = investigations.map((investigation) => {
      const candidate = candidateById(investigation.candidate_id);
      const receiver = streamById(investigation.target_stream_id);
      const record = makeElement("article", "record");
      record.dataset.investigationId = investigation.id;
      const top = makeElement("div", "record-topline");
      top.append(makeElement("h3", "record-title", receiver?.device || "Receiver"), makeBadge(investigation.state));
      record.append(top, makeElement("p", "record-frequency", formatHz(investigation.target_frequency_hz)));
      const lines = [
        `Режим: ${investigation.mode || "не изменять"} · приоритет ${investigation.priority}`,
        `Кандидат: ${candidate ? formatHz(candidate.center_frequency_hz) : investigation.candidate_id}`,
        `Создано: ${formatTime(investigation.created_at)}`,
        `Начато: ${formatTime(investigation.started_at)}`,
        `Наблюдений в результате: ${investigation.result_observation_ids?.length || 0}`,
        `ID: ${investigation.id}`,
      ];
      for (const text of lines) record.append(makeElement("p", "record-meta", text));
      if (investigation.error) record.append(makeElement("p", "error-text", investigation.error));
      const actions = makeElement("div", "record-actions");
      if (investigation.state === "queued") actions.append(makeButton("Запустить", "investigation-start", investigation.id, { small: true, disabled: !runningSession() }));
      if (["listening", "decoding"].includes(investigation.state)) actions.append(makeButton("Завершить", "investigation-complete", investigation.id, { small: true }));
      if (OPEN_INVESTIGATIONS.has(investigation.state)) {
        actions.append(makeButton("Сбой…", "investigation-fail", investigation.id, { small: true }));
        actions.append(makeButton("Отменить", "investigation-cancel", investigation.id, { small: true }));
      }
      if (actions.childElementCount) record.append(actions);
      return record;
    });
    dom.investigationList.replaceChildren(...records);
  }

  function renderAll() {
    renderHealth();
    renderSession();
    renderDevices();
    renderSpectrumSelector();
    renderSpectrum();
    renderEvents();
    renderCandidates();
    renderAutomaticDetection();
    renderAutomaticHandoff();
    renderInvestigations();
  }

  function sampledIndices(length, peakIndex = -1) {
    if (length <= MAX_POINTS) return Array.from({ length }, (_item, index) => index);
    const indices = new Set([0, length - 1]);
    if (peakIndex >= 0) indices.add(peakIndex);
    const slots = MAX_POINTS - indices.size;
    for (let index = 1; index <= slots; index += 1) {
      indices.add(Math.round(index * (length - 1) / (slots + 1)));
    }
    return Array.from(indices).sort((a, b) => a - b);
  }

  function peakOf(sweep) {
    let index = -1;
    let value = -Infinity;
    sweep.levels.forEach((level, itemIndex) => {
      if (level > value) { value = level; index = itemIndex; }
    });
    return { index, value, frequency_hz: index >= 0 ? sweep.frequencies_hz[index] : null };
  }

  function axisFrequency(value) {
    const absolute = Math.abs(value);
    if (absolute >= 1_000_000_000) return `${levelFormatter.format(value / 1_000_000_000)} ГГц`;
    if (absolute >= 1_000_000) return `${levelFormatter.format(value / 1_000_000)} МГц`;
    if (absolute >= 1_000) return `${levelFormatter.format(value / 1_000)} кГц`;
    return `${integerFormatter.format(value)} Гц`;
  }

  function clearCanvas(canvas, color = "#07131c") {
    const context = canvas.getContext("2d");
    context.fillStyle = color;
    context.fillRect(0, 0, canvas.width, canvas.height);
    return context;
  }

  function updateComparison() {
    const tinyControl = document.getElementById('tinysa-control-link');
    if(tinyControl) tinyControl.href='/instrument?session_id='+encodeURIComponent(state.sessionId||'')+'&stream_id='+encodeURIComponent(state.workers?.find(w=>w.adapter==='tinysa-serial')?.stream_id||state.spectrumStreamId||'');
    window.OpenRadioFrequencyRounding?.update({
      sessionId: state.sessionId, candidates: state.candidates,
    });
    window.OpenRadioFrequencyCounter?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioDvbEvaluation?.update({ sessionId: state.sessionId });
    window.OpenRadioPhonogram?.update({ sessionId: state.sessionId, streams: state.workers?.map(w => ({ id: w.stream_id || w.id, device: w.device })) || [] });
    window.OpenRadioSignalList?.update({ sessionId: state.sessionId, streams: (state.workers || []).map(w => ({ id: w.stream_id || w.id, level_unit: w.level_unit || w.unit || '' })) });
    window.OpenRadioMeasurementMap?.update({ sessionId: state.sessionId });
    window.OpenRadioAudioStream?.update({ sessionId: state.sessionId, streams: (state.workers || []).map(w => ({ id: w.stream_id || w.id, device: w.device })) });
    window.OpenRadioSweepComparison?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioSweepThreshold?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioSweepMask?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioSweepAverage?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioSweepThreeThresholds?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
    window.OpenRadioSweepContext?.update({
      sessionId: state.sessionId, streamId: state.spectrumStreamId, sweeps: state.sweeps,
    });
  }

  function renderSpectrum() {
    updateComparison();
    const view = state.spectrumView;
    const latest = view.display.at(-1);
    renderSpectrumControls();
    dom.spectrumCanvas.hidden = !latest;
    dom.spectrumEmpty.hidden = Boolean(latest);
    if (!latest) {
      clearCanvas(dom.spectrumCanvas);
      clearCanvas(dom.waterfallCanvas);
      dom.range.textContent = "—";
      dom.peak.textContent = "—";
      dom.points.textContent = "—";
      dom.time.textContent = "—";
      dom.waterfallScale.textContent = "ожидание данных";
      dom.spectrumCanvas.setAttribute("aria-label", "Нет спектральных свипов выбранного потока");
      return;
    }
    const peak = peakOf(latest);
    const firstFrequency = latest.frequencies_hz[0];
    const lastFrequency = latest.frequencies_hz.at(-1);
    dom.range.textContent = `${formatHz(firstFrequency)} — ${formatHz(lastFrequency)}`;
    dom.peak.textContent = `${formatHz(peak.frequency_hz)} · ${formatLevel(peak.value, latest.level_unit)}`;
    dom.points.textContent = `${integerFormatter.format(latest.levels.length)} · ${latest.level_unit}`;
    dom.time.textContent = `${latest.interval.started_at} → ${latest.interval.ended_at} · ID: ${latest.id}`;
    dom.spectrumCanvas.setAttribute("aria-label", `Спектр ${axisFrequency(firstFrequency)} — ${axisFrequency(lastFrequency)}, пик ${formatHz(peak.frequency_hz)}, ${formatLevel(peak.value, latest.level_unit)}`);
    drawSpectrum(latest, peak);
    drawWaterfall(view.display);
  }

  function drawSpectrum(sweep, peak) {
    const canvas = dom.spectrumCanvas;
    const context = clearCanvas(canvas);
    const width = canvas.width;
    const height = canvas.height;
    const margin = { left: 78, right: 24, top: 24, bottom: 52 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const minFrequency = sweep.frequencies_hz[0];
    const maxFrequency = sweep.frequencies_hz.at(-1);
    const [minLevel, maxLevel] = state.spectrumView.bounds;
    const x = (frequency) => margin.left + (frequency - minFrequency) / Math.max(maxFrequency - minFrequency, 1) * plotWidth;
    const y = (level) => margin.top + (1 - window.OpenRadioSpectrumView.fraction(level, state.spectrumView.bounds)) * plotHeight;

    context.font = "12px monospace";
    context.lineWidth = 1;
    context.textBaseline = "middle";
    for (let row = 0; row <= 4; row += 1) {
      const level = maxLevel * (1 - row / 4) + minLevel * (row / 4);
      const py = margin.top + plotHeight * row / 4;
      context.strokeStyle = "#18384a";
      context.beginPath();
      context.moveTo(margin.left, py);
      context.lineTo(width - margin.right, py);
      context.stroke();
      context.fillStyle = "#9eb5c2";
      context.textAlign = "right";
      context.fillText(levelFormatter.format(level), margin.left - 10, py);
    }
    for (let column = 0; column <= 5; column += 1) {
      const frequency = minFrequency + (maxFrequency - minFrequency) * column / 5;
      const px = margin.left + plotWidth * column / 5;
      context.strokeStyle = "#18384a";
      context.beginPath();
      context.moveTo(px, margin.top);
      context.lineTo(px, height - margin.bottom);
      context.stroke();
      context.fillStyle = "#9eb5c2";
      context.textAlign = column === 0 ? "left" : column === 5 ? "right" : "center";
      context.textBaseline = "top";
      context.fillText(axisFrequency(frequency), px, height - margin.bottom + 14);
    }

    const indices = sampledIndices(sweep.levels.length, peak.index);
    const gradient = context.createLinearGradient(0, margin.top, 0, height - margin.bottom);
    gradient.addColorStop(0, "rgba(96, 228, 239, 0.24)");
    gradient.addColorStop(1, "rgba(96, 228, 239, 0.01)");
    context.beginPath();
    indices.forEach((index, itemIndex) => {
      const px = x(sweep.frequencies_hz[index]);
      const py = y(sweep.levels[index]);
      if (itemIndex === 0) context.moveTo(px, py);
      else context.lineTo(px, py);
    });
    context.lineTo(x(sweep.frequencies_hz[indices.at(-1)]), height - margin.bottom);
    context.lineTo(x(sweep.frequencies_hz[indices[0]]), height - margin.bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();

    context.beginPath();
    indices.forEach((index, itemIndex) => {
      const px = x(sweep.frequencies_hz[index]);
      const py = y(sweep.levels[index]);
      if (itemIndex === 0) context.moveTo(px, py);
      else context.lineTo(px, py);
    });
    context.strokeStyle = "#60e4ef";
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.stroke();

    const peakX = x(peak.frequency_hz);
    const peakY = y(peak.value);
    context.save();
    context.setLineDash([5, 5]);
    context.strokeStyle = "#ffd36e";
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(peakX, margin.top);
    context.lineTo(peakX, height - margin.bottom);
    context.stroke();
    context.restore();
    context.fillStyle = "#ffd36e";
    context.beginPath();
    context.arc(peakX, peakY, 4, 0, Math.PI * 2);
    context.fill();
    context.textAlign = "left";
    context.textBaseline = "bottom";
    context.fillText(`ПИК · ${sweep.level_unit}`, margin.left + 8, margin.top - 8);
  }

  function waterfallColor(ratio) {
    const stops = [
      [0.0, [5, 15, 28]], [0.25, [16, 65, 95]], [0.5, [31, 170, 184]],
      [0.72, [93, 225, 174]], [0.88, [255, 211, 110]], [1.0, [244, 100, 91]],
    ];
    const value = Math.max(0, Math.min(1, ratio));
    let upper = 1;
    while (upper < stops.length - 1 && stops[upper][0] < value) upper += 1;
    const lower = upper - 1;
    const local = (value - stops[lower][0]) / (stops[upper][0] - stops[lower][0]);
    return stops[lower][1].map((channel, index) => Math.round(channel + (stops[upper][1][index] - channel) * local));
  }

  function waterfallLevels(sweep, minimum, maximum, width) {
    const frequencies = sweep.frequencies_hz;
    const result = Array(width).fill(-Infinity);
    const span = Math.max(maximum - minimum, 1);
    let index = 0;
    for (let x = 0; x < width; x += 1) {
      const left = minimum + x * span / width;
      const right = minimum + (x + 1) * span / width;
      while (index < frequencies.length && frequencies[index] < left) index += 1;
      let cursor = index;
      while (cursor < frequencies.length && (frequencies[cursor] < right || (x === width - 1 && frequencies[cursor] <= right))) {
        result[x] = Math.max(result[x], sweep.levels[cursor]);
        cursor += 1;
      }
      // Empty display pixels interpolate between neighbouring measured points;
      // all processing/exports still use the unchanged measurements in the archive.
      if (result[x] === -Infinity && index > 0 && index < frequencies.length) {
        const a = index - 1;
        const ratio = (left - frequencies[a]) / (frequencies[index] - frequencies[a]);
        result[x] = sweep.levels[a] + ratio * (sweep.levels[index] - sweep.levels[a]);
      }
    }
    return result;
  }

  function drawWaterfall(sweeps) {
    const canvas = dom.waterfallCanvas;
    const context = clearCanvas(canvas);
    const [minLevel, maxLevel] = state.spectrumView.bounds;
    let minimum = Infinity;
    let maximum = -Infinity;
    for (const sweep of sweeps) {
      minimum = Math.min(minimum, sweep.frequencies_hz[0]);
      maximum = Math.max(maximum, sweep.frequencies_hz.at(-1));
    }
    const image = context.createImageData(canvas.width, canvas.height);
    const rowHeight = canvas.height / sweeps.length;
    for (let row = 0; row < sweeps.length; row += 1) {
      const sweep = sweeps[row];
      const yStart = Math.floor(row * rowHeight);
      const yEnd = Math.max(yStart + 1, Math.floor((row + 1) * rowHeight));
      const rowLevels = waterfallLevels(sweep, minimum, maximum, canvas.width);
      for (let x = 0; x < canvas.width; x += 1) {
        const level = rowLevels[x];
        const [red, green, blue] = waterfallColor(window.OpenRadioSpectrumView.fraction(level, state.spectrumView.bounds));
        for (let y = yStart; y < Math.min(yEnd, canvas.height); y += 1) {
          const offset = (y * canvas.width + x) * 4;
          image.data[offset] = red;
          image.data[offset + 1] = green;
          image.data[offset + 2] = blue;
          image.data[offset + 3] = 255;
        }
      }
    }
    context.putImageData(image, 0, 0);
    dom.waterfallScale.textContent = `${sweeps.length} свипов · ${formatLevel(minLevel, sweeps.at(-1).level_unit)} … ${formatLevel(maxLevel, sweeps.at(-1).level_unit)} · старые ↑ / новые ↓ · строки: порядок свипов, не время; столбцы: общая шкала частоты (Гц)`;
    dom.waterfallCanvas.setAttribute("aria-label", `Водопад ${sweeps.length} свипов, уровни ${formatLevel(minLevel, sweeps.at(-1).level_unit)} — ${formatLevel(maxLevel, sweeps.at(-1).level_unit)}`);
  }

  async function performRefresh() {
    const [health, sessions] = await Promise.all([
      api("/health"), api("/sessions"),
    ]);
    state.health = health;
    state.lastEventId = Math.max(state.lastEventId, health.last_event_id || 0);
    state.sessions = sessions;

    const storedId = readStoredSession();
    const requestedId = state.sessionId || storedId;
    const selected = sessions.find((session) => session.id === requestedId) || sessions[0] || null;
    const nextSessionId = selected?.id || null;
    if (nextSessionId !== state.sessionId) {
      state.sessionId = nextSessionId;
      state.session = selected;
      state.workers = [];
      state.candidates = [];
      state.investigations = [];
      state.nextInvestigation = null;
      state.automaticHandoff = null;
      state.automaticDetection = null;
      state.automaticFormDirty = false;
      state.detectionFormDirty = false;
      state.events = [];
      state.sweeps = [];
      state.spectrumView?.clear();
      state.spectrumStreamId = selected?.streams.find((stream) => stream.role === "spectrum_analyzer")?.id || null;
      state.revision = (state.revision || 0) + 1;
      updateComparison();
      storeSession(nextSessionId);
      closeHandoff();
    } else {
      state.session = selected;
    }

    if (!selected) {
      state.spectrumStreamId = null;
      state.nextInvestigation = null;
      state.automaticHandoff = null;
      state.automaticDetection = null;
      state.automaticFormDirty = false;
      state.detectionFormDirty = false;
      return true;
    }
    const analyzers = selected.streams.filter((stream) => stream.role === "spectrum_analyzer");
    if (!analyzers.some((stream) => stream.id === state.spectrumStreamId)) {
      state.spectrumStreamId = analyzers[0]?.id || null;
      state.sweeps = [];
      state.spectrumView?.clear();
      updateComparison();
    }
    const revision = state.revision || 0;
    const sessionId = selected.id;
    const spectrumStreamId = state.spectrumStreamId;
    const measurementsPath = spectrumStreamId
      ? `/sweeps?stream_id=${encode(spectrumStreamId)}&limit=${MAX_SWEEPS}`
      : null;
    const requests = [
      api(`/sessions/${encode(sessionId)}/workers`),
      api(`/sessions/${encode(sessionId)}/candidates`),
      api(`/sessions/${encode(sessionId)}/investigations`),
      api(`/sessions/${encode(sessionId)}/investigations/next`),
      api(`/sessions/${encode(sessionId)}/automatic-handoff`),
      api(`/sessions/${encode(sessionId)}/automatic-detection`),
      api(`/events/history?after=${Math.max(0, health.last_event_id - 200)}&limit=200`),
      measurementsPath ? api(`/sessions/${encode(sessionId)}${measurementsPath}`) : Promise.resolve([]),
    ];
    const [workers, candidates, investigations, nextInvestigation, automaticHandoff, automaticDetection, events, sweeps] = await Promise.all(requests);
    if (state.sessionId !== sessionId || (state.revision || 0) !== revision) {
      state.refreshQueued = true;
      return false;
    }
    state.workers = workers;
    state.candidates = candidates;
    state.investigations = investigations;
    state.nextInvestigation = nextInvestigation;
    state.automaticHandoff = automaticHandoff;
    state.automaticDetection = automaticDetection;
    state.events = events;
    state.sweeps = Array.isArray(sweeps) ? sweeps.slice(-MAX_SWEEPS) : [];
    state.spectrumView.update({ server: window.location.origin, session: sessionId, stream: spectrumStreamId },
      streamById(spectrumStreamId)?.level_unit === "dBm" ? sweeps : []);
    return true;
  }

  async function refresh(options = {}) {
    const silent = Boolean(options.silent);
    if (state.refreshRunning) {
      state.refreshQueued = true;
      return;
    }
    state.refreshRunning = true;
    window.OpenRadioArchiveCapacity?.refresh();
    renderSession();
    try {
      state.refreshQueued = false;
      const fresh = await performRefresh();
      if (fresh) {
        window.OpenRadioFrequencyRounding?.update({
          sessionId: state.sessionId, candidates: state.candidates, fresh: true,
        });
        window.OpenRadioFrequencyRounding?.setConnected(true);
      }
      window.OpenRadioSweepThreshold?.setConnected(true);
      window.OpenRadioSweepMask?.setConnected(true);
      window.OpenRadioSweepAverage?.setConnected(true);
      window.OpenRadioSweepThreeThresholds?.setConnected(true);
      window.OpenRadioSweepContext?.setConnected(true);
      window.OpenRadioFrequencyRaster?.setConnected(true);
      window.OpenRadioMeasurementResults?.setConnected(true);
      window.OpenRadioFrequencyCounter?.setConnected(true);
      window.OpenRadioDvbEvaluation?.setConnected(true);
      window.OpenRadioPhonogram?.setConnected(true);
      window.OpenRadioSignalList?.setConnected(true);
      window.OpenRadioMeasurementMap?.setConnected(true);
      window.OpenRadioAudioStream?.setConnected(true);
    } catch (error) {
      state.spectrumView?.clear();
      state.sweeps = [];
      state.health = null;
      window.OpenRadioFrequencyRounding?.setConnected(false);
      window.OpenRadioSweepThreshold?.setConnected(false);
      window.OpenRadioSweepMask?.setConnected(false);
      window.OpenRadioSweepAverage?.setConnected(false);
      window.OpenRadioSweepThreeThresholds?.setConnected(false);
      window.OpenRadioSweepContext?.setConnected(false);
      window.OpenRadioFrequencyRaster?.setConnected(false);
      window.OpenRadioMeasurementResults?.setConnected(false);
      window.OpenRadioFrequencyCounter?.setConnected(false);
      window.OpenRadioDvbEvaluation?.setConnected(false);
      window.OpenRadioPhonogram?.setConnected(false);
      window.OpenRadioSignalList?.setConnected(false);
      window.OpenRadioMeasurementMap?.setConnected(false);
      window.OpenRadioAudioStream?.setConnected(false);
      if (!silent) reportError(error);
      else dom.health.textContent = `ошибка обновления: ${error.message}`;
    } finally {
      state.refreshRunning = false;
      state.initialized = true;
      renderAll();
      if (state.refreshQueued) {
        state.refreshQueued = false;
        scheduleRefresh(1000);
      }
    }
  }

  function scheduleRefresh(delay = 150) {
    if (state.refreshTimer !== null) return;
    state.refreshTimer = window.setTimeout(() => {
      state.refreshTimer = null;
      refresh({ silent: true });
    }, delay);
  }

  function setSseStatus(text, tone) {
    dom.sseStatus.replaceChildren(makeElement("span", "status-dot"), document.createTextNode(text));
    dom.sseStatus.dataset.tone = tone;
  }

  function connectEvents() {
    if (!("EventSource" in window)) {
      setSseStatus("SSE недоступен · polling", "warning");
      return;
    }
    if (state.eventSource) state.eventSource.close();
    setSseStatus("SSE подключение", "muted");
    const source = new EventSource(`${API_ROOT}/events?after=${encode(state.lastEventId)}`);
    state.eventSource = source;
    source.addEventListener("open", () => setSseStatus("SSE подключено", "live"));
    source.addEventListener("error", () => {
      setSseStatus("SSE переподключение · polling", "warning");
      window.OpenRadioFrequencyRounding?.setConnected(false);
      window.OpenRadioSweepAverage?.setConnected(false);
      window.OpenRadioSweepThreeThresholds?.setConnected(false);
      window.OpenRadioSweepContext?.setConnected(false);
      window.OpenRadioFrequencyRaster?.setConnected(false);
      window.OpenRadioMeasurementResults?.setConnected(false);
      window.OpenRadioFrequencyCounter?.setConnected(false);
      window.OpenRadioDvbEvaluation?.setConnected(false);
      window.OpenRadioPhonogram?.setConnected(false);
      window.OpenRadioSignalList?.setConnected(false);
      window.OpenRadioMeasurementMap?.setConnected(false);
      window.OpenRadioAudioStream?.setConnected(false);
    });
    const receive = (message) => {
      try {
        const envelope = JSON.parse(message.data);
        const event = envelope.data ?? envelope;
        if (!event || typeof event !== "object") throw new Error("Invalid event envelope");
        const sequence = Number(message.lastEventId || event.sequence || 0);
        if (Number.isSafeInteger(sequence)) state.lastEventId = Math.max(state.lastEventId, sequence);
        setSseStatus(`SSE live · #${state.lastEventId}`, "live");
        const relevant = !event.session_id || event.session_id === state.sessionId || event.event_type === "session.created";
        if (relevant) scheduleRefresh(event.event_type === "sweep.received" || event.event_type === "observation.received" ? 1500 : 80);
      } catch (_error) {
        setSseStatus("SSE · некорректное событие", "error");
      }
    };
    for (const eventType of EVENT_TYPES) source.addEventListener(eventType, receive);
  }

  async function withAction(action) {
    if (state.actionPending) return null;
    state.actionPending = true;
    renderAll();
    try {
      const result = await action();
      await refresh({ silent: true });
      return result;
    } catch (error) {
      reportError(error);
      return null;
    } finally {
      state.actionPending = false;
      renderAll();
    }
  }

  function updateCandidateUnit() {
    const stream = streamById(dom.candidateSource.value);
    dom.candidateUnit.textContent = stream?.level_unit || "—";
  }

  function markLatestPeak() {
    if (state.spectrumView.paused) return;
    const sweep = state.spectrumView.display.at(-1);
    if (!sweep) return;
    const peak = peakOf(sweep);
    dom.candidateSource.value = sweep.stream_id;
    updateCandidateUnit();
    dom.candidateStarted.value = sweep.interval.started_at;
    dom.candidateEnded.value = sweep.interval.ended_at;
    dom.candidateFrequency.value = String(peak.frequency_hz);
    const step = sweep.frequencies_hz.length > 1
      ? Math.abs(sweep.frequencies_hz[1] - sweep.frequencies_hz[0])
      : 1;
    dom.candidateBandwidth.value = String(sweep.settings?.rbw_hz || Math.max(step, 1));
    dom.candidateLevel.value = String(peak.value);
    dom.candidateScore.value = "";
    dom.candidateForm.dataset.observationId = sweep.id;
    dom.candidateEditor.open = true;
    dom.candidateFrequency.focus();
    notify(`Пик ${formatHz(peak.frequency_hz)} взят из свипа ${sweep.id}; единица ${sweep.level_unit} сохранена.`);
  }

  function requiredInteger(input, label, minimum = 1) {
    const value = input.valueAsNumber;
    if (!Number.isSafeInteger(value) || value < minimum) throw new ApiError(`${label}: требуется целое число ≥ ${minimum}.`, "invalid_form");
    return value;
  }

  function requiredNumber(input, label, minimum, maximum) {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw new ApiError(`${label}: требуется число от ${minimum} до ${maximum}.`, "invalid_form");
    }
    return value;
  }

  function optionalNumber(input, label, integer = false) {
    if (input.value.trim() === "") return undefined;
    const value = input.valueAsNumber;
    if (!Number.isFinite(value) || (integer && !Number.isSafeInteger(value))) {
      throw new ApiError(`${label}: некорректное число.`, "invalid_form");
    }
    return value;
  }

  async function createCandidate() {
    if (!state.sessionId) throw new ApiError("Сначала выберите сеанс.", "invalid_form");
    const source = streamById(dom.candidateSource.value);
    if (!source) throw new ApiError("Выберите исходный поток.", "invalid_form");
    const bandwidth = optionalNumber(dom.candidateBandwidth, "Полоса", true);
    if (bandwidth !== undefined && bandwidth < 1) throw new ApiError("Полоса должна быть положительным целым числом Гц.", "invalid_form");
    const observationId = dom.candidateForm.dataset.observationId;
    const payload = {
      source_stream_id: source.id,
      interval: { started_at: dom.candidateStarted.value.trim(), ended_at: dom.candidateEnded.value.trim() },
      center_frequency_hz: requiredInteger(dom.candidateFrequency, "Центральная частота"),
      level_unit: source.level_unit,
      observation_ids: observationId ? [observationId] : [],
    };
    const peak = optionalNumber(dom.candidateLevel, "Пик");
    const score = optionalNumber(dom.candidateScore, "Оценка");
    if (bandwidth !== undefined) payload.bandwidth_hz = bandwidth;
    if (peak !== undefined) payload.peak_level = peak;
    if (score !== undefined) payload.score = score;
    const candidate = await withAction(() => api(sessionPath("/candidates"), { method: "POST", body: payload }));
    if (candidate) {
      notify(`Кандидат ${candidate.id} создан в единицах ${candidate.level_unit}.`, "completed");
      dom.candidateForm.reset();
      delete dom.candidateForm.dataset.observationId;
      dom.candidateEditor.open = false;
      updateCandidateUnit();
    }
  }

  async function configureAutomaticDetection(enabled) {
    if (!state.sessionId) throw new ApiError("Сначала выберите сеанс.", "invalid_form");
    const payload = { enabled };
    if (enabled) {
      if (!runningSession()) throw new ApiError("Обнаружение можно включить только в работающем сеансе.", "invalid_form");
      payload.interval_seconds = requiredNumber(dom.detectionInterval, "Период проверки", 0.1, 3600);
      payload.threshold_dbm = requiredNumber(dom.detectionThreshold, "Порог", -300, 100);
      payload.dedup_bandwidth_hz = requiredInteger(dom.detectionDedup, "Полоса dedup", 1);
      payload.cooldown_seconds = requiredNumber(dom.detectionCooldown, "Cooldown", 0, 86400);
      payload.max_candidates_per_sweep = requiredInteger(dom.detectionMax, "Максимум за sweep", 1);
      payload.stream_id = dom.detectionStream.value || null;
    }
    const status = await withAction(() => api(sessionPath("/automatic-detection"), { method: "POST", body: payload }));
    if (!status) return;
    state.automaticDetection = status;
    state.detectionFormDirty = false;
    renderAutomaticDetection();
    notify(
      status.policy.enabled
        ? "Автоматическое обнаружение явно включено только для новых sweeps; исследования в очередь не ставятся."
        : "Автоматическое обнаружение выключено оператором; архив и кандидаты сохранены.",
      status.policy.enabled ? "running" : "warning",
    );
  }

  async function configureAutomaticHandoff(enabled) {
    if (!state.sessionId) throw new ApiError("Сначала выберите сеанс.", "invalid_form");
    const payload = { enabled };
    if (enabled) {
      if (!runningSession()) throw new ApiError("Автоматический handoff можно включить только в работающем сеансе.", "invalid_form");
      payload.interval_seconds = requiredNumber(dom.automaticInterval, "Период проверки", 0.1, 3600);
      payload.minimum_priority = requiredInteger(dom.automaticPriority, "Минимальный приоритет", 0);
      payload.max_consecutive_failures = requiredInteger(dom.automaticFailures, "Сбоев до отключения", 1);
      payload.retry_delay_seconds = requiredNumber(dom.automaticRetry, "Задержка повтора", 0.1, 86400);
      payload.receiver_stream_id = dom.automaticReceiver.value || null;
    }
    const status = await withAction(() => api(sessionPath("/automatic-handoff"), { method: "POST", body: payload }));
    if (!status) return;
    state.automaticHandoff = status;
    state.automaticFormDirty = false;
    renderAutomaticHandoff();
    notify(
      status.policy.enabled
        ? "Автоматический handoff явно включён. Он запускает только подходящие ожидающие исследования."
        : "Автоматический handoff выключен оператором; активное исследование не отменено, параметры сохранены до перезапуска.",
      status.policy.enabled ? "running" : "warning",
    );
  }

  function closeHandoff() {
    dom.handoffForm.hidden = true;
    dom.handoffCandidate.value = "";
    dom.handoffLabel.textContent = "";
  }

  function openHandoff(candidateId) {
    const candidate = candidateById(candidateId);
    if (!candidate) return;
    const receivers = (state.session?.streams || []).filter((stream) => stream.role === "receiver");
    if (!receivers.length) {
      notify("В сеансе нет потока receiver для handoff.", "error");
      return;
    }
    const items = receivers.map((stream) => ({ value: stream.id, label: `${stream.device} · ${stream.level_unit}` }));
    setOptions(dom.handoffTarget, items, receivers[0].id);
    dom.handoffCandidate.value = candidate.id;
    dom.handoffLabel.textContent = `${candidate.id} · ${formatHz(candidate.center_frequency_hz)} · ${candidate.level_unit}`;
    dom.handoffFrequency.value = String(candidate.center_frequency_hz);
    dom.handoffPriority.value = "0";
    dom.handoffMode.value = workerById(receivers[0].id)?.current?.mode || receivers[0].settings.mode || "";
    dom.handoffForm.hidden = false;
    dom.handoffTarget.focus();
    dom.handoffForm.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function enqueueInvestigation() {
    const candidateId = dom.handoffCandidate.value;
    const candidate = candidateById(candidateId);
    if (!candidate) throw new ApiError("Кандидат больше не доступен. Обновите данные.", "invalid_form");
    const target = streamById(dom.handoffTarget.value);
    if (!target || target.role !== "receiver") throw new ApiError("Выберите receiver того же сеанса.", "invalid_form");
    const payload = {
      target_stream_id: target.id,
      target_frequency_hz: requiredInteger(dom.handoffFrequency, "Частота handoff"),
      priority: requiredInteger(dom.handoffPriority, "Приоритет", 0),
    };
    const mode = dom.handoffMode.value.trim();
    if (mode) payload.mode = mode;
    const investigation = await withAction(() => api(sessionPath(`/candidates/${encode(candidateId)}/investigations`), { method: "POST", body: payload }));
    if (investigation) {
      notify(`Исследование ${investigation.id} поставлено в очередь. Запуск выполняется вручную.`, "queued");
      closeHandoff();
    }
  }

  function changeSession() {
    state.sessionId = dom.sessionSelect.value || null;
    state.spectrumStreamId = null;
    dom.candidateForm.reset();
    delete dom.candidateForm.dataset.observationId;
    dom.candidateEditor.open = false;
    state.session = null;
    state.sweeps = [];
    state.spectrumView?.clear();
    if (state.spectrumView) renderSpectrum();
    updateComparison();
    state.automaticHandoff = null;
    state.automaticDetection = null;
    state.automaticFormDirty = false;
    state.detectionFormDirty = false;
    state.revision = (state.revision || 0) + 1;
    storeSession(state.sessionId);
    closeHandoff();
    refresh();
  }

  function changeSpectrumStream() {
    state.spectrumStreamId = dom.spectrumStream.value || null;
    state.sweeps = [];
    state.spectrumView?.clear();
    if (state.spectrumView) renderSpectrum();
    updateComparison();
    state.revision = (state.revision || 0) + 1;
    refresh();
  }

  function renderSpectrumControls() {
    const view = state.spectrumView;
    byId("spectrum-pause").disabled = !view.display.length;
    byId("spectrum-pause").textContent = view.paused ? "Live" : "Pause view";
    byId("spectrum-fit").disabled = !view.display.length;
    byId("spectrum-apply").disabled = !view.display.length;
    byId("spectrum-view-status").textContent = view.display.length
      ? `${view.paused ? "PAUSED: пауза только просмотра; состояние сбора не изменено" : "LIVE"} · отсечено значений: ${view.clipped()} · шкала ${view.bounds[0]} … ${view.bounds[1]} dBm`
      : "Нет доступного спектра";
    byId("spectrum-scale-error").textContent = view.error;
    dom.markPeak.disabled = !view.display.length || view.paused || state.actionPending;
  }
  byId("spectrum-pause").addEventListener("click", () => {
    if (state.spectrumView.paused) state.spectrumView.resume(); else state.spectrumView.pause();
    renderSpectrum();
  });
  byId("spectrum-fit").addEventListener("click", () => { state.spectrumView.fit(); renderSpectrum(); });
  byId("spectrum-scale-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const read = (id) => {
      const text = byId(id).value.replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "");
      return /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(text) ? Number(text) : NaN;
    };
    state.spectrumView.manual(read("spectrum-min"), read("spectrum-max"));
    renderSpectrum();
  });

  dom.refresh.addEventListener("click", () => refresh());
  dom.sessionSelect.addEventListener("change", changeSession);
  dom.spectrumStream.addEventListener("change", changeSpectrumStream);
  dom.sessionStart.addEventListener("click", () => withAction(async () => {
    await api(sessionPath("/start"), { method: "POST", body: {} });
    notify("Сеанс запущен. Приборы запускаются отдельно.", "running");
  }));
  dom.sessionStop.addEventListener("click", () => withAction(async () => {
    await api(sessionPath("/stop"), { method: "POST", body: {} });
    notify("Сеанс и открытые исследования остановлены ядром.", "warning");
  }));
  dom.workersStart.addEventListener("click", () => withAction(async () => {
    const result = await api(sessionPath("/workers/start"), { method: "POST", body: {} });
    const alive = result.filter((worker) => worker.alive).length;
    const failed = result.length - alive;
    notify(`Запрошены исполнители: работают ${alive}, ошибок ${failed}.`, failed ? "warning" : "running");
  }));
  dom.workersStop.addEventListener("click", () => withAction(async () => {
    await api(sessionPath("/workers/stop"), { method: "POST", body: {} });
    notify("Все исполнители выбранного сеанса остановлены.", "warning");
  }));
  dom.markPeak.addEventListener("click", markLatestPeak);
  dom.candidateSource.addEventListener("change", () => {
    updateCandidateUnit();
    delete dom.candidateForm.dataset.observationId;
  });
  dom.candidateCancel.addEventListener("click", () => { dom.candidateEditor.open = false; });
  dom.candidateForm.addEventListener("submit", (event) => {
    event.preventDefault();
    createCandidate().catch(reportError);
  });
  dom.candidateFilter.addEventListener("change", renderCandidates);
  dom.investigationFilter.addEventListener("change", renderInvestigations);
  dom.handoffCancel.addEventListener("click", closeHandoff);
  dom.handoffForm.addEventListener("submit", (event) => {
    event.preventDefault();
    enqueueInvestigation().catch(reportError);
  });
  dom.handoffTarget.addEventListener("change", () => {
    const stream = streamById(dom.handoffTarget.value);
    dom.handoffMode.value = workerById(stream?.id)?.current?.mode || stream?.settings.mode || "";
  });
  dom.detectionForm.addEventListener("input", () => { state.detectionFormDirty = true; });
  dom.detectionForm.addEventListener("change", () => { state.detectionFormDirty = true; });
  dom.detectionForm.addEventListener("submit", (event) => {
    event.preventDefault();
    configureAutomaticDetection(true).catch(reportError);
  });
  dom.detectionDisable.addEventListener("click", () => {
    configureAutomaticDetection(false).catch(reportError);
  });
  dom.automaticForm.addEventListener("input", () => { state.automaticFormDirty = true; });
  dom.automaticForm.addEventListener("change", () => { state.automaticFormDirty = true; });
  dom.automaticForm.addEventListener("submit", (event) => {
    event.preventDefault();
    configureAutomaticHandoff(true).catch(reportError);
  });
  dom.automaticDisable.addEventListener("click", () => {
    configureAutomaticHandoff(false).catch(reportError);
  });
  dom.dispatchNext.addEventListener("click", () => {
    const next = state.nextInvestigation;
    if (!next) return;
    withAction(async () => {
      const investigation = await api(sessionPath("/investigations/start-next"), {
        method: "POST",
        body: { investigation_id: next.id, confirm: true },
      });
      if (investigation.state === "failed") {
        notify(`Запуск завершился сбоем: ${investigation.error}`, "error");
      } else {
        notify(`Подтверждённое исследование ${investigation.id} переведено в прослушивание.`, "listening");
      }
    });
  });

  dom.deviceList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const streamId = button.dataset.id;
    const action = button.dataset.action === "stream-start" ? "start" : "stop";
    withAction(async () => {
      const worker = await api(sessionPath(`/streams/${encode(streamId)}/${action}`), { method: "POST", body: {} });
      notify(`${worker.device}: ${action === "start" ? "исполнитель запущен" : "исполнитель остановлен"}. Другие потоки не останавливались.`, action === "start" ? "running" : "warning");
    });
  });

  dom.candidateList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const candidateId = button.dataset.id;
    if (button.dataset.action === "candidate-handoff") {
      openHandoff(candidateId);
      return;
    }
    withAction(async () => {
      const candidate = await api(sessionPath(`/candidates/${encode(candidateId)}/dismiss`), { method: "POST", body: {} });
      notify(`Кандидат ${candidate.id} отклонён; измерения сохранены в архиве.`, "warning");
    });
  });

  dom.investigationList.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const investigationId = button.dataset.id;
    const action = button.dataset.action.replace("investigation-", "");
    let failureReason;
    if (action === "fail") {
      failureReason = window.prompt("Причина сбоя исследования (обязательное поле error):", "Остановлено оператором через WebUI");
      if (failureReason === null) return;
      if (!failureReason.trim()) {
        notify("Причина сбоя не должна быть пустой.", "error");
        return;
      }
    }
    withAction(async () => {
      const body = action === "fail" ? { error: failureReason.trim() } : {};
      const investigation = await api(sessionPath(`/investigations/${encode(investigationId)}/${action}`), { method: "POST", body });
      if (investigation.state === "failed" && action === "start") {
        notify(`Запуск завершился сбоем: ${investigation.error}`, "error");
      } else {
        const messages = {
          start: "Receiver настроен; исследование переведено в прослушивание.",
          complete: `Исследование завершено; связанных наблюдений: ${investigation.result_observation_ids.length}.`,
          fail: "Сбой исследования зафиксирован; кандидат возвращён в очередь.",
          cancel: "Исследование отменено; кандидат возвращён в очередь.",
        };
        notify(messages[action], action === "complete" ? "completed" : action === "start" ? "listening" : "warning");
      }
    });
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") scheduleRefresh(0);
  });
  window.addEventListener("beforeunload", () => {
    if (state.eventSource) state.eventSource.close();
  });
  window.addEventListener('ori-monitoring-session', event => {
    const id = event.detail?.sessionId;
    if (typeof id !== 'string' || !id || id === state.sessionId) return;
    if (![...dom.sessionSelect.options].some(option => option.value === id)) {
      dom.sessionSelect.add(new Option('Сеанс проекта наблюдения', id));
    }
    dom.sessionSelect.value = id;
    changeSession();
  });

  async function initialize() {
    renderAll();
    try {
      state.capabilities = await api("/capabilities");
    } catch (error) {
      reportError(error);
    }
    await refresh();
    connectEvents();
    window.setInterval(() => refresh({ silent: true }), 5000);
  }

  initialize().catch(reportError);
})();
