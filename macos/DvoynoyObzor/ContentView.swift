import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @StateObject private var model = RemoteDualModel()
    @State private var workspaceMode = "research"

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
                    Button(model.session?.streams.count == 1 ? "Запустить поток" : "Запустить оба потока") { model.startWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Button(model.session?.streams.count == 1 ? "Остановить поток" : "Остановить оба потока") { model.stopWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Button("tinySA · lab / экран") { model.showTinySAControl = true }
                        .disabled(!model.connected || model.analyzer == nil)
                    Spacer()
                    Text(model.status).font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            RemoteArchiveCapacityPane(model: model)
                .padding(.horizontal, 16)
                .padding(.bottom, 8)
            Picker("Рабочий режим", selection: $workspaceMode) {
                Text("Радиомониторинг").tag("monitoring")
                Text("Исследование").tag("research")
            }.pickerStyle(.segmented).padding(.horizontal)
            MonitoringWorkspacePane(server: model.connected ? model.serverURL : "", sessionID: model.selectedSessionID)
            Divider()
            VSplitView {
                HSplitView {
                    RemoteArPane(model: model).frame(minWidth: 460)
                    RemoteUltraPane(model: model).frame(minWidth: 460)
                }
                if workspaceMode == "research" {
                    RemoteWorkflowPane(model: model).frame(minHeight: 280)
                } else {
                    Text("Радиомониторинг · проект, готовность и исходные панорамы. Исследование не останавливает сбор.")
                        .padding()
                }
            }
        }
        .frame(minWidth: 980, minHeight: 860)
        .sheet(isPresented: $model.showSerialForm) {
            RemoteSerialSessionPane(model: model)
        }
        .sheet(isPresented: $model.showTinySAControl) {
            RemoteTinySAControlPane(server: model.serverURL, sessionID: model.selectedSessionID,
                                    streamID: model.analyzer?.id ?? "")
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

struct PinnedSweepReference {
    let id: String
    let sessionId: String
    let streamId: String
    let interval: PlatformInterval
    let name: String?

    init(_ sweep: PlatformSweep) {
        id = sweep.id; sessionId = sweep.sessionId; streamId = sweep.streamId
        interval = sweep.interval; name = nil
    }
    init(_ record: PlatformSweepReference) {
        id = record.sweepId; sessionId = record.sessionId; streamId = record.streamId
        interval = record.interval; name = record.name
    }
}

@MainActor
final class RemoteDualModel: ObservableObject {
    @Published var showTinySAControl = false
    @Published var serverURL = ProcessInfo.processInfo.environment["ORI_LOCAL_CORE_URL"] ?? "http://127.0.0.1:8770" {
        didSet { if !PlatformThreeSweepThresholds.sameID(serverURL, oldValue) { resetComparisonScope(); frequencyRounding.reset(); configureAuxiliaryStreams(client: nil, session: nil) } }
    }
    @Published var sessions: [PlatformSession] = []
    @Published var selectedSessionID = "" {
        didSet { if !PlatformThreeSweepThresholds.sameID(selectedSessionID, oldValue) { resetComparisonScope(); frequencyRounding.reset(); configureAuxiliaryStreams(client: nil, session: nil) } }
    }
    @Published var session: PlatformSession? {
        didSet {
            if !PlatformThreeSweepThresholds.sameID(session?.streams.first(where: { $0.isDbmAnalyzer })?.id,
                oldValue?.streams.first(where: { $0.isDbmAnalyzer })?.id) {
                resetComparisonScope()
            }
        }
    }
    @Published var auxiliaryStreamID = "" {
        didSet { if !PlatformThreeSweepThresholds.sameID(auxiliaryStreamID, oldValue) { applyAuxiliaryStream() } }
    }
    private var auxiliaryClient: PlatformClient?
    private var auxiliarySession: PlatformSession?
    @Published var workers: [PlatformWorker] = []
    @Published var connected = false
    @Published var busy = false
    @Published var status = "Сервер не подключён"
    @Published var errorText: String?
    @Published var mode = "FM"
    @Published var threshold = "-80"
    @Published var spectrumPresentation = PlatformSpectrumPresentation()
    @Published var spectrumLower = ""
    @Published var spectrumUpper = ""
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
    @Published private(set) var comparisonSweeps: [PlatformSweep] = []
    @Published var comparisonSelectionID = ""
    @Published private(set) var comparisonReference: PinnedSweepReference?
    @Published private(set) var sweepComparison: PlatformSweepComparison?
    @Published private(set) var sweepComparisonError: String?
    @Published private(set) var sweepComparisonPending = false
    @Published var referenceName = ""
    @Published var savedReferenceSelectionID = ""
    @Published private(set) var referenceCatalog: PlatformSweepReferenceCatalog?
    @Published private(set) var savedSweepReferences: [PlatformSweepReference] = []
    @Published private(set) var referenceCatalogError: String?
    @Published private(set) var referenceSaveStatus: String?
    @Published private(set) var referenceWriteNotice: String?
    @Published private(set) var referenceCatalogPending = false
    @Published private(set) var referenceSavePending = false
    @Published var archiveCapacity: PlatformArchiveCapacity?
    @Published var archiveCapacityError: String?
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

    let sweepAverage = SweepAverageModel()
    let threeSweepThresholds = ThreeSweepThresholdsModel()
    let sweepContext = SweepContextModel()
    let spectralMask = SpectralMaskModel()
    let frequencyRounding = FrequencyRoundingModel()

    let frequencyRaster = FrequencyRasterModel()
    let measurementResults = MeasurementResultsModel()
    let frequencyCounter = FrequencyCounterModel()
    let dvbEvaluation = DvbEvaluationModel()
    let phonogram = PhonogramModel()
    let signalList = SignalListModel()
    let measurementMap = MeasurementMapModel()
    let audioStream = AudioStreamModel()
    let sweepThreshold: SweepThresholdModel

    private let makeClient: (String) throws -> PlatformClient
    private let comparisonTimeoutNanoseconds: UInt64
    private var comparisonTask: Task<Void, Never>?
    private var comparisonTimeoutTask: Task<Void, Never>?
    private var comparisonRevision = 0
    private struct ReferenceScope: Hashable {
        let server: String
        let session: String
        let stream: String
    }
    private var connectedServerURL: String?
    private var loadedReferenceScope: ReferenceScope?
    private var referenceRequestScope: ReferenceScope?
    private var uncertainReferenceScopes: Set<ReferenceScope> = []
    private var referenceNoticeScope: ReferenceScope?
    private var referenceRequestRevision = 0
    private var referenceTask: Task<Void, Never>?
    private var referenceTimeoutTask: Task<Void, Never>?
    private var connectionGeneration = 0
    private var capacityRequestRevision = 0
    private var client: PlatformClient?
    private var pollTask: Task<Void, Never>?
    private var generation = 0
    private var sessionRefreshRevision = 0
    private var detectionFormInitialized = false
    private var handoffFormInitialized = false
    private var completionFormInitialized = false
    private var taskSchedulerCommandRevision = 0

    func refreshSerialPorts() {
        let ports = SerialPort.availablePaths()
        if let modem = ports.first(where: { $0.contains("usbmodem") }) {
            tinySAPort = modem
        } else if let first = ports.first(where: { $0.contains("cu.") }) {
            tinySAPort = first
        }
        if let serial = ports.first(where: { $0 != tinySAPort && ($0.contains("usbserial") || $0.contains("cu.")) }) {
            receiverPort = serial
        } else if receiverPort == "/dev/ttyUSB0" {
            receiverPort = ""
        }
    }

    init(
        makeClient: @escaping (String) throws -> PlatformClient = { try PlatformClient(baseURL: $0) },
        comparisonTimeoutNanoseconds: UInt64 = 6_000_000_000,
        thresholdTimeoutNanoseconds: UInt64 = 6_000_000_000
    ) {
        self.makeClient = makeClient
        self.sweepThreshold = SweepThresholdModel(timeoutNanoseconds: thresholdTimeoutNanoseconds)
        self.comparisonTimeoutNanoseconds = comparisonTimeoutNanoseconds
        refreshSerialPorts()
    }

    var analyzer: PlatformStream? { session?.streams.first(where: { $0.isDbmAnalyzer }) }
    var receiver: PlatformStream? { session?.streams.first(where: { $0.isReceiver }) }
    var thresholdDbm: Double? { parseUserNumber(threshold) }
    var selectedCandidate: PlatformCandidate? {
        candidates.first(where: { $0.id == selectedCandidateID })
    }
    var selectedInvestigation: PlatformInvestigation? {
        investigations.first(where: { $0.id == selectedInvestigationID })
    }

    func configureAuxiliaryStreams(client: PlatformClient?, session: PlatformSession?) {
        auxiliaryClient = client; auxiliarySession = session
        if let session, !session.streams.contains(where: { PlatformThreeSweepThresholds.sameID($0.id, auxiliaryStreamID) }) {
            auxiliaryStreamID = session.streams.first(where: { $0.isReceiver })?.id ?? session.streams.first?.id ?? ""
        }
        applyAuxiliaryStream()
    }
    private func applyAuxiliaryStream() {
        guard let client = auxiliaryClient, let session = auxiliarySession,
              session.streams.contains(where: { PlatformThreeSweepThresholds.sameID($0.id, auxiliaryStreamID) }) else {
            phonogram.reset(); signalList.reset(); audioStream.reset(); return
        }
        phonogram.configure(client: client,
            scope: .init(server: serverURL, session: session.id, stream: auxiliaryStreamID))
        signalList.configure(client: client,
            scope: .init(server: serverURL, session: session.id, stream: auxiliaryStreamID))
        audioStream.configure(client: client,
            scope: .init(server: serverURL, session: session.id, stream: auxiliaryStreamID))
    }

    func worker(for streamID: String?) -> PlatformWorker? {
        guard let streamID else { return nil }
        return workers.first(where: { $0.streamId == streamID })
    }

    func connect(startPolling: Bool = true) {
        frequencyRounding.reset()
        do {
            client = try makeClient(serverURL)
            resetComparisonScope()
            connectedServerURL = serverURL
            connectionGeneration += 1
            archiveCapacity = nil
            archiveCapacityError = nil
            connected = true
            errorText = nil
            status = "Сервер подключён; приборные порты остаются у ядра"
            generation += 1
            if startPolling { self.startPolling() }
            Task { await reloadSessions() }
        } catch {
            connected = false
            clearSpectrumPresentation()
            errorText = describeSession(error)
        }
    }

    func disconnect() {
        showTinySAControl = false
        connectionGeneration += 1
        capacityRequestRevision += 1
        archiveCapacity = nil
        archiveCapacityError = nil
        generation += 1
        pollTask?.cancel()
        pollTask = nil
        client = nil
        configureAuxiliaryStreams(client: nil, session: nil)
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
            clearSpectrumPresentation()
            errorText = describeSession(error)
        }
    }

    func selectSession(_ id: String) {
        guard !PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
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
                showSerialForm = false
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
        // Capacity belongs to this server, even when the selected session fails or changes.
        async let capacityRefresh: Void = refreshArchiveCapacity(client)
        await refreshSession(client)
        await capacityRefresh
    }

    private func refreshArchiveCapacity(_ client: PlatformClient) async {
        guard self.client === client else { return }
        let connection = connectionGeneration
        capacityRequestRevision += 1
        let revision = capacityRequestRevision
        do {
            let capacity = try await client.archiveCapacity()
            guard connection == connectionGeneration, self.client === client,
                  revision == capacityRequestRevision else { return }
            archiveCapacity = capacity
            archiveCapacityError = nil
        } catch {
            guard connection == connectionGeneration, self.client === client,
                  revision == capacityRequestRevision else { return }
            // Never display stale capacity as current after an endpoint failure.
            archiveCapacity = nil
            if case PlatformError.http(status: 404, code: _, message: _) = error {
                archiveCapacityError = "Сервер не поддерживает сведения о ёмкости архива (404)"
            } else {
                archiveCapacityError = describeSession(error)
            }
        }
    }

    private func refreshSession(_ client: PlatformClient) async {
        guard self.client === client else { return }
        if selectedSessionID.isEmpty {
            await refreshTaskScheduler(client)
            return
        }
        sessionRefreshRevision += 1
        let refreshRevision = sessionRefreshRevision
        let id = selectedSessionID
        let requestGeneration = generation
        let schedulerRevision = taskSchedulerCommandRevision
        do {
            let snapshot = try await client.snapshot(sessionID: id, includeArchiveCapacity: false)
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client, PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
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
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client, PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            sweepAverage.reset()
            threeSweepThresholds.reset()
            sweepContext.reset()
            frequencyRounding.unavailable()
            clearSpectrumPresentation()
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
        guard !spectrumPresentation.paused else { return }
        guard let receiver, receiver.isReceiver else {
            errorText = "Перестройка доступна только receiver"
            return
        }
        guard let sweep = spectrumPresentation.sweeps.last,
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
        let requestedMode = mode
        performStreamAction(receiver) {
            try await $0.tune(sessionID: $1, streamID: receiver.id, frequencyHz: frequencyHz, mode: requestedMode)
        }
    }

    func createCandidateFromLatestPeak() {
        guard !spectrumPresentation.paused else { return }
        do {
            guard let analyzer, analyzer.isDbmAnalyzer,
                  let sweep = spectrumPresentation.sweeps.last, sweep.streamId == analyzer.id, sweep.levelUnit == "dBm",
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

    private var currentReferenceScope: ReferenceScope? {
        guard connected, connectedServerURL == serverURL, !selectedSessionID.isEmpty,
              session?.id == selectedSessionID, let stream = analyzer?.id else { return nil }
        return ReferenceScope(server: serverURL, session: selectedSessionID, stream: stream)
    }

    var referenceStorageDescription: String {
        guard let catalog = referenceCatalog else { return "Хранение каталога неизвестно" }
        if catalog.persistent { return "SQLite: постоянный каталог" }
        let mode = catalog.storageMode == "sqlite" ? "SQLite (:memory:)" : "Memory"
        return "\(mode): временный каталог, исчезнет при перезапуске ядра"
    }

    var referenceRequestPending: Bool { referenceCatalogPending || referenceSavePending }
    var canRefreshSweepReferences: Bool { currentReferenceScope != nil && !referenceRequestPending }
    var canSaveSweepReference: Bool {
        guard let scope = currentReferenceScope, let reference = comparisonReference else { return false }
        return !referenceRequestPending && !uncertainReferenceScopes.contains(scope)
            && reference.sessionId == scope.session && reference.streamId == scope.stream
            && (try? normalizedSweepReferenceName(referenceName)) != nil
    }
    var canUseSavedReference: Bool {
        guard let scope = currentReferenceScope else { return false }
        return savedSweepReferences.contains {
            $0.id == savedReferenceSelectionID && $0.sessionId == scope.session && $0.streamId == scope.stream
        }
    }

    func useSavedSweepReference() {
        guard canUseSavedReference, let record = savedSweepReferences.first(where: { $0.id == savedReferenceSelectionID }) else { return }
        cancelSweepComparison()
        comparisonReference = PinnedSweepReference(record)
    }

    func refreshSweepReferences() {
        guard canRefreshSweepReferences, let scope = currentReferenceScope, let client else { return }
        loadedReferenceScope = scope
        beginReferenceRequest(scope: scope, saving: false)
        let revision = referenceRequestRevision
        referenceTask = Task { [weak self] in
            do {
                let catalog = try await client.sweepReferences(sessionID: scope.session, streamID: scope.stream)
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                self.referenceCatalog = catalog
                self.savedSweepReferences = catalog.items
                if !catalog.items.contains(where: { $0.id == self.savedReferenceSelectionID }) {
                    self.savedReferenceSelectionID = catalog.items.first?.id ?? ""
                }
                self.referenceCatalogError = nil
                self.uncertainReferenceScopes.remove(scope)
                if self.referenceNoticeScope == scope {
                    self.referenceWriteNotice = nil
                    self.referenceNoticeScope = nil
                    self.referenceSaveStatus = "Каталог обновлён. Проверьте имя; повтор с тем же именем и свипом безопасен."
                }
                self.finishReferenceRequest()
            } catch {
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                self.referenceCatalog = nil
                self.savedSweepReferences = []
                self.savedReferenceSelectionID = ""
                self.referenceCatalogError = self.describeReferenceError(error)
                self.finishReferenceRequest()
            }
        }
    }

    func savePinnedSweepReference() {
        guard canSaveSweepReference, let scope = currentReferenceScope, let client,
              let reference = comparisonReference, let name = try? normalizedSweepReferenceName(referenceName) else { return }
        beginReferenceRequest(scope: scope, saving: true)
        referenceSaveStatus = nil
        let revision = referenceRequestRevision
        referenceTask = Task { [weak self] in
            do {
                let record = try await client.saveSweepReference(sessionID: scope.session, sweepID: reference.id, name: name)
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                guard record.streamId == scope.stream else {
                    throw PlatformError.invalidResponse("Сервер вернул опорный свип другого потока")
                }
                self.savedSweepReferences.removeAll { $0.id == record.id }
                self.savedSweepReferences.append(record)
                self.savedSweepReferences.sort { $0.createdAt < $1.createdAt }
                self.savedReferenceSelectionID = record.id
                self.referenceSaveStatus = "Сохранено: \(record.name). Каталог ограничен 100 записями на сеанс."
                self.finishReferenceRequest()
            } catch {
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                if case PlatformError.http(let status, _, _) = error, [400, 404, 409].contains(status) {
                    self.referenceSaveStatus = self.describeReferenceError(error)
                } else {
                    self.markReferenceWriteUncertain(scope)
                }
                self.finishReferenceRequest()
            }
        }
    }

    private func describeReferenceError(_ error: Error) -> String {
        switch error {
        case PlatformError.http(status: 404, code: _, message: _):
            return "Каталог или свип не найден; старый сервер может не поддерживать именованные опорные свипы (404). Локальное закрепление доступно."
        case PlatformError.http(status: 409, code: "reference_name_conflict", message: _):
            return "Имя уже занято другим свипом. Выберите новое имя; перезапись не выполняется."
        case PlatformError.http(status: 409, code: "reference_limit_reached", message: _):
            return "Достигнут предел: 100 опорных свипов на сеанс."
        case PlatformError.http(status: 409, code: _, message: let message):
            return "Свип несовместим (409): \(message)"
        default: return describeSession(error)
        }
    }

    private func referenceRequestMatches(_ scope: ReferenceScope, _ revision: Int, _ client: PlatformClient) -> Bool {
        referenceRequestRevision == revision && currentReferenceScope == scope && self.client === client
    }

    private func beginReferenceRequest(scope: ReferenceScope, saving: Bool) {
        referenceRequestRevision += 1
        referenceRequestScope = scope
        referenceSavePending = saving
        referenceCatalogPending = !saving
        referenceCatalogError = nil
        let revision = referenceRequestRevision
        referenceTimeoutTask = Task { [weak self, comparisonTimeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: comparisonTimeoutNanoseconds) } catch { return }
            guard let self, self.referenceRequestRevision == revision, self.referenceRequestPending else { return }
            self.cancelReferenceRequest()
            if !saving {
                self.referenceCatalog = nil
                self.savedSweepReferences = []
                self.savedReferenceSelectionID = ""
                self.referenceCatalogError = "Время ожидания каталога истекло. Нажмите Обновить каталог."
            }
        }
    }

    private func markReferenceWriteUncertain(_ scope: ReferenceScope) {
        uncertainReferenceScopes.insert(scope)
        referenceNoticeScope = scope
        referenceWriteNotice = "Результат сохранения неизвестен: \(scope.server), сеанс \(scope.session). Запись могла сохраниться на сервере. Вернитесь к этому сеансу и нажмите Обновить каталог перед повтором."
    }

    /// Stops waiting locally, never promises that the server write was canceled.
    func cancelReferenceRequest() {
        if referenceSavePending, let scope = referenceRequestScope { markReferenceWriteUncertain(scope) }
        referenceRequestRevision += 1
        referenceTask?.cancel()
        finishReferenceRequest()
    }

    private func finishReferenceRequest() {
        referenceTimeoutTask?.cancel()
        referenceTimeoutTask = nil
        referenceTask = nil
        referenceRequestScope = nil
        referenceSavePending = false
        referenceCatalogPending = false
    }

    var canPinComparisonReference: Bool {
        currentReferenceScope != nil && comparisonSweeps.contains(where: { $0.id == comparisonSelectionID })
    }

    var canCompareSweep: Bool {
        currentReferenceScope != nil && comparisonReference != nil && comparisonSweeps.last != nil
    }

    func pinComparisonReference() {
        guard currentReferenceScope != nil, let selected = comparisonSweeps.first(where: { $0.id == comparisonSelectionID }),
              selected.sessionId == selectedSessionID, selected.streamId == analyzer?.id,
              selected.levelUnit == "dBm" else { return }
        cancelSweepComparison()
        comparisonReference = PinnedSweepReference(selected)
    }

    func clearSweepComparison() {
        cancelSweepComparison()
        comparisonReference = nil
    }

    func cancelSweepComparison() {
        comparisonRevision += 1
        comparisonTask?.cancel()
        comparisonTimeoutTask?.cancel()
        comparisonTask = nil
        comparisonTimeoutTask = nil
        sweepComparisonPending = false
        sweepComparison = nil
        sweepComparisonError = nil
    }

    private func resetComparisonScope() {
        clearSpectrumPresentation()
        sweepAverage.reset()
        threeSweepThresholds.reset()
        sweepContext.reset()
        spectralMask.reset()
        frequencyRaster.reset()
        measurementResults.reset()
        frequencyCounter.reset()
        dvbEvaluation.reset()
        phonogram.reset()
        signalList.reset()
        measurementMap.reset()
        audioStream.reset()
        sweepThreshold.reset()
        cancelReferenceRequest()
        loadedReferenceScope = nil
        referenceCatalog = nil
        savedSweepReferences = []
        savedReferenceSelectionID = ""
        referenceName = ""
        referenceCatalogError = nil
        referenceSaveStatus = nil
        clearSweepComparison()
        comparisonSweeps = []
        comparisonSelectionID = ""
    }

    /// Explicit only: polling never moves the pinned reference or refreshes its comparison.
    func compareLatestSweep() {
        guard currentReferenceScope != nil, let client, let reference = comparisonReference,
              let target = comparisonSweeps.last, reference.sessionId == selectedSessionID,
              target.sessionId == selectedSessionID, reference.streamId == analyzer?.id,
              target.streamId == reference.streamId else { return }
        cancelSweepComparison()
        let revision = comparisonRevision
        let requestGeneration = generation
        let connection = connectionGeneration
        let sessionID = selectedSessionID
        let streamID = reference.streamId
        sweepComparisonPending = true
        comparisonTask = Task { [weak self] in
            do {
                let result = try await client.sweepComparison(
                    sessionID: sessionID, referenceID: reference.id, targetID: target.id
                )
                guard let self, !Task.isCancelled, revision == self.comparisonRevision,
                      requestGeneration == self.generation, connection == self.connectionGeneration,
                      self.client === client, self.selectedSessionID == sessionID,
                      self.analyzer?.id == streamID else { return }
                guard result.streamId == streamID else {
                    throw PlatformError.invalidResponse("Сервер вернул сравнение другого потока")
                }
                self.sweepComparison = result
                self.finishSweepComparison()
            } catch {
                guard let self, !Task.isCancelled, revision == self.comparisonRevision,
                      requestGeneration == self.generation, connection == self.connectionGeneration,
                      self.client === client, self.selectedSessionID == sessionID,
                      self.analyzer?.id == streamID else { return }
                self.sweepComparison = nil
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.sweepComparisonError = "Архивный свип не найден или сервер не поддерживает сравнение (404)"
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.sweepComparisonError = "Свипы несовместимы (409): \(message)"
                default:
                    self.sweepComparisonError = describeSession(error)
                }
                self.finishSweepComparison()
            }
        }
        comparisonTimeoutTask = Task { [weak self, comparisonTimeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: comparisonTimeoutNanoseconds) }
            catch { return }
            guard let self, revision == self.comparisonRevision, self.sweepComparisonPending else { return }
            self.cancelSweepComparison()
            self.sweepComparisonError = "Время ожидания сравнения истекло. Можно повторить запрос."
        }
    }

    private func finishSweepComparison() {
        comparisonTimeoutTask?.cancel()
        comparisonTimeoutTask = nil
        comparisonTask = nil
        sweepComparisonPending = false
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
        guard let ndjsonType = UTType(filenameExtension: "ndjson", conformingTo: .plainText) else {
            archiveExportStatus = "Не удалось определить тип файла NDJSON"
            return
        }
        panel.allowedContentTypes = [ndjsonType]
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
        resetComparisonScope()
        frequencyRounding.reset()
        session = nil
        workers = []
        latestObservation = nil
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
        taskCancelStatus = "Queued ControlTask не отменена"
        taskCancelPending = false
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
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            frequencyRounding.configure(client: client, scope: .init(server: serverURL, session: selectedSessionID),
                candidates: snapshot.candidates, ready: !snapshot.workflowErrors.contains { $0.hasPrefix("candidates:") })
        } else { frequencyRounding.reset() }

        frequencyRaster.configure(client: (connectedServerURL == serverURL ? client : nil),
            scope: connectedServerURL == serverURL
                ? .init(server: serverURL, session: selectedSessionID) : nil,
            candidates: snapshot.candidates)
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           let stream = analyzer?.id {
            measurementResults.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream))
            frequencyCounter.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                sweeps: snapshot.sweeps)
        } else { measurementResults.reset(); frequencyCounter.reset() }
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            dvbEvaluation.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID))
            measurementMap.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID))
            configureAuxiliaryStreams(client: client, session: snapshot.session)
        } else { dvbEvaluation.reset(); measurementMap.reset(); configureAuxiliaryStreams(client: nil, session: nil) }
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
        let selectedSweeps = snapshot.sweeps.filter {
            PlatformThreeSweepThresholds.sameID($0.sessionId, selectedSessionID)
                && PlatformThreeSweepThresholds.sameID($0.streamId, analyzer?.id) && $0.levelUnit == "dBm"
        }
        comparisonSweeps = selectedSweeps
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL), let stream = analyzer?.id {
            sweepContext.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream), sweeps: selectedSweeps)
            threeSweepThresholds.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                source: selectedSweeps.last)
            sweepAverage.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                sweeps: selectedSweeps)
            spectralMask.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                source: selectedSweeps.last)
            sweepThreshold.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                source: selectedSweeps.last)
        } else { sweepAverage.reset(); sweepContext.reset(); threeSweepThresholds.reset(); sweepThreshold.reset(); spectralMask.reset() }
        if let scope = currentReferenceScope, scope != loadedReferenceScope {
            refreshSweepReferences()
        }
        if !selectedSweeps.contains(where: { $0.id == comparisonSelectionID }) {
            comparisonSelectionID = selectedSweeps.last?.id ?? ""
        }
        latestObservation = snapshot.observations.last
        if PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL), let stream = analyzer?.id,
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            spectrumPresentation.update(scope: .init(server: serverURL, session: selectedSessionID, stream: stream), sweeps: snapshot.sweeps)
        } else { spectrumPresentation.reset() }
        if !spectrumPresentation.paused { renderSpectrumPresentation() }
    }

    func toggleSpectrumPause() { spectrumPresentation.togglePause(); renderSpectrumPresentation() }
    func fitSpectrum() { spectrumPresentation.fit() }
    func applySpectrumBounds() { spectrumPresentation.setBounds(lower: spectrumLower, upper: spectrumUpper) }
    private func clearSpectrumPresentation() {
        spectrumPresentation.reset(); renderSpectrumPresentation()
    }
    private func renderSpectrumPresentation() {
        let sweeps = spectrumPresentation.sweeps
        var envelope = TraceEnvelope()
        var found: [Exceedance] = []
        var history = WaterfallHistory(capacity: PlatformSpectrumPresentation.capacity)
        for sweep in sweeps {
            let bins = zip(sweep.frequenciesHz, sweep.levels).map { LevelBin(frequency: $0, level: $1) }
            envelope.absorb(bins)
            found = ExceedanceList.update(existing: found, bins: bins, threshold: thresholdDbm ?? -80)
            history.absorb(bins)
        }
        current = sweeps.last.map(plot) ?? []
        minimum = envelope.minimumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        maximum = envelope.maximumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        rows = found
        // Polls and source metadata changes do not invalidate identical pixels.
        // Scale changes have their own WaterfallView.onChange observer.
        if waterfall.grid != history.grid || waterfall.rows != history.rows || waterfall.capacity != history.capacity {
            waterfall = history
            waterfallRevision += 1
        }
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
                        .disabled(model.receiver == nil || model.current.isEmpty || model.busy || model.spectrumPresentation.paused)
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
                    threshold: model.thresholdDbm, yTitle: "дБм (исходные)", displayScale: model.spectrumPresentation.scale
                )
                HStack {
                    Button(model.spectrumPresentation.paused ? "Live" : "Pause view") { model.toggleSpectrumPause() }
                    Button("Fit") { model.fitSpectrum() }
                    TextField("Min", text: $model.spectrumLower).frame(width: 85)
                    TextField("Max", text: $model.spectrumUpper).frame(width: 85)
                    Button("Применить") { model.applySpectrumBounds() }
                }.disabled(model.spectrumPresentation.sweeps.isEmpty)
                Text("Границы: десятичная точка / e; обрезано отсчётов: \(model.spectrumPresentation.clippedCount)").font(.caption)
                ErrorLine(text: model.spectrumPresentation.error)
                if let scale = model.spectrumPresentation.scale {
                    Text("Шкала: \(String(scale.lower)) … \(String(scale.upper)) dBm").font(.caption)
                }
                if let sweep = model.spectrumPresentation.sweeps.last {
                    Text("\(model.spectrumPresentation.paused ? "Pause view" : "Live") · \(sweep.id) · \(sweep.interval.startedAt) → \(sweep.interval.endedAt)").font(.caption)
                    Text("Исходные уровни: \(sweep.levels.min().map { String($0) } ?? "—") … \(sweep.levels.max().map { String($0) } ?? "—") dBm").font(.caption)
                } else { Text("Спектр недоступен") }
                WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "дБм", displayScale: model.spectrumPresentation.scale)
                Text("Строки: порядок свипов, новые сверху, не шкала времени. Столбцы: порядок bins; интервалы частот могут быть неравными.").font(.caption)
                Text("Пауза замораживает только графики. Опрос и состояние сбора/записи не меняются; панели анализа независимы.").font(.caption)
                RemoteSweepComparisonPane(model: model)
                RemoteSweepAveragePane(model: model.sweepAverage)
                RemoteThreeSweepThresholdsPane(model: model.threeSweepThresholds)
                RemoteSweepContextPane(model: model.sweepContext)
                RemoteSweepThresholdPane(model: model.sweepThreshold)
                RemoteSpectralMaskPane(model: model.spectralMask)
                RemoteFrequencyRasterPane(model: model.frequencyRaster)
                RemoteMeasurementResultsPane(model: model.measurementResults)
                RemoteFrequencyCounterPane(model: model.frequencyCounter)
                RemoteDvbEvaluationPane(model: model.dvbEvaluation)
                Picker("Поток фонограммы / обмена", selection: $model.auxiliaryStreamID) {
                    ForEach(model.session?.streams ?? [], id: \.id) { stream in
                        Text("\(stream.device) · \(stream.id) · \(stream.levelUnit)").tag(stream.id)
                    }
                }.disabled(model.session?.streams.isEmpty ?? true)
                RemotePhonogramPane(model: model.phonogram)
                RemoteSignalListPane(model: model.signalList)
                RemoteMeasurementMapPane(model: model.measurementMap)
                RemoteAudioStreamPane(model: model.audioStream)
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

