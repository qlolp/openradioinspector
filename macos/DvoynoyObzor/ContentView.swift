import SwiftUI

struct ContentView: View {
    @StateObject private var model = RemoteDualModel()

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Двойной обзор · общее ядро")
                            .font(.title.weight(.semibold))
                        Text("macOS — тонкий клиент /api/v1. Приборами и архивом владеет сервер; слева исходные отсчёты S-метра, справа dBm.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Circle()
                        .fill(model.connected ? Color.green : Color.secondary)
                        .frame(width: 10, height: 10)
                }
                HStack {
                    TextField("http://127.0.0.1:8770", text: $model.serverURL)
                        .textFieldStyle(.roundedBorder)
                        .disabled(model.connected)
                    if model.connected {
                        Button("Отключить UI") { model.disconnect() }
                        Button("Обновить") { Task { await model.reloadSessions() } }
                    } else {
                        Button("Подключить") { model.connect() }
                    }
                }
                HStack {
                    Picker("Сеанс", selection: Binding(
                        get: { model.selectedSessionID },
                        set: { model.selectSession($0) }
                    )) {
                        Text("Не выбран").tag("")
                        ForEach(model.sessions) { session in
                            Text("\(session.name) · \(session.state)").tag(session.id)
                        }
                    }
                    .disabled(!model.connected || model.busy)
                    Button("Новый draft") { model.createSession(simulated: false) }
                        .disabled(!model.connected || model.busy)
                    Button("Новый с симуляторами") { model.createSession(simulated: true) }
                        .disabled(!model.connected || model.busy)
                    Button("Новый serial…") { model.showSerialForm = true }
                        .disabled(!model.connected || model.busy)
                }
                HStack {
                    Button("Запустить сеанс") { model.startSession() }
                        .disabled(model.session == nil || model.busy)
                    Button("Остановить сеанс") { model.stopSession() }
                        .disabled(model.session == nil || model.busy)
                    Button("Запустить оба потока") { model.startWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Button("Остановить оба потока") { model.stopWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Spacer()
                    Text(model.status).font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            Divider()
            VSplitView {
                HSplitView {
                    RemoteArPane(model: model).frame(minWidth: 460)
                    RemoteUltraPane(model: model).frame(minWidth: 460)
                }
                RemoteWorkflowPane(model: model).frame(minHeight: 280)
            }
        }
        .frame(minWidth: 980, minHeight: 860)
        .sheet(isPresented: $model.showSerialForm) {
            RemoteSerialSessionPane(model: model)
        }
        .background(WindowCloseObserver {
            // Закрытие UI не останавливает измерения и не закрывает приборы ядра.
            model.disconnect()
        })
    }
}

import Combine
import Foundation
import SpektrKomandy
import SwiftUI

@MainActor
final class RemoteDualModel: ObservableObject {
    @Published var serverURL = "http://127.0.0.1:8770"
    @Published var sessions: [PlatformSession] = []
    @Published var selectedSessionID = ""
    @Published var session: PlatformSession?
    @Published var workers: [PlatformWorker] = []
    @Published var connected = false
    @Published var busy = false
    @Published var status = "Сервер не подключён"
    @Published var errorText: String?
    @Published var mode = "FM"
    @Published var threshold = "-80"
    @Published var current: [PlotPoint] = []
    @Published var minimum: [PlotPoint] = []
    @Published var maximum: [PlotPoint] = []
    @Published var rows: [Exceedance] = []
    @Published var waterfall = WaterfallHistory()
    @Published var waterfallRevision = 0
    @Published var latestObservation: PlatformObservation?
    @Published var live = "Данные ядра ещё не получены"
    @Published var candidates: [PlatformCandidate] = []
    @Published var investigations: [PlatformInvestigation] = []
    @Published var nextInvestigation: PlatformInvestigation?
    @Published var selectedCandidateID = ""
    @Published var selectedInvestigationID = ""
    @Published var investigationPriority = "0"
    @Published var failureReason = ""
    @Published var automaticHandoffStatus: PlatformAutomaticStatus?
    @Published var automaticDetectionStatus: PlatformAutomaticStatus?
    @Published var automaticCompletionStatus: PlatformAutomaticStatus?
    @Published var taskSchedulerStatus: PlatformTaskSchedulerStatus?
    @Published var taskSchedulerError: String?
    @Published var taskSchedulerTasks: [PlatformControlTask] = []
    @Published var taskSchedulerTasksError: String?
    @Published var newTaskName = ""
    @Published var newTaskFile = ""
    @Published var newTaskStartAt = ""
    @Published var newTaskDuration = "60"
    @Published var newTaskEquipment = ""
    @Published var newTaskStatus = "Новая ControlTask ещё не создана"
    @Published var showTaskCreate = false
    @Published var taskCreatePending = false
    @Published var taskCancelStatus = "Queued ControlTask не отменена"
    @Published var taskCancelPending = false
    @Published var archiveAnalytics: PlatformAnalyticsSummary?
    @Published var archiveAnalyticsError: String?
    @Published var archiveAnalyticsPending = false
    @Published var archiveExportStatus = "NDJSON snapshot ещё не выгружен"
    @Published var archiveExportPending = false
    @Published var workflowErrors: [String] = []
    @Published var detectionInterval = "1"
    @Published var detectionStreamID = ""
    @Published var detectionThreshold = "-70"
    @Published var detectionDedupBandwidth = "100000"
    @Published var detectionCooldown = "60"
    @Published var detectionMaximumCandidates = "10"
    @Published var handoffInterval = "1"
    @Published var handoffMinimumPriority = "0"
    @Published var handoffReceiverStreamID = ""
    @Published var handoffMaximumFailures = "3"
    @Published var handoffRetryDelay = "30"
    @Published var completionInterval = "1"
    @Published var completionMinimumListening = "0"
    @Published var completionMinimumObservations = "1"
    @Published var completionReceiverStreamID = ""
    @Published var tinySAPort = "/dev/ttyACM0"
    @Published var receiverPort = "/dev/ttyUSB0"
    @Published var showSerialForm = false

