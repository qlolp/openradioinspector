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
    @Published var archiveAnalytics: PlatformAnalyticsSummary?
    @Published var archiveAnalyticsError: String?
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
    private var policyFormsInitialized = false

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
            if !selectedSessionID.isEmpty { await refresh() }
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
        guard let client, !selectedSessionID.isEmpty else { return }
        let id = selectedSessionID
        let requestGeneration = generation
        do {
            let snapshot = try await client.snapshot(sessionID: id)
            guard requestGeneration == generation, id == selectedSessionID else { return }
            session = snapshot.session
            workers = snapshot.workers
            applyMeasurements(snapshot)
            applyWorkflow(snapshot)
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
        configureAutomaticDetection(enabled: automaticDetectionStatus?.policy.enabled ?? false)
    }

    func enableAutomaticDetection() {
        configureAutomaticDetection(enabled: true)
    }

    func disableAutomaticDetection() {
        perform { try await $0.disableAutomaticDetection(sessionID: $1) }
    }

    func saveAutomaticHandoff() {
        configureAutomaticHandoff(enabled: automaticHandoffStatus?.policy.enabled ?? false)
    }

    func enableAutomaticHandoff() {
        configureAutomaticHandoff(enabled: true)
    }

    func disableAutomaticHandoff() {
        perform { try await $0.disableAutomaticHandoff(sessionID: $1) }
    }

    func saveAutomaticCompletion() {
        configureAutomaticCompletion(enabled: automaticCompletionStatus?.policy.enabled ?? false)
    }

    func enableAutomaticCompletion() {
        configureAutomaticCompletion(enabled: true)
    }

    func disableAutomaticCompletion() {
        perform { try await $0.disableAutomaticCompletion(sessionID: $1) }
    }

    private func configureAutomaticDetection(enabled: Bool) {
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

    private func configureAutomaticHandoff(enabled: Bool) {
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

    private func configureAutomaticCompletion(enabled: Bool) {
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
                let result = try await operation(client, id)
                guard requestGeneration == generation, id == selectedSessionID else { return }
                await refresh()
                return result
            } catch {
                guard requestGeneration == generation, id == selectedSessionID else { return }
                errorText = describeSession(error)
            }
            guard requestGeneration == generation, id == selectedSessionID else { return }
            busy = false
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
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
        archiveAnalytics = nil
        archiveAnalyticsError = nil
        workflowErrors = []
        policyFormsInitialized = false
    }

    private func applyWorkflow(_ snapshot: PlatformSnapshot) {
        candidates = snapshot.candidates
        investigations = snapshot.investigations
        nextInvestigation = snapshot.nextInvestigation
        automaticHandoffStatus = snapshot.automaticHandoff
        automaticDetectionStatus = snapshot.automaticDetection
        automaticCompletionStatus = snapshot.automaticCompletion
        archiveAnalytics = snapshot.analytics
        archiveAnalyticsError = snapshot.analyticsError
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
        guard !policyFormsInitialized,
              automaticHandoffStatus != nil, automaticDetectionStatus != nil,
              automaticCompletionStatus != nil else { return }
        if let policy = automaticDetectionStatus?.policy {
            detectionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            detectionStreamID = policy.streamId ?? analyzer?.id ?? ""
            detectionThreshold = policy.thresholdDbm.map { String(describing: $0) } ?? "-70"
            detectionDedupBandwidth = policy.dedupBandwidthHz.map { String($0) } ?? "100000"
            detectionCooldown = policy.cooldownSeconds.map { String(describing: $0) } ?? "60"
            detectionMaximumCandidates = policy.maxCandidatesPerSweep.map { String($0) } ?? "10"
        }
        if let policy = automaticHandoffStatus?.policy {
            handoffInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            handoffMinimumPriority = policy.minimumPriority.map { String($0) } ?? "0"
            handoffReceiverStreamID = policy.receiverStreamId ?? receiver?.id ?? ""
            handoffMaximumFailures = policy.maxConsecutiveFailures.map { String($0) } ?? "3"
            handoffRetryDelay = policy.retryDelaySeconds.map { String(describing: $0) } ?? "30"
        }
        if let policy = automaticCompletionStatus?.policy {
            completionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            completionMinimumListening = policy.minimumListeningSeconds.map { String(describing: $0) } ?? "0"
            completionMinimumObservations = policy.minimumObservations.map { String($0) } ?? "1"
            completionReceiverStreamID = policy.receiverStreamId ?? ""
        }
        policyFormsInitialized = true
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
        current = envelope.current
        minimum = envelope.minimum
        maximum = envelope.maximum
        rows = found
    }
}
