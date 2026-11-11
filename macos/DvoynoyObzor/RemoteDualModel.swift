import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

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
    @Published var authorizationRequired = false
    @Published var busy = false
    @Published var status = "Сервер не подключён"
    @Published var errorText: String?
    @Published var mode = "FM"
    @Published var threshold = "-80"
    @Published var spectrumPresentation = PlatformSpectrumPresentation()
    @Published var spectrumClippedCount = 0
    @Published var spectrumLower = ""
    @Published var spectrumUpper = ""
    @Published var current: [PlotPoint] = []
    @Published var minimum: [PlotPoint] = []
    @Published var maximum: [PlotPoint] = []
    @Published var rows: [Exceedance] = []
    @Published var waterfall = WaterfallHistory()
    @Published var waterfallRevision = 0
    @Published var latestObservation: PlatformObservation?
    @Published var eventGapNotice: String?
    @Published var eventStatus = "Ожидание подключения"
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
    // The app-internal action extensions share these setters across source files.
    @Published var comparisonSweeps: [PlatformSweep] = []
    @Published var comparisonSelectionID = ""
    @Published var comparisonReference: PinnedSweepReference?
    @Published var sweepComparison: PlatformSweepComparison?
    @Published var sweepComparisonError: String?
    @Published var sweepComparisonPending = false
    @Published var referenceName = ""
    @Published var savedReferenceSelectionID = ""
    @Published var referenceCatalog: PlatformSweepReferenceCatalog?
    @Published var savedSweepReferences: [PlatformSweepReference] = []
    @Published var referenceCatalogError: String?
    @Published var referenceSaveStatus: String?
    @Published var referenceWriteNotice: String?
    @Published var referenceCatalogPending = false
    @Published var referenceSavePending = false
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
    let sweepQuality = SweepQualityModel()
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
    let comparisonTimeoutNanoseconds: UInt64
    var comparisonTask: Task<Void, Never>?
    var comparisonTimeoutTask: Task<Void, Never>?
    var comparisonRevision = 0
    typealias ReferenceScope = PlatformSpectrumPresentation.Scope

    var connectedServerURL: String?
    var loadedReferenceScope: ReferenceScope?
    var referenceRequestScope: ReferenceScope?
    var uncertainReferenceScopes: Set<ReferenceScope> = []
    var referenceNoticeScope: ReferenceScope?
    var referenceRequestRevision = 0
    var referenceTask: Task<Void, Never>?
    var referenceTimeoutTask: Task<Void, Never>?
    var connectionGeneration = 0
    private var capacityRequestRevision = 0
    var client: PlatformClient?
    private var pollTask: Task<Void, Never>?
    private let measurementStream = RemoteMeasurementStream()
    private var streamingEnabled = false
    private var measurementSnapshot: PlatformSnapshot?
    private var spectrumRenderRevision = 0
    var generation = 0
    private var sessionRefreshRevision = 0
    var detectionFormInitialized = false
    var handoffFormInitialized = false
    var completionFormInitialized = false
    var taskSchedulerCommandRevision = 0

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
    var usesBundledLegacyHelper: Bool {
        let environment = ProcessInfo.processInfo.environment
        guard environment["ORI_BUNDLED_LEGACY_CORE"] == "1",
              let owned = environment["ORI_LOCAL_CORE_URL"],
              let ownedURL = try? PlatformBaseURL(owned),
              let selectedURL = try? PlatformBaseURL(serverURL) else { return false }
        return ownedURL == selectedURL
    }
    var selectedCandidate: PlatformCandidate? {
        candidates.first(where: { PlatformExactID.same($0.id, selectedCandidateID) })
    }
    var selectedInvestigation: PlatformInvestigation? {
        investigations.first(where: { PlatformExactID.same($0.id, selectedInvestigationID) })
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
        return workers.first(where: { PlatformExactID.same($0.streamId, streamID) })
    }

    func connect(startPolling: Bool = true, using preparedClient: PlatformClient? = nil) {
        measurementStream.stop(); measurementSnapshot = nil; eventGapNotice = nil
        frequencyRounding.reset()
        do {
            client = try preparedClient ?? makeClient(serverURL)
            resetComparisonScope()
            connectedServerURL = serverURL
            connectionGeneration += 1
            archiveCapacity = nil
            archiveCapacityError = nil
            connected = true
            authorizationRequired = false
            errorText = nil
            status = "Сервер подключён; приборные порты остаются у ядра"
            generation += 1
            streamingEnabled = startPolling
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
        measurementStream.stop()
        streamingEnabled = false
        eventStatus = "UI отключён; охрана и архив продолжают работу в ядре"
        client = nil
        configureAuxiliaryStreams(client: nil, session: nil)
        connected = false
        authorizationRequired = false
        busy = false
        session = nil
        sessions = []
        selectedSessionID = ""
        clearMeasurements()
        status = "Отключено от сервера; сеанс ядра не остановлен"
    }

    @discardableResult
    func disconnectIfCurrent(connection: Int, server: String) -> Bool {
        guard connection == connectionGeneration, PlatformExactID.same(server, serverURL) else { return false }
        disconnect()
        return true
    }

    func reloadSessions() async {
        guard let client else { return }
        let requestGeneration = generation
        do {
            let loaded = try await client.listSessions()
            guard requestGeneration == generation, self.client === client else { return }
            sessions = loaded
            if selectedSessionID.isEmpty || !loaded.contains(where: { PlatformExactID.same($0.id, selectedSessionID) }) {
                selectedSessionID = loaded.first?.id ?? ""
                clearMeasurements()
            }
            await refresh()
        } catch {
            guard requestGeneration == generation, self.client === client else { return }
            if expireAuthorization(error, client: client) { return }
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
            let streaming = measurementStream.isActive
            // Read the watermark before the seed snapshot: replay closes the commit race.
            let watermark = streamingEnabled && !streaming ? try? await client.eventWatermark() : nil
            var snapshot = try await client.snapshot(sessionID: id, includeArchiveCapacity: false, includeMeasurements: !streaming)
            if streaming, !measurementStream.matches(sessionID: id, streamID: snapshot.session.streams.first(where: { $0.isDbmAnalyzer })?.id ?? "") {
                measurementStream.stop()
                await refreshSession(client)
                return
            }
            if let cached = measurementStream.measurements {
                snapshot.sweeps = cached.sweeps
                snapshot.observations = cached.observation.map { [$0] } ?? []
            }
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client, PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            session = snapshot.session
            if let modes = receiver?.tuneModes, !modes.contains(mode) {
                mode = modes.first ?? "FM"
            }
            workers = snapshot.workers
            measurementSnapshot = snapshot
            guard await applyMeasurements(snapshot) else { return }
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client,
                  PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            applyWorkflow(snapshot, schedulerRevision: schedulerRevision)
            if streamingEnabled, !measurementStream.isActive, let watermark {
                eventStatus = "SSE · ожидание событий после \(watermark)"
                measurementStream.start(client: client, sessionID: id, streamID: analyzer?.id ?? "", receiverStreamID: receiver?.id, watermark: watermark,
                    sweeps: snapshot.sweeps, observation: snapshot.observations.last,
                    receive: { [weak self] sweeps, observation, cursor in
                        guard let self, requestGeneration == self.generation, self.client === client,
                              PlatformThreeSweepThresholds.sameID(id, self.selectedSessionID), var latest = self.measurementSnapshot else { return }
                        latest.sweeps = sweeps; latest.observations = observation.map { [$0] } ?? []
                        self.measurementSnapshot = latest
                        guard await self.applyMeasurements(latest) else { return }
                        guard requestGeneration == self.generation, self.client === client else { return }
                        self.eventStatus = "SSE · принято событие \(cursor)"
                        self.live = "Закоммичено ядром · \(sweeps.last?.interval.endedAt ?? observation?.interval.endedAt ?? "—")"
                    }, recover: { [weak self] reason in
                        guard let self, requestGeneration == self.generation, self.client === client else { return }
                        self.eventStatus = "Восстановление потока отображения"
                        if let eventError = reason as? PlatformEventError {
                            self.eventGapNotice = "Пропуск доставки в этот UI. Отображение восстанавливается из архива; охрана продолжает работать."
                            if eventError == .consumerOverrun {
                                self.streamingEnabled = false
                                self.eventGapNotice = "Буфер отображения переполнен. Включён опрос снимков; переподключите UI для возврата к потоку. Охрана продолжает работать."
                            }
                            await self.refresh()
                        } else if case PlatformError.http(status: 404, code: _, message: _) = reason {
                            self.streamingEnabled = false
                        }
                    })
            } else if !streamingEnabled || watermark == nil && !measurementStream.isActive {
                eventStatus = "REST · совместимый опрос (SSE не подтверждён)"
            }
            errorText = snapshot.workflowErrors.isEmpty ? nil : snapshot.workflowErrors.joined(separator: " · ")
            live = "Обновлено от ядра · свипов \(snapshot.sweeps.count) · наблюдений \(snapshot.observations.count)"
        } catch {
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client, PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            if expireAuthorization(error, client: client) { return }
            sweepAverage.reset()
            threeSweepThresholds.reset()
            sweepContext.reset()
            sweepQuality.reset()
            frequencyRounding.unavailable()
            clearSpectrumPresentation()
            errorText = describeSession(error)
        }
    }

    private func expireAuthorization(_ error: Error, client: PlatformClient) -> Bool {
        guard client.hasBearerCredential, case PlatformError.http(status: 401, code: _, message: _) = error else { return false }
        disconnect()
        authorizationRequired = true
        errorText = "Доступ этого Mac отозван или истёк. Выполните сопряжение заново; охрана ядра не изменена."
        return true
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

    func performSessionAction(
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

    func perform(
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        guard let client, !selectedSessionID.isEmpty else { return }
        let id = selectedSessionID
        let requestGeneration = generation
        busy = true
        Task {
            do {
                _ = try await operation(client, id)
                guard requestGeneration == generation, self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                await refresh()
            } catch {
                guard requestGeneration == generation, self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                errorText = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
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
        measurementStream.stop()
        measurementSnapshot = nil
        eventGapNotice = nil
        spectrumRenderRevision += 1
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

    private func applyMeasurements(_ snapshot: PlatformSnapshot) async -> Bool {
        let selectedSweeps = snapshot.sweeps.filter {
            PlatformThreeSweepThresholds.sameID($0.sessionId, selectedSessionID)
                && PlatformThreeSweepThresholds.sameID($0.streamId, analyzer?.id) && $0.levelUnit == "dBm"
        }
        let requestGeneration = generation, sourceClient = client
        let scope: PlatformSpectrumPresentation.Scope?
        if PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL), let stream = analyzer?.id,
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            scope = .init(server: serverURL, session: selectedSessionID, stream: stream)
        } else { scope = nil }
        // Analysis sources are readiness signals used by the UI and callers. Publish
        // them only after the corresponding off-main display preparation finishes.
        guard await prepareSpectrum(scope: scope, incoming: snapshot.sweeps),
              requestGeneration == generation, sourceClient === client else { return false }
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
            sweepQuality.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream), sweeps: selectedSweeps)
            spectralMask.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                source: selectedSweeps.last)
            sweepThreshold.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                source: selectedSweeps.last)
        } else { sweepAverage.reset(); sweepContext.reset(); threeSweepThresholds.reset(); sweepThreshold.reset(); spectralMask.reset(); sweepQuality.reset() }
        if let scope = currentReferenceScope, scope != loadedReferenceScope {
            refreshSweepReferences()
        }
        if !selectedSweeps.contains(where: { PlatformExactID.same($0.id, comparisonSelectionID) }) {
            comparisonSelectionID = selectedSweeps.last?.id ?? ""
        }
        latestObservation = snapshot.observations.last
        return true
    }

    func toggleSpectrumPause() {
        if spectrumPresentation.paused, let cache = measurementStream.measurements, let stream = analyzer?.id,
           PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL) {
            spectrumPresentation.update(scope: .init(server: serverURL, session: selectedSessionID, stream: stream), sweeps: cache.sweeps)
            if spectrumPresentation.paused { spectrumPresentation.togglePause() }
        } else { spectrumPresentation.togglePause() }
        renderSpectrumPresentation()
    }
    func fitSpectrum() { spectrumRenderRevision += 1; spectrumPresentation.fit(); spectrumClippedCount = spectrumPresentation.clippedCount }
    func applySpectrumBounds() { spectrumRenderRevision += 1; spectrumPresentation.setBounds(lower: spectrumLower, upper: spectrumUpper); spectrumClippedCount = spectrumPresentation.clippedCount }
    func clearSpectrumPresentation() { spectrumPresentation.reset(); renderSpectrumPresentation() }
    private func renderSpectrumPresentation() {
        spectrumRenderRevision += 1
        spectrumClippedCount = spectrumPresentation.clippedCount
        if let frame = try? NativeSpectrumFrame(sweeps: spectrumPresentation.sweeps, threshold: thresholdDbm ?? -80) { publish(frame) }
    }
    private func prepareSpectrum(scope: PlatformSpectrumPresentation.Scope?, incoming: [PlatformSweep]) async -> Bool {
        spectrumRenderRevision += 1
        let revision = spectrumRenderRevision, previous = spectrumPresentation, threshold = thresholdDbm ?? -80
        let work = Task.detached(priority: .userInitiated) { () throws -> (PlatformSpectrumPresentation, NativeSpectrumFrame?, Int) in
            var next = previous
            if let scope { next.update(scope: scope, sweeps: incoming) } else { next.reset() }
            let frame = next.paused ? nil : try NativeSpectrumFrame(sweeps: next.sweeps, threshold: threshold)
            return (next, frame, next.clippedCount)
        }
        let result = try? await withTaskCancellationHandler(operation: { try await work.value }, onCancel: { work.cancel() })
        guard !Task.isCancelled, revision == spectrumRenderRevision, let (next, frame, clipped) = result else { return false }
        spectrumPresentation = next; spectrumClippedCount = clipped
        if let frame { publish(frame) }
        return true
    }

    private func publish(_ frame: NativeSpectrumFrame) {
        current = frame.current; minimum = frame.minimum; maximum = frame.maximum; rows = frame.rows
        let history = frame.waterfall
        if waterfall.grid != history.grid || waterfall.rows != history.rows || waterfall.capacity != history.capacity {
            waterfall = history; waterfallRevision += 1
        }
    }


}