struct RemoteSweepComparisonPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        GroupBox("Сравнение архивных свипов · dB") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Опорный свип из snapshot", selection: $model.comparisonSelectionID) {
                    if model.comparisonSweeps.isEmpty { Text("Нет dBm свипов").tag("") }
                    ForEach(model.comparisonSweeps) { sweep in
                        Text("\(sweep.id) · \(sweep.interval.startedAt)").tag(sweep.id)
                    }
                }
                Button("Закрепить опорный свип") { model.pinComparisonReference() }
                    .disabled(!model.canPinComparisonReference)
                savedReferences
                if let reference = model.comparisonReference {
                    if let name = reference.name { Text("Сохранённое имя: \(name)") }
                    Text("Опорный: \(reference.id)")
                    Text("UTC: \(reference.interval.startedAt) — \(reference.interval.endedAt)")
                        .font(.caption)
                    HStack {
                        Button("Сравнить с последним свипом") { model.compareLatestSweep() }
                            .disabled(!model.canCompareSweep)
                        Button("Очистить") { model.clearSweepComparison() }
                    }
                    if model.sweepComparisonPending {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Ядро сравнивает архивные свипы…").font(.caption)
                            Button("Отмена") { model.cancelSweepComparison() }
                        }
                    }
                }
                if let result = model.sweepComparison {
                    Text("Целевой: \(result.target.id)")
                    Text("UTC: \(result.target.interval.startedAt) — \(result.target.interval.endedAt)")
                        .font(.caption)
                    Text("Опорный результат: \(result.reference.id) · UTC \(result.reference.interval.startedAt) — \(result.reference.interval.endedAt)")
                        .font(.caption)
                    TraceChart(
                        current: zip(result.frequenciesHz, result.deltaLevels).map {
                            PlotPoint(x: Double($0) / 1_000_000, y: $1)
                        }, minimum: [], maximum: [], threshold: 0, yTitle: "dB · целевой − опорный",
                        currentTitle: "разность", thresholdTitle: "ноль"
                    )
                    Text("Частота, МГц").font(.caption).foregroundStyle(.secondary)
                    Text("Максимальное изменение: \(change(result.maxIncrease))")
                    Text("Минимальное изменение: \(change(result.minChange))")
                    Text("\(result.points) точек · исходные уровни dBm сохранены отдельно")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ErrorLine(text: model.sweepComparisonError)
                Text("Только по кнопке, без интерполяции и калибровки. Настройки сравнивает ядро; совпадение записанных настроек не доказывает одинаковую антенну или состояние прибора.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var savedReferences: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Имя опорного свипа (1–80 символов Unicode)", text: $model.referenceName)
            Button("Сохранить закреплённый опорный свип") { model.savePinnedSweepReference() }
                .disabled(!model.canSaveSweepReference)
            HStack {
                Button("Обновить каталог") { model.refreshSweepReferences() }
                    .disabled(!model.canRefreshSweepReferences)
                if model.referenceRequestPending {
                    ProgressView().controlSize(.small)
                    Button("Прекратить ожидание") { model.cancelReferenceRequest() }
                }
            }
            if let catalog = model.referenceCatalog {
                Text(model.referenceStorageDescription)
                    .font(.caption)
                Text("\(model.savedSweepReferences.count) в потоке · максимум \(catalog.limit) на сеанс")
                    .font(.caption)
            }
            Picker("Сохранённый опорный свип", selection: $model.savedReferenceSelectionID) {
                if model.savedSweepReferences.isEmpty { Text("Нет записей").tag("") }
                ForEach(model.savedSweepReferences) { record in
                    Text("\(record.name) · \(record.sweepId)").tag(record.id)
                }
            }
            Button("Использовать сохранённый опорный свип") { model.useSavedSweepReference() }
                .disabled(!model.canUseSavedReference)
            if let status = model.referenceSaveStatus { Text(status).font(.caption) }
            ErrorLine(text: model.referenceCatalogError)
            ErrorLine(text: model.referenceWriteNotice)
        }
    }

    private func change(_ value: PlatformComparisonExtremum) -> String {
        String(format: "%+.2f dB · %.6f МГц", value.deltaDb, Double(value.frequencyHz) / 1_000_000)
    }
}