    private var client: PlatformClient?
    private var pollTask: Task<Void, Never>?
    private var generation = 0
    private var absorbedSweepIDs: Set<String> = []
    private var detectionFormInitialized = false
    private var handoffFormInitialized = false
    private var completionFormInitialized = false
    private var taskSchedulerCommandRevision = 0

    var analyzer: PlatformStream? { session?.streams.first(where: { $0.isDbmAnalyzer }) }
    var receiver: PlatformStream? { session?.streams.first(where: { $0.isReceiver }) }
    var thresholdDbm: Double? { parseUserNumber(threshold) }
    var selectedCandidate: PlatformCandidate? {
        candidates.first(where: { $0.id == selectedCandidateID })
    }
    var selectedInvestigation: PlatformInvestigation? {
        investigations.first(where: { $0.id == selectedInvestigationID })
    }

    func worker(for streamID: String?) -> PlatformWorker? {
        guard let streamID else { return nil }
        return workers.first(where: { $0.streamId == streamID })
    }

    func connect() {
        do {
            client = try PlatformClient(baseURL: serverURL)
            connected = true
            errorText = nil
            status = "Сервер подключён; приборные порты остаются у ядра"
            generation += 1
            startPolling()
            Task { await reloadSessions() }
        } catch {
            connected = false
            errorText = describeSession(error)
        }
    }

    func disconnect() {
        generation += 1
        pollTask?.cancel()
        pollTask = nil
        client = nil
        connected = false
        busy = false
        session = nil
        sessions = []
        selectedSessionID = ""
        clearMeasurements()
        status = "Отключено от сервера; сеанс ядра не остановлен"
    }

    func reloadSessions() async {
        guard let client else { return }
        let requestGeneration = generation
        do {
            let loaded = try await client.listSessions()
            guard requestGeneration == generation, self.client === client else { return }
            sessions = loaded
            if selectedSessionID.isEmpty || !loaded.contains(where: { $0.id == selectedSessionID }) {
                selectedSessionID = loaded.first?.id ?? ""
                clearMeasurements()
            }
            await refresh()
        } catch {
            guard requestGeneration == generation, self.client === client else { return }
            errorText = describeSession(error)
        }
    }

    func selectSession(_ id: String) {
        guard id != selectedSessionID else { return }
        generation += 1
        selectedSessionID = id
        busy = false
        clearMeasurements()
        Task { await refresh() }
    }