import AVFoundation

@MainActor
final class MonitoringWorkspaceModel: ObservableObject {
    @Published var projects: [MonitoringValue] = []
    @Published var selected = "" {
        didSet { if oldValue != selected { invalidateProject() } }
    }
    @Published var status: MonitoringValue = .null
    @Published var preflight: MonitoringValue = .null
    @Published var events: [MonitoringValue] = []
    @Published var selectedEvent = ""
    @Published var event: MonitoringValue = .null
    @Published var audioInputs: MonitoringValue = .null
    @Published var receiverMode = "AUTO"
    @Published var audioEnabled = false
    @Published var selectedInput = ""
    @Published var audioConfirmed = false
    @Published var details = "Выберите подготовленный сеанс."
    @Published var notice = ""
    @Published var error = ""
    @Published var audioNotice = ""
    @Published var pending = false
    @Published var hasPlayback = false
    @Published var audioLoading = false
    private var player: AVAudioPlayer?
    private var client: PlatformClient?
    private var scope = ""
    private var revision = 0
    private var requestID = UUID()
    private var playbackRevision = 0
    let receiverModes = ["AUTO", "FM", "AM", "DSTAR", "YSF", "ALINCO", "NXDN", "P25", "DPMR", "DMR"]

    var project: MonitoringValue { status["project"] }
    var devices: [MonitoringValue] { audioInputs["devices"].items }
    var chosenDevice: MonitoringValue? { devices.first { $0["index"].scalar == selectedInput } }
    var isArmed: Bool { status["active"].bool == true || project["state"].text == "armed" }
    var canConfigure: Bool { !selected.isEmpty && status != .null && !isArmed && !pending }
    var configurationDirty: Bool {
        let savedMode = project["receiver_mode"].text.isEmpty ? "AUTO" : project["receiver_mode"].text
        if savedMode != receiverMode || (project["audio"] != .null) != audioEnabled { return true }
        return audioEnabled && (chosenDevice?["index"] != project["audio"]["device"] || chosenDevice?["name"] != project["audio"]["name"])
    }
    var canSaveConfiguration: Bool {
        canConfigure && (!audioEnabled || (audioInputs["available"].bool == true && chosenDevice != nil && audioConfirmed))
    }
    var canArm: Bool { canConfigure && !configurationDirty }
    var canPlay: Bool { !pending && !selectedEvent.isEmpty && event["analysis"]["audio"] != .null }

    func configure(_ server: String, _ session: String) {
        let next = server + "|" + session
        guard next != scope else { return }
        scope = next; selected = ""; projects = []; audioInputs = .null
        invalidateProject()
        do { client = server.isEmpty ? nil : try PlatformClient(baseURL: server) }
        catch { client = nil; self.error = error.localizedDescription }
    }

    private func invalidateProject() {
        revision += 1; requestID = UUID(); pending = false
        status = .null; preflight = .null; events = []; selectedEvent = ""; event = .null
        receiverMode = "AUTO"; audioEnabled = false; selectedInput = ""; audioConfirmed = false
        details = "Выберите проект и обновите состояние."; error = ""; notice = ""
        stopAudio()
    }

    func stopAudio() {
        playbackRevision += 1
        if audioLoading { requestID = UUID(); pending = false }
        audioLoading = false
        player?.stop(); player = nil; hasPlayback = false; audioNotice = ""
    }

    // Every completion belongs to the exact endpoint, project and request that started it.
    @discardableResult
    private func run(
        eventID: String? = nil,
        request: (PlatformClient, String) async throws -> MonitoringValue,
        apply: (MonitoringValue) -> Void
    ) async -> Bool {
        guard let client, !pending else { return false }
        let current = revision, identifier = selected, token = UUID()
        requestID = token; pending = true; error = ""
        defer { if requestID == token { pending = false } }
        do {
            let result = try await request(client, identifier)
            guard current == revision, requestID == token, selected == identifier,
                  eventID == nil || selectedEvent == eventID, !Task.isCancelled else { return false }
            details = result.pretty
            apply(result)
            return true
        } catch {
            if current == revision, requestID == token, selected == identifier, !Task.isCancelled {
                self.error = error.localizedDescription
            }
            return false
        }
    }