    func createSession(simulated: Bool) {
        guard let client else { return }
        let requestGeneration = generation
        busy = true
        Task {
            do {
                let created = try await client.createSession(
                    name: simulated ? "Двойной обзор · симуляторы" : "Двойной обзор",
                    simulated: simulated
                )
                let loaded = try await client.listSessions()
                guard requestGeneration == generation, self.client === client else { return }
                sessions = loaded
                selectedSessionID = created.id
                clearMeasurements()
                status = simulated ? "Создан сеанс с явно выбранными симуляторами" : "Создан draft без hardware_control"
                await refresh()
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                errorText = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            busy = false
        }
    }

    func createSerialSession() {
        guard let client else { return }
        let spectrumPort = tinySAPort
        let targetPort = receiverPort
        let requestGeneration = generation
        busy = true
        Task {
            do {
                let created = try await client.createSession(
                    name: "Двойной обзор · serial",
                    tinySAPort: spectrumPort, receiverPort: targetPort
                )
                let loaded = try await client.listSessions()
                guard requestGeneration == generation, self.client === client else { return }
                sessions = loaded
                selectedSessionID = created.id
                clearMeasurements()
                status = "Создан serial-профиль с явными адаптерами; стенд не заявляется"
                await refresh()
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                errorText = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            busy = false
        }
    }

    func refresh() async {
        guard let client else { return }
        if selectedSessionID.isEmpty {
            await refreshTaskScheduler(client)
            return
        }
        let id = selectedSessionID
        let requestGeneration = generation
        let schedulerRevision = taskSchedulerCommandRevision
        do {
            let snapshot = try await client.snapshot(sessionID: id)
            guard requestGeneration == generation, id == selectedSessionID else { return }
            session = snapshot.session
            if let modes = receiver?.tuneModes, !modes.contains(mode) {
                mode = modes.first ?? "FM"
            }
            workers = snapshot.workers
            applyMeasurements(snapshot)
            applyWorkflow(snapshot, schedulerRevision: schedulerRevision)
            errorText = snapshot.workflowErrors.isEmpty ? nil : snapshot.workflowErrors.joined(separator: " · ")
            live = "Обновлено от ядра · свипов \(snapshot.sweeps.count) · наблюдений \(snapshot.observations.count)"
        } catch {
            guard requestGeneration == generation, id == selectedSessionID else { return }
            errorText = describeSession(error)
        }
    }

    func startSession() { performSessionAction { try await $0.startSession($1) } }
    func stopSession() { performSessionAction { try await $0.stopSession($1) } }
    func startWorkers() { performSessionAction { try await $0.startWorkers($1) } }
    func stopWorkers() { performSessionAction { try await $0.stopWorkers($1) } }

    func startStream(_ stream: PlatformStream?) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.startStream(sessionID: $1, streamID: stream.id) }
    }