    func load() async {
        let currentScope = scope
        await reloadProjects()
        guard scope == currentScope, !Task.isCancelled else { return }
        await loadAudioInputs()
    }

    func reloadProjects() async {
        let loaded = await run(request: { client, _ in try await client.monitoring() }) { result in
            // Only the collection root is allowed to replace the project selector.
            projects = result["items"].items
            if !projects.contains(where: { $0["id"].text == selected }) {
                selected = projects.first?["id"].text ?? ""
            }
        }
        if loaded, !selected.isEmpty, !Task.isCancelled { await refreshStatus(syncConfiguration: true) }
    }

    func createProject(name: String, site: String, sessionID: String) async {
        let created = await run(request: { client, _ in
            try await client.monitoring(body: ["name": .string(name), "site": .string(site), "session_id": .string(sessionID)])
        }) { result in
            projects.insert(result, at: 0); selected = result["id"].text
            notice = "Проект создан. Приборы запускаются только кнопкой постановки на охрану."
        }
        if created, !Task.isCancelled { await refreshStatus(syncConfiguration: true) }
    }

    func refreshStatus(syncConfiguration: Bool = false) async {
        guard !selected.isEmpty else { return }
        await run(request: { client, id in try await client.monitoringProject(id, resource: "status") }) { result in
            status = result
            if syncConfiguration {
                let mode = project["receiver_mode"].text
                receiverMode = mode.isEmpty ? "AUTO" : mode
                audioEnabled = project["audio"] != .null
                selectedInput = audioEnabled ? project["audio"]["device"].scalar : ""
                // Each save of an assigned audio input requires an explicit operator confirmation.
                audioConfirmed = false
            }
        }
    }

    func loadAudioInputs() async {
        await run(request: { client, _ in try await client.monitoring("/audio-inputs") }) { result in
            let oldDevice = chosenDevice
            audioInputs = result
            if oldDevice != chosenDevice { selectedInput = ""; audioConfirmed = false }
        }
    }

    func saveConfiguration() async {
        guard canSaveConfiguration else { return }
        let audio: MonitoringValue
        if audioEnabled, let device = chosenDevice {
            audio = .object(["device": device["index"], "name": device["name"], "confirm": .boolean(true)])
        } else { audio = .null }
        await command("configure", body: ["receiver_mode": .string(receiverMode), "audio": audio],
                      success: "Настройки приёмника сохранены.", syncConfiguration: true)
    }

    func command(_ resource: String, body: [String: MonitoringValue] = [:], success: String,
                 syncConfiguration: Bool = false) async {
        guard !selected.isEmpty, !pending else { return }
        let current = revision
        let succeeded = await run(request: { client, id in try await client.monitoringProject(id, resource: resource, body: body) }) { _ in
            notice = success
        }
        guard current == revision, !Task.isCancelled else { return }
        if ["arm", "disarm", "configure"].contains(resource) {
            // A failed/timeout command can still have reached the core; query its actual state.
            status = .null
            await refreshStatus(syncConfiguration: syncConfiguration && succeeded)
        }
    }