    func stopStream(_ stream: PlatformStream?) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.stopStream(sessionID: $1, streamID: stream.id) }
    }

    func tuneLatestPeak() {
        guard let receiver, receiver.isReceiver else {
            errorText = "Перестройка доступна только receiver"
            return
        }
        guard let sweep = latestSweep,
              let index = sweep.levels.indices.max(by: { sweep.levels[$0] < sweep.levels[$1] }) else {
            errorText = "Нет фактического пика из архивного sweep"
            return
        }
        tune(frequencyHz: sweep.frequenciesHz[index])
    }

    func tune(frequencyHz: Int64) {
        guard let receiver, receiver.isReceiver else {
            errorText = "Перестройка доступна только receiver"
            return
        }
        performStreamAction(receiver) {
            try await $0.tune(sessionID: $1, streamID: receiver.id, frequencyHz: frequencyHz, mode: mode)
        }
    }

    func createCandidateFromLatestPeak() {
        do {
            guard let analyzer, analyzer.isDbmAnalyzer,
                  let sweep = latestSweep, sweep.streamId == analyzer.id, sweep.levelUnit == "dBm",
                  let index = sweep.levels.indices.max(by: { sweep.levels[$0] < sweep.levels[$1] }) else {
                throw PlatformError.invalidCommand("Нет фактического dBm sweep для создания кандидата")
            }
            let draft = try PlatformCandidateDraft(
                sourceStreamId: analyzer.id, interval: sweep.interval,
                centerFrequencyHz: sweep.frequenciesHz[index],
                peakLevel: sweep.levels[index], levelUnit: sweep.levelUnit,
                observationIds: [sweep.id]
            )
            perform { try await $0.createCandidate(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    func enqueueSelectedCandidate() {
        do {
            guard let selectedCandidate else {
                throw PlatformError.invalidCommand("Выберите кандидата")
            }
            guard let receiver, receiver.isReceiver else {
                throw PlatformError.invalidCommand("Для исследования нужен receiver сеанса")
            }
            let priority = try integerField(investigationPriority, "Приоритет", 0...1_000_000)
            let draft = try PlatformInvestigationDraft(
                targetStreamId: receiver.id,
                targetFrequencyHz: selectedCandidate.centerFrequencyHz,
                mode: mode, priority: priority
            )
            let candidateID = selectedCandidate.id
            perform {
                try await $0.enqueueInvestigation(sessionID: $1, candidateID: candidateID, draft: draft)
            }
        } catch {
            errorText = describeSession(error)
        }
    }

    func dismissSelectedCandidate() {
        guard let selectedCandidate else {
            errorText = "Выберите кандидата"
            return
        }
        let id = selectedCandidate.id
        perform { try await $0.dismissCandidate(sessionID: $1, candidateID: id) }
    }

    func startSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.startInvestigation(sessionID: $1, investigationID: id) }
    }

    func startNextInvestigation() {
        guard let nextInvestigation else {
            errorText = "Очередь исследований пуста"
            return
        }
        let id = nextInvestigation.id
        perform { try await $0.startNextInvestigation(sessionID: $1, investigationID: id, confirm: true) }
    }

    func completeSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.completeInvestigation(sessionID: $1, investigationID: id) }
    }

    func failSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        let reason = failureReason
        perform { try await $0.failInvestigation(sessionID: $1, investigationID: id, error: reason) }
    }

    func cancelSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.cancelInvestigation(sessionID: $1, investigationID: id) }
    }

    func saveAutomaticDetection() {
        configureAutomaticDetection(enabled: nil)
    }

    func enableAutomaticDetection() {
        configureAutomaticDetection(enabled: true)
    }

    func disableAutomaticDetection() {
        perform { try await $0.disableAutomaticDetection(sessionID: $1) }
    }

    func saveAutomaticHandoff() {
        configureAutomaticHandoff(enabled: nil)
    }

    func enableAutomaticHandoff() {
        configureAutomaticHandoff(enabled: true)
    }

    func disableAutomaticHandoff() {
        perform { try await $0.disableAutomaticHandoff(sessionID: $1) }
    }

    func saveAutomaticCompletion() {
        configureAutomaticCompletion(enabled: nil)
    }

    func enableAutomaticCompletion() {
        configureAutomaticCompletion(enabled: true)
    }

    func disableAutomaticCompletion() {
        perform { try await $0.disableAutomaticCompletion(sessionID: $1) }
    }

    func enableTaskScheduler() {
        configureTaskScheduler(enabled: true)
    }

    func disableTaskScheduler() {
        configureTaskScheduler(enabled: false)
    }

    func refreshArchiveAnalytics() {
        guard let client, !selectedSessionID.isEmpty, !archiveAnalyticsPending else { return }
        let sessionID = selectedSessionID
        let requestGeneration = generation
        archiveAnalyticsPending = true
        archiveAnalyticsError = nil
        Task {
            do {
                let summary = try await client.analyticsSummary(sessionID: sessionID)
                guard requestGeneration == generation, sessionID == selectedSessionID,
                      self.client === client else { return }
                archiveAnalytics = summary
            } catch {
                guard requestGeneration == generation, sessionID == selectedSessionID,
                      self.client === client else { return }
                archiveAnalytics = nil
                archiveAnalyticsError = describeSession(error)
            }
            guard requestGeneration == generation, sessionID == selectedSessionID,
                  self.client === client else { return }
            archiveAnalyticsPending = false
        }
    }

    func exportArchive() {
        guard let client, !selectedSessionID.isEmpty, !archiveExportPending else { return }
        let sessionID = selectedSessionID
        let safeName = sessionID.replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "\\", with: "_")
        let panel = NSSavePanel()
        panel.title = "Сохранить NDJSON snapshot"
        panel.nameFieldStringValue = "archive-\(safeName).ndjson"
        panel.allowedFileTypes = ["ndjson"]
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        let requestGeneration = generation
        archiveExportPending = true
        archiveExportStatus = "Сервер формирует NDJSON snapshot…"
        Task {
            do {
                let result = try await client.exportArchive(
                    sessionID: sessionID, destination: destination
                )
                guard requestGeneration == generation, sessionID == selectedSessionID,
                      self.client === client else { return }
                archiveExportStatus = "NDJSON сохранён: \(result.sweeps) sweeps, \(result.observations) observations · \(result.fileURL.lastPathComponent)"
            } catch {
                guard requestGeneration == generation, sessionID == selectedSessionID,
                      self.client === client else { return }
                archiveExportStatus = "NDJSON не сохранён: \(describeSession(error))"
            }
            guard requestGeneration == generation, sessionID == selectedSessionID,
                  self.client === client else { return }
            archiveExportPending = false
        }
    }

    func createTask() {
        guard let client, !taskCreatePending else { return }
        let requestGeneration = generation
        taskCreatePending = true
        newTaskStatus = "ControlTask создаётся через API…"
        Task {
            do {
                let duration = try numberField(newTaskDuration, "duration_s", 0.001...86_400.0)
                let draft = try PlatformControlTaskDraft(
                    name: newTaskName, taskFile: newTaskFile, startAt: newTaskStartAt,
                    durationS: duration, equipment: newTaskEquipment
                )
                let created = try await client.createTask(draft: draft)
                guard requestGeneration == generation, self.client === client else { return }
                newTaskStatus = "Создано: \(created.name) · \(created.status) · \(created.startAt ?? "время не задано")"
                showTaskCreate = false
                newTaskName = ""
                newTaskFile = ""
                newTaskStartAt = ""
                newTaskEquipment = ""
                await refreshTaskScheduler(client)
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                newTaskStatus = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            taskCreatePending = false
        }
    }

    func cancelTask(name: String) {
        guard let client, !taskCancelPending else { return }
        let requestGeneration = generation
        taskCancelPending = true
        taskCancelStatus = "Отмена \(name) через API…"
        Task {
            do {
                let cancelled = try await client.cancelTask(name: name)
                guard requestGeneration == generation, self.client === client else { return }
                taskCancelStatus = "Отменено: \(cancelled.name) · \(cancelled.status) · \(cancelled.outcome)"
                await refreshTaskScheduler(client)
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                taskCancelStatus = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            taskCancelPending = false
        }
    }

    private func configureTaskScheduler(enabled: Bool) {
        guard let client else { return }
        let requestGeneration = generation
        taskSchedulerCommandRevision += 1
        busy = true
        Task {
            do {
                let status = try await client.configureTaskScheduler(enabled: enabled)
                guard requestGeneration == generation, self.client === client else { return }
                taskSchedulerStatus = status
                taskSchedulerError = nil
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                taskSchedulerError = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            busy = false
        }
    }

    private func refreshTaskScheduler(_ client: PlatformClient) async {
        let requestGeneration = generation
        let schedulerRevision = taskSchedulerCommandRevision
        do {
            let status = try await client.taskScheduler()
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerStatus = status
            taskSchedulerError = nil
        } catch {
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerError = describeSession(error)
        }
        do {
            let tasks = try await client.taskSchedulerTasks()
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerTasks = tasks
            taskSchedulerTasksError = nil
        } catch {
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerTasksError = describeSession(error)
        }
    }

    private func configureAutomaticCompletion(enabled: Bool?) {
        do {
            let draft = try PlatformCompletionPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(completionInterval, "Период завершения", 0.1...3600.0),
                minimumListeningSeconds: try numberField(completionMinimumListening, "Минимум прослушивания", 0.0...86_400.0),
                minimumObservations: try integerField(completionMinimumObservations, "Минимум наблюдений", 1...1000),
                receiverStreamId: completionReceiverStreamID
            )
            perform { try await $0.configureAutomaticCompletion(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func configureAutomaticDetection(enabled: Bool?) {
        do {
            let draft = try PlatformDetectionPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(detectionInterval, "Период обнаружения", 0.1...3600.0),
                streamId: detectionStreamID,
                thresholdDbm: try numberField(detectionThreshold, "Порог dBm", -300.0...100.0),
                dedupBandwidthHz: try int64Field(detectionDedupBandwidth, "Полоса dedup", 1...10_000_000_000),
                cooldownSeconds: try numberField(detectionCooldown, "Cooldown", 0.0...86400.0),
                maxCandidatesPerSweep: try integerField(detectionMaximumCandidates, "Максимум кандидатов", 1...100)
            )
            perform { try await $0.configureAutomaticDetection(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func configureAutomaticHandoff(enabled: Bool?) {
        do {
            let draft = try PlatformHandoffPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(handoffInterval, "Период handoff", 0.1...3600.0),
                minimumPriority: try integerField(handoffMinimumPriority, "Минимальный приоритет", 0...1_000_000),
                receiverStreamId: handoffReceiverStreamID,
                maxConsecutiveFailures: try integerField(handoffMaximumFailures, "Сбоев до отключения", 1...100),
                retryDelaySeconds: try numberField(handoffRetryDelay, "Задержка повтора", 0.1...86400.0)
            )
            perform { try await $0.configureAutomaticHandoff(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func integerField(_ text: String, _ label: String, _ range: ClosedRange<Int>) throws -> Int {
        guard let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)), range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется целое в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    private func int64Field(_ text: String, _ label: String, _ range: ClosedRange<Int64>) throws -> Int64 {
        guard let value = Int64(text.trimmingCharacters(in: .whitespacesAndNewlines)), range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется целое в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    private func numberField(_ text: String, _ label: String, _ range: ClosedRange<Double>) throws -> Double {
        guard let value = parseUserNumber(text), value.isFinite, range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется конечное число в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    private var latestSweep: PlatformSweep?

    private func performSessionAction(
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        perform(operation)
    }

    private func performStreamAction(
        _ stream: PlatformStream,
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        perform(operation)
    }

    private func perform(
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        guard let client, !selectedSessionID.isEmpty else { return }
        let id = selectedSessionID
        let requestGeneration = generation
        busy = true
        Task {
            do {
                _ = try await operation(client, id)
                guard requestGeneration == generation, self.client === client, id == selectedSessionID else { return }
                await refresh()
            } catch {
                guard requestGeneration == generation, self.client === client, id == selectedSessionID else { return }
                errorText = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client, id == selectedSessionID else { return }
            busy = false
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { @MainActor in
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func clearMeasurements() {
        session = nil
        workers = []
        latestSweep = nil
        latestObservation = nil
        absorbedSweepIDs = []
        current = []
        minimum = []
        maximum = []
        rows = []
        waterfall = WaterfallHistory()
        waterfallRevision += 1
        candidates = []
        investigations = []
        nextInvestigation = nil
        selectedCandidateID = ""
        selectedInvestigationID = ""
        automaticHandoffStatus = nil
        automaticDetectionStatus = nil
        automaticCompletionStatus = nil
        taskSchedulerStatus = nil
        taskSchedulerError = nil
        taskSchedulerTasks = []
        taskSchedulerTasksError = nil
        newTaskName = ""
        newTaskFile = ""
        newTaskStartAt = ""
        newTaskEquipment = ""
        newTaskStatus = "Новая ControlTask ещё не создана"
        showTaskCreate = false
        taskCreatePending = false
        archiveAnalytics = nil
        archiveAnalyticsError = nil
        archiveAnalyticsPending = false
        archiveExportStatus = "NDJSON snapshot ещё не выгружен"
        archiveExportPending = false
        workflowErrors = []
        detectionFormInitialized = false
        handoffFormInitialized = false
        completionFormInitialized = false
    }

    private func applyWorkflow(
        _ snapshot: PlatformSnapshot, schedulerRevision: Int
    ) {
        candidates = snapshot.candidates
        investigations = snapshot.investigations
        nextInvestigation = snapshot.nextInvestigation
        automaticHandoffStatus = snapshot.automaticHandoff
        automaticDetectionStatus = snapshot.automaticDetection
        automaticCompletionStatus = snapshot.automaticCompletion
        if schedulerRevision == taskSchedulerCommandRevision {
            taskSchedulerStatus = snapshot.taskScheduler
            taskSchedulerError = snapshot.taskSchedulerError
            taskSchedulerTasks = snapshot.taskSchedulerTasks
            taskSchedulerTasksError = snapshot.taskSchedulerTasksError
        }
        workflowErrors = snapshot.workflowErrors
        if !candidates.contains(where: { $0.id == selectedCandidateID }) {
            selectedCandidateID = candidates.first(where: { $0.state == "new" })?.id ?? candidates.first?.id ?? ""
        }
        if !investigations.contains(where: { $0.id == selectedInvestigationID }) {
            selectedInvestigationID = investigations.first(where: {
                ["queued", "tuning", "listening", "decoding"].contains($0.state)
            })?.id ?? investigations.first?.id ?? ""
        }
        loadPolicyFormsIfNeeded()
    }

    private func loadPolicyFormsIfNeeded() {
        if !detectionFormInitialized, let policy = automaticDetectionStatus?.policy {
            detectionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            detectionStreamID = policy.streamId ?? ""
            detectionThreshold = policy.thresholdDbm.map { String(describing: $0) } ?? "-70"
            detectionDedupBandwidth = policy.dedupBandwidthHz.map { String($0) } ?? "100000"
            detectionCooldown = policy.cooldownSeconds.map { String(describing: $0) } ?? "60"
            detectionMaximumCandidates = policy.maxCandidatesPerSweep.map { String($0) } ?? "10"
            detectionFormInitialized = true
        }
        if !handoffFormInitialized, let policy = automaticHandoffStatus?.policy {
            handoffInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            handoffMinimumPriority = policy.minimumPriority.map { String($0) } ?? "0"
            handoffReceiverStreamID = policy.receiverStreamId ?? ""
            handoffMaximumFailures = policy.maxConsecutiveFailures.map { String($0) } ?? "3"
            handoffRetryDelay = policy.retryDelaySeconds.map { String(describing: $0) } ?? "30"
            handoffFormInitialized = true
        }
        if !completionFormInitialized, let policy = automaticCompletionStatus?.policy {
            completionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            completionMinimumListening = policy.minimumListeningSeconds.map { String(describing: $0) } ?? "0"
            completionMinimumObservations = policy.minimumObservations.map { String($0) } ?? "1"
            completionReceiverStreamID = policy.receiverStreamId ?? ""
            completionFormInitialized = true
        }
    }

    private func applyMeasurements(_ snapshot: PlatformSnapshot) {
        var envelope = TraceEnvelope()
        var found: [Exceedance] = []
        let level = thresholdDbm ?? -80
        for sweep in snapshot.sweeps where sweep.levelUnit == "dBm" {
            let bins = zip(sweep.frequenciesHz, sweep.levels).map {
                LevelBin(frequency: $0, level: $1)
            }
            envelope.absorb(bins)
            found = ExceedanceList.update(existing: found, bins: bins, threshold: level)
            if !absorbedSweepIDs.contains(sweep.id) {
                waterfall.absorb(bins)
                absorbedSweepIDs.insert(sweep.id)
                waterfallRevision += 1
            }
        }
        latestSweep = snapshot.sweeps.last
        latestObservation = snapshot.observations.last
        if let latestSweep {
            current = plot(latestSweep)
        } else {
            current = []
        }
        minimum = envelope.minimumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        maximum = envelope.maximumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        rows = found
    }

    private func plot(_ sweep: PlatformSweep) -> [PlotPoint] {
        zip(sweep.frequenciesHz, sweep.levels).map {
            PlotPoint(x: Double($0) / 1_000_000, y: $1)
        }
    }
}

import SpektrKomandy
import SwiftUI

struct RemoteStreamControls: View {
    var title: String
    var stream: PlatformStream?
    var worker: PlatformWorker?
    var busy: Bool
    var onStart: () -> Void
    var onStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(stream.map { "\($0.device) · \($0.id)" } ?? "Подходящий поток не найден")
                .font(.caption)
                .textSelection(.enabled)
            HStack {
                Label(worker?.state ?? stream?.state ?? "—", systemImage: worker?.alive == true ? "circle.fill" : "circle")
                    .foregroundStyle(worker?.alive == true ? .green : .secondary)
                Spacer()
                Button("Запустить поток", action: onStart)
                    .disabled(stream == nil || busy)
                Button("Остановить поток", action: onStop)
                    .disabled(stream == nil || busy)
            }
            if let error = worker?.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red)
            }
        }
    }
}

struct RemoteArPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("AOR AR-DV1 · через ядро").font(.title2.weight(.semibold))
                Text("Наблюдения и команды поступают через /api/v1. Уровень остаётся в исходных отсчётах S-метра; звук и аппаратное декодирование выполняет приёмник.")
                    .foregroundStyle(.secondary)
                RemoteStreamControls(
                    title: "AR-DV1", stream: model.receiver,
                    worker: model.worker(for: model.receiver?.id), busy: model.busy,
                    onStart: { model.startStream(model.receiver) },
                    onStop: { model.stopStream(model.receiver) }
                )
                HStack {
                    Picker("Режим", selection: $model.mode) {
                        ForEach(model.receiver?.tuneModes ?? ["FM", "AM"], id: \.self) { Text($0) }
                    }
                    .frame(width: 180)
                    Button("Перестроить на фактический пик") { model.tuneLatestPeak() }
                        .disabled(model.receiver == nil || model.current.isEmpty || model.busy)
                }
                if let observation = model.latestObservation {
                    GroupBox("Последнее наблюдение ядра") {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(frequency(observation.frequencyHz))
                            Text("Режим: \(observation.mode) · декодер: \(observation.decodedMode ?? "—")")
                            Text("Уровень: \(observation.level.map { String(format: "%.1f", $0) } ?? "—") \(observation.levelUnit)")
                            Text("Шумодав: \(observation.squelchOpen.map { $0 ? "открыт" : "закрыт" } ?? "—")")
                            Text("Интервал: \(observation.interval.startedAt) — \(observation.interval.endedAt)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else {
                    Text("Наблюдений receiver пока нет").foregroundStyle(.secondary)
                }
                Text("Перестройка AR-DV1 не останавливает tinySA. Прежняя частота автоматически не восстанавливается.")
                    .font(.caption).foregroundStyle(.secondary)
                ErrorLine(text: model.errorText)
            }
            .padding(16)
        }
    }

    private func frequency(_ hertz: Int64) -> String {
        String(format: "%.6f МГц", Double(hertz) / 1_000_000) + " · \(hertz) Гц"
    }
}

struct RemoteUltraPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("tinySA Ultra · через ядро").font(.title2.weight(.semibold))
                Text("Спектр и водопад строятся только из архивных dBm sweeps. Клиент не отправляет scan и не включает генератор.")
                    .foregroundStyle(.secondary)
                RemoteStreamControls(
                    title: "tinySA", stream: model.analyzer,
                    worker: model.worker(for: model.analyzer?.id), busy: model.busy,
                    onStart: { model.startStream(model.analyzer) },
                    onStop: { model.stopStream(model.analyzer) }
                )
                HStack {
                    TextField("Порог, dBm", text: $model.threshold)
                        .textFieldStyle(.roundedBorder).frame(width: 140)
                    Text(model.live).font(.caption).foregroundStyle(.secondary)
                }
                TraceChart(
                    current: model.current, minimum: model.minimum, maximum: model.maximum,
                    threshold: model.thresholdDbm, yTitle: "дБм (исходные)"
                )
                WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "дБм")
                GroupBox("Превышения порога") {
                    if model.rows.isEmpty {
                        Text("Превышений нет").foregroundStyle(.secondary)
                    } else {
                        ForEach(model.rows.prefix(30)) { row in
                            Button {
                                model.tune(frequencyHz: row.center)
                            } label: {
                                HStack {
                                    Text(String(format: "%.6f МГц", Double(row.center) / 1_000_000))
                                    Spacer()
                                    Text("полоса \(row.width) Гц · \(row.hits)")
                                }
                            }
                            .buttonStyle(.plain)
                            .disabled(model.receiver == nil || model.busy)
                        }
                    }
                }
                ErrorLine(text: model.errorText)
            }
            .padding(16)
        }
    }
}

// Observe this particular window; other WindowGroup windows keep their own sessions.
struct WindowCloseObserver: NSViewRepresentable {
    var onClose: () -> Void

    func makeNSView(context: Context) -> WindowCloseView {
        let view = WindowCloseView()
        view.onClose = onClose
        return view
    }

    func updateNSView(_ view: WindowCloseView, context: Context) {
        view.onClose = onClose
    }
}

final class WindowCloseView: NSView {
    var onClose: (() -> Void)?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: nil)
        if let window {
            NotificationCenter.default.addObserver(self, selector: #selector(windowWillClose),
                name: NSWindow.willCloseNotification, object: window)
        }
    }

    @objc private func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    deinit { NotificationCenter.default.removeObserver(self) }
}