    func checkPreflight() async {
        guard !selected.isEmpty else { return }
        await run(request: { client, id in try await client.monitoringProject(id, resource: "preflight") }) { preflight = $0 }
    }

    func loadEvents() async {
        guard !selected.isEmpty else { return }
        await run(request: { client, id in try await client.monitoringProject(id, resource: "events") }) { result in
            events = result["items"].items
            if !events.contains(where: { $0["id"].text == selectedEvent }) {
                selectedEvent = ""; event = .null; stopAudio()
            }
            notice = "Событий в текущей странице: \(events.count)."
        }
    }

    func showEvent(_ id: String) async {
        guard !pending else { return }
        stopAudio(); selectedEvent = id; event = .null
        await run(eventID: id, request: { client, project in
            try await client.monitoringEvent(projectID: project, eventID: id)
        }) { event = $0 }
    }

    func playAudio() async {
        guard let client, canPlay else { return }
        stopAudio()
        let current = revision, identifier = selected, eventID = selectedEvent, token = UUID()
        let playbackScope = playbackRevision
        requestID = token; pending = true; audioLoading = true; error = ""
        defer {
            if requestID == token { pending = false }
            if playbackRevision == playbackScope { audioLoading = false }
        }
        do {
            let bytes = try await client.monitoringEventAudio(projectID: identifier, eventID: eventID)
            guard current == revision, requestID == token, selected == identifier,
                  selectedEvent == eventID, playbackRevision == playbackScope, !Task.isCancelled else { return }
            let playback = try AVAudioPlayer(data: bytes)
            guard playback.prepareToPlay(), playback.play() else {
                throw PlatformError.invalidResponse("Не удалось воспроизвести WAV")
            }
            player = playback; hasPlayback = true
            audioNotice = String(format: "WAV загружен: %.1f с · %d байт", playback.duration, bytes.count)
        } catch {
            if current == revision, requestID == token, selectedEvent == eventID,
               playbackRevision == playbackScope, !Task.isCancelled {
                self.error = error.localizedDescription
            }
        }
    }
}

struct MonitoringWorkspacePane: View {
    let server: String
    let sessionID: String
    @StateObject private var workspace = MonitoringWorkspaceModel()
    @State private var name = "Наблюдение"
    @State private var site = "Объект"
    @State private var confirm = false
    @State private var allowedName = "Разрешённый сигнал"
    @State private var allowedStart = "100000000"
    @State private var allowedEnd = "100100000"
    @State private var watchChanges = false

    var body: some View {
        DisclosureGroup("Проект наблюдения · охрана и события") {
            ScrollView {
              VStack(alignment: .leading, spacing: 10) {
                preparation
                receiverControls
                allowedControls
                eventControls
                if workspace.pending { ProgressView("Запрос к ядру…").controlSize(.small) }
                if !workspace.notice.isEmpty { Text(workspace.notice).font(.caption) }
                ErrorLine(text: workspace.error)
                Text("Аппаратная проверка не выполнена. Приборы и запись звука обслуживает ядро; закрытие панели не снимает проект с охраны.")
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Диагностический JSON последнего ответа") {
                    ScrollView { Text(workspace.details).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 160)
                }
              }
            }
            .frame(maxHeight: 440)
        }
        .padding(.horizontal)
        .task(id: server + "|" + sessionID) { workspace.configure(server, sessionID); await workspace.load() }
        .onChange(of: workspace.selected) { _ in
            confirm = false
            Task { await workspace.refreshStatus(syncConfiguration: true) }
        }
        .onChange(of: workspace.selectedInput) { _ in workspace.audioConfirmed = false }
        .onChange(of: workspace.audioEnabled) { _ in workspace.audioConfirmed = false }
        .onDisappear { workspace.stopAudio() }
    }

    private var preparation: some View {
        GroupBox("Подготовка") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Название", text: $name)
                    TextField("Объект", text: $site)
                    Button("Создать") { Task { await workspace.createProject(name: name, site: site, sessionID: sessionID) } }
                        .disabled(server.isEmpty || sessionID.isEmpty || workspace.pending)
                    Button("Обновить проекты") { Task { await workspace.reloadProjects() } }
                        .disabled(server.isEmpty || workspace.pending)
                }
                Picker("Проект", selection: $workspace.selected) {
                    Text("Не выбран").tag("")
                    ForEach(Array(workspace.projects.enumerated()), id: \.offset) { _, p in
                        Text(p["name"].text + " · " + p["site"].text).tag(p["id"].text)
                    }
                }
                HStack {
                    Button("Проверить готовность") { Task { await workspace.checkPreflight() } }
                    Toggle("Подтверждаю обычную обстановку", isOn: $confirm)
                    Button("Зафиксировать эталон") { Task {
                        await workspace.command("baselines", body: ["name": .string(name), "location": .string(site), "confirm": .boolean(confirm)], success: "Эталон сохранён.")
                    } }.disabled(!confirm || workspace.isArmed)
                    Button("Анализ новых свипов") { Task { await workspace.command("analyze", success: "Анализ новых свипов выполнен.") } }
                }.disabled(workspace.selected.isEmpty || workspace.pending)
                if workspace.preflight != .null {
                    Text(workspace.preflight["ready"].bool == true ? "Подготовка: успешно" : "Подготовка: требуется внимание")
                    Text("Эталон: \(workspace.preflight["baseline_ready"].bool == true ? "готов" : "не создан")").font(.caption)
                    ForEach(Array(workspace.preflight["checks"].items.enumerated()), id: \.offset) { _, check in
                        Text("\(check["ok"].bool == true ? "✓" : "⚠") \(check["detail"].text)").font(.caption)
                    }
                }
            }
        }
    }

    private var receiverControls: some View {
        GroupBox("Приёмник и автономная охрана") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button("Обновить состояние") { Task { await workspace.refreshStatus() } }
                        .disabled(workspace.selected.isEmpty || workspace.pending)
                    Button("Поставить на охрану") { Task { await workspace.command("arm", success: "Команда постановки на охрану выполнена.") } }
                        .disabled(!workspace.canArm)
                    Button("Снять с охраны") { Task { await workspace.command("disarm", success: "Проект снят с охраны.") } }
                        .disabled(workspace.selected.isEmpty || workspace.pending)
                }
                if workspace.status == .null {
                    Text("Состояние неизвестно: запросите его у ядра.").foregroundStyle(.secondary)
                } else {
                    Text("Состояние проекта: \(label(workspace.project["state"].text)) · охрана: \(workspace.status["active"].bool == true ? "активна" : "не активна")")
                    Text("Приёмник: \(workspace.status["receiver_busy"].bool == true ? "исследует событие" : "свободен") · аудиовход: \(workspace.status["audio_configured"].bool == true ? "назначен" : "не назначен")").font(.caption)
                    ErrorLine(text: workspace.status["error"].text)
                    ForEach(Array(workspace.status["workers"].items.enumerated()), id: \.offset) { _, worker in
                        Text("Исполнитель \(worker["stream_id"].text): \(label(worker["state"].text)) · \(worker["last_error"].text)").font(.caption)
                    }
                }
                HStack {
                    Picker("Режим приёмника", selection: $workspace.receiverMode) {
                        ForEach(workspace.receiverModes, id: \.self) { Text($0).tag($0) }
                    }.frame(width: 240).disabled(!workspace.canConfigure)
                    Button("Обновить аудиовходы ядра") { Task { await workspace.loadAudioInputs() } }
                        .disabled(server.isEmpty || workspace.pending)
                }
                Text("AUTO и режимы AR-DV1 задаются явно; TETRA не входит в AUTO.").font(.caption).foregroundStyle(.secondary)
                Toggle("Записывать аналоговый звук AR-DV1", isOn: $workspace.audioEnabled).disabled(!workspace.canConfigure)
                if workspace.audioInputs["available"].bool != true {
                    Text("Аудиовходы недоступны: \(workspace.audioInputs["reason"].text.isEmpty ? "список ещё не получен" : workspace.audioInputs["reason"].text)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if workspace.audioEnabled {
                    Picker("Аудиовход на сервере", selection: $workspace.selectedInput) {
                        Text("Выберите вход явно").tag("")
                        ForEach(Array(workspace.devices.enumerated()), id: \.offset) { _, device in
                            Text("\(device["index"].scalar) · \(device["name"].text)").tag(device["index"].scalar)
                        }
                    }.disabled(!workspace.canConfigure)
                    Toggle("Подтверждаю, что выбран аналоговый выход AR-DV1", isOn: $workspace.audioConfirmed)
                        .disabled(!workspace.canConfigure || workspace.chosenDevice == nil)
                }
                Button("Сохранить режим и аудиовход") { Task { await workspace.saveConfiguration() } }
                    .disabled(!workspace.canSaveConfiguration)
                if workspace.configurationDirty { Text("Есть несохранённые настройки; сохраните их перед постановкой на охрану.").font(.caption) }
            }
        }
    }

    private var allowedControls: some View {
        GroupBox("Разрешённые сигналы") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Название сигнала", text: $allowedName)
                    TextField("От, Гц", text: $allowedStart)
                    TextField("До, Гц", text: $allowedEnd)
                    Button("Разрешить") { Task {
                        await workspace.command("allowed", body: ["name": .string(allowedName), "start_hz": .integer(Int64(allowedStart) ?? -1), "end_hz": .integer(Int64(allowedEnd) ?? -1), "watch_changes": .boolean(watchChanges)], success: "Разрешённый сигнал сохранён.")
                    } }
                }
                Toggle("Наблюдать изменения разрешённого сигнала", isOn: $watchChanges)
            }.disabled(workspace.selected.isEmpty || workspace.pending)
        }
    }

    private var eventControls: some View {
        GroupBox("События и записи") {
            VStack(alignment: .leading, spacing: 8) {
                Button("Обновить события") { Task { await workspace.loadEvents() } }
                    .disabled(workspace.selected.isEmpty || workspace.pending)
                if workspace.events.isEmpty { Text("Список событий пуст или ещё не загружен.").font(.caption) }
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(Array(workspace.events.enumerated()), id: \.offset) { _, item in
                            Button { Task { await workspace.showEvent(item["id"].text) } } label: {
                                VStack(alignment: .leading) {
                                    Text("\(label(item["kind"].text)) · \(item["center_frequency_hz"].scalar) Гц · \(label(item["state"].text))")
                                    Text("UTC \(item["last_seen"].text) · приёмник: \(label(item["analysis"]["state"].text))").font(.caption)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.disabled(workspace.pending)
                        }
                    }
                }.frame(maxHeight: 160)
                if workspace.event != .null { eventDetails }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var eventDetails: some View {
        let event = workspace.event
        let analysis = event["analysis"]
        return VStack(alignment: .leading, spacing: 6) {
            Text("Событие \(event["id"].text)").font(.headline)
            Text("\(label(event["kind"].text)) · \(event["start_frequency_hz"].scalar)…\(event["end_frequency_hz"].scalar) Гц")
            Text("Уровень \(event["peak_level"].scalar) dBm · превышение \(event["peak_excess_db"].scalar) dB")
            Text("UTC: \(event["first_seen"].text) — \(event["last_seen"].text)").font(.caption)
            Text("Исследование: \(label(analysis["state"].text)) · запрошенный режим: \(analysis["requested_mode"].scalar)")
            ErrorLine(text: analysis["error"].text)
            ErrorLine(text: analysis["reason"].text)
            ErrorLine(text: analysis["audio_error"].text)
            ForEach(Array(analysis["classifications"].items.enumerated()), id: \.offset) { _, item in
                Text("Декодер: \(item["mode"].text) · источник: \(item["source"].text)").font(.caption)
            }
            ForEach(Array(analysis["receiver_status"].items.enumerated()), id: \.offset) { _, item in
                Text("AR-DV1: \(item["frequency_hz"].scalar) Гц · \(item["level"].scalar) \(item["level_unit"].text) · шумодав: \(item["squelch_open"].scalar)").font(.caption)
            }
            if analysis["audio"] != .null {
                Text("Архивная фонограмма WAV: \(analysis["audio"]["filename"].text)").font(.caption)
                HStack {
                    Button("Воспроизвести WAV") { Task { await workspace.playAudio() } }.disabled(!workspace.canPlay)
                    Button("Стоп") { workspace.stopAudio() }.disabled(!workspace.hasPlayback && !workspace.audioLoading)
                    Text(workspace.audioNotice).font(.caption)
                }
                Text("Загрузка до 16 МиБ через API ядра.").font(.caption).foregroundStyle(.secondary)
            } else { Text("Записи WAV для события нет.").font(.caption) }
            DisclosureGroup("JSON выбранного события") {
                ScrollView { Text(event.pretty).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }.frame(maxHeight: 160)
            }
        }.textSelection(.enabled)
    }

    private func label(_ value: String) -> String {
        ["draft": "подготовка", "ready": "готов", "armed": "на охране", "error": "ошибка",
         "running": "работает", "stopped": "остановлен", "connected": "подключён",
         "persistent": "устойчивый сигнал", "impulse": "импульс", "broadband_change": "широкополосное изменение",
         "authorized_changed": "изменение разрешённого сигнала", "open": "открыто", "closed": "закрыто",
         "queued": "в очереди", "observed": "наблюдался приёмником", "not_present_at_receiver": "не обнаружен приёмником",
         "no_observations": "нет наблюдений", "expired": "исчез до перестройки", "interrupted": "прервано",
         "failed": "не выполнено"][value] ?? (value.isEmpty ? "нет данных" : value)
    }
}
