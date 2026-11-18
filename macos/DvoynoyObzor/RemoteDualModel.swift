import Foundation
import Observation
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
@Observable
final class RemoteDualModel {
    var showFrequencyHistory = false
    var historyFrequency: Int64?
    var reviewedSweepRequest = UUID()
    var reviewedSweep: PlatformSweep?
    var showTinySAControl = false
    var serverURL = ProcessInfo.processInfo.environment["ORI_LOCAL_CORE_URL"] ?? "http://127.0.0.1:8770" {
        didSet {
            guard !PlatformExactID.same(serverURL, oldValue) else { return }
            selectedScanBand = ""
            coreReachable = false; lastCoreContactAt = nil; resetMeasurementHealth()
            resetComparisonScope(); frequencyRounding.reset(); configureAuxiliaryStreams(client: nil, session: nil)
        }
    }
    var sessions: [PlatformSession] = []
    var selectedSessionID = "" {
        didSet {
            guard !PlatformExactID.same(selectedSessionID, oldValue) else { return }
            selectedScanBand = ""
            resetMeasurementHealth(); resetComparisonScope(); frequencyRounding.reset()
            configureAuxiliaryStreams(client: nil, session: nil)
        }
    }
    var session: PlatformSession? {
        didSet {
            if !PlatformThreeSweepThresholds.sameID(session?.streams.first(where: { $0.isDbmAnalyzer })?.id,
                oldValue?.streams.first(where: { $0.isDbmAnalyzer })?.id) {
                if oldValue != nil { resetMeasurementHealth() }
                resetComparisonScope()
            }
        }
    }
    var auxiliaryStreamID = "" {
        didSet { if !PlatformThreeSweepThresholds.sameID(auxiliaryStreamID, oldValue) { applyAuxiliaryStream() } }
    }
    private var auxiliaryClient: PlatformClient?
    private var auxiliarySession: PlatformSession?
    var workers: [PlatformWorker] = []
    // Connection intent is distinct from a successfully refreshed core.
    var connected = false
    private(set) var coreReachable = false
    private(set) var lastCoreContactAt: Date?
    private(set) var measurementsStale = true
    private(set) var lastMeasurementAt: Date?
    // Display-only fallback: never used as archive evidence or an action source.
    private(set) var staleSpectrum: NativeSpectrumFrame?
    private(set) var staleSpectrumScale: PlatformSpectrumScale?
    private var lastRenderedFrame: NativeSpectrumFrame?
    private var lastRenderedSweeps: [PlatformSweep] = []
    // Allow a full-band hardware sweep (about 36 seconds) plus polling/retry margin.
    static let measurementFreshnessInterval: TimeInterval = 90
    var authorizationRequired = false
    var busy = false
    var status = "Сервер не подключён"
    let spectrumViewMemory = NativeSpectrumViewMemory()
    var errorText: String?
    var mode = "FM"
    var threshold = "-80"
    var spectrumPresentation = PlatformSpectrumPresentation()
    var selectedScanBand = ""
    var spectrumClippedCount = 0
    var spectrumLower = ""
    var spectrumUpper = ""
    var current: [PlotPoint] = []
    var minimum: [PlotPoint] = []
    var maximum: [PlotPoint] = []
    var rows: [Exceedance] = []
    var waterfall = WaterfallHistory()
    var waterfallRevision = 0
    var latestObservation: PlatformObservation?
    var eventGapNotice: String?
    var eventStatus = "Ожидание подключения"
    var measurementSource: PlatformMeasurementSource?
    var measurementDisplayIssue: PlatformMeasurementDisplayIssue?
    var live = "Данные ядра ещё не получены"
    var candidates: [PlatformCandidate] = []
    var investigations: [PlatformInvestigation] = []
    var nextInvestigation: PlatformInvestigation?
    var selectedCandidateID = ""
    var selectedInvestigationID = ""
    var investigationPriority = "0"
    var failureReason = ""
    var automaticHandoffStatus: PlatformAutomaticStatus?
    var automaticDetectionStatus: PlatformAutomaticStatus?
    var automaticCompletionStatus: PlatformAutomaticStatus?
    var taskSchedulerStatus: PlatformTaskSchedulerStatus?
    var taskSchedulerError: String?
    var taskSchedulerTasks: [PlatformControlTask] = []
    var taskSchedulerTasksError: String?
    var newTaskName = ""
    var newTaskFile = ""
    var newTaskStartAt = ""
    var newTaskDuration = "60"
    var newTaskEquipment = ""
    var newTaskStatus = "Новая ControlTask ещё не создана"
    var showTaskCreate = false
    var taskCreatePending = false
    var taskCancelStatus = "Queued ControlTask не отменена"
    var taskCancelPending = false
    // The app-internal action extensions share these setters across source files.
    var comparisonSweeps: [PlatformSweep] = []
    var comparisonSelectionID = ""
    var comparisonReference: PinnedSweepReference?
    var sweepComparison: PlatformSweepComparison?
    var sweepComparisonError: String?
    var sweepComparisonPending = false
    var referenceName = ""
    var savedReferenceSelectionID = ""
    var referenceCatalog: PlatformSweepReferenceCatalog?
    var savedSweepReferences: [PlatformSweepReference] = []
    var referenceCatalogError: String?
    var referenceSaveStatus: String?
    var referenceWriteNotice: String?
    var referenceCatalogPending = false
    var referenceSavePending = false
    var archiveCapacity: PlatformArchiveCapacity?
    var archiveCapacityError: String?
    var archiveAnalytics: PlatformAnalyticsSummary?
    var archiveAnalyticsError: String?
    var archiveAnalyticsPending = false
    var archiveExportStatus = "NDJSON snapshot ещё не выгружен"
    var archiveExportPending = false
    var workflowErrors: [String] = []
    var detectionInterval = "1"
    var detectionStreamID = ""
    var detectionThreshold = "-70"
    var detectionDedupBandwidth = "100000"
    var detectionCooldown = "60"
    var detectionMaximumCandidates = "10"
    var handoffInterval = "1"
    var handoffMinimumPriority = "0"
    var handoffReceiverStreamID = ""
    var handoffMaximumFailures = "3"
    var handoffRetryDelay = "30"
    var completionInterval = "1"
    var completionMinimumListening = "0"
    var completionMinimumObservations = "1"
    var completionReceiverStreamID = ""
    var tinySAPort = "/dev/ttyACM0"
    var receiverPort = ""
    var showSerialForm = false

    let sweepAverage = SweepAverageModel()
    let threeSweepThresholds = ThreeSweepThresholdsModel()
    let sweepContext = SweepContextModel()
    let spectralMask = SpectralMaskModel()
    let sweepQuality = SweepQualityModel()
    let frequencyRounding = FrequencyRoundingModel()

    let frequencyRaster = FrequencyRasterModel()
    let measurementResults = MeasurementResultsModel()
    let frequencyCounter = FrequencyCounterModel()
    let fieldStrength = FieldStrengthModel()
    let dvbEvaluation = DvbEvaluationModel()
    let phonogram = PhonogramModel()
    let signalList = SignalListModel()
    let measurementMap = MeasurementMapModel()
    let audioStream = AudioStreamModel()
    let sweepThreshold: SweepThresholdModel

    private let makeClient: (String) throws -> PlatformClient
    private let onArchiveContextReset: @MainActor (Int64?) -> Void
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
    private let requireRuntimeOwner: Bool
    private(set) var verifiedRuntimeOwner: String?
    var connectionGeneration = 0
    private var capacityRequestRevision = 0
    var client: PlatformClient?
    private var pollTask: Task<Void, Never>?
    private let measurementStream = RemoteMeasurementStream()
    private let makeSpectrumFrame: @Sendable ([PlatformSweep], Double) throws -> NativeSpectrumFrame
    private var streamingEnabled = false
    private var negotiatedMeasurementTransport: PlatformMeasurementTransport?
    private var measurementSnapshot: PlatformSnapshot?
    private var archiveHighWatermark: Int64?
    private var archiveCacheVerified = false
    @ObservationIgnored private var spectrumRenderRevision = 0
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
        }
        if let serial = ports.first(where: { $0 != tinySAPort && $0.contains("usbserial") }) {
            receiverPort = serial
        } else if receiverPort == "/dev/ttyUSB0" {
            receiverPort = ""
        }
    }

    init(
        makeClient: @escaping (String) throws -> PlatformClient = { try PlatformClient(baseURL: $0) },
        comparisonTimeoutNanoseconds: UInt64 = 6_000_000_000,
        thresholdTimeoutNanoseconds: UInt64 = 6_000_000_000,
        requireRuntimeOwner: Bool = false,
        makeSpectrumFrame: @escaping @Sendable ([PlatformSweep], Double) throws -> NativeSpectrumFrame = {
            try NativeSpectrumFrame(sweeps: $0, threshold: $1)
        },
        onArchiveContextReset: @escaping @MainActor (Int64?) -> Void = { _ in }
    ) {
        self.requireRuntimeOwner = requireRuntimeOwner
        self.makeClient = makeClient
        self.makeSpectrumFrame = makeSpectrumFrame
        self.onArchiveContextReset = onArchiveContextReset
        self.sweepThreshold = SweepThresholdModel(timeoutNanoseconds: thresholdTimeoutNanoseconds)
        self.comparisonTimeoutNanoseconds = comparisonTimeoutNanoseconds
        refreshSerialPorts()
    }

    var displayCurrent: [PlotPoint] { current.isEmpty && measurementsStale ? staleSpectrum?.current ?? current : current }
    var displayMinimum: [PlotPoint] { current.isEmpty && measurementsStale ? staleSpectrum?.minimum ?? minimum : minimum }
    var displayMaximum: [PlotPoint] { current.isEmpty && measurementsStale ? staleSpectrum?.maximum ?? maximum : maximum }
    var displayWaterfall: WaterfallHistory { current.isEmpty && measurementsStale ? staleSpectrum?.waterfall ?? waterfall : waterfall }
    var displayWaterfallRevision: Int { waterfallRevision }
    var displaySpectrumScale: PlatformSpectrumScale? { current.isEmpty && measurementsStale ? staleSpectrumScale ?? spectrumPresentation.scale : spectrumPresentation.scale }
    var displayInspectorSweeps: [PlatformSweep] {
        spectrumPresentation.sweeps.isEmpty && measurementsStale && staleSpectrum != nil
            ? lastRenderedSweeps : spectrumPresentation.sweeps
    }
    var displayArchiveCapacity: PlatformArchiveCapacity? { coreReachable ? archiveCapacity : nil }
    var acquisitionSweepTime: Date? {
        analyzer == nil ? nil : lastMeasurementAt
    }

    var analyzer: PlatformStream? { session?.streams.first(where: { $0.isDbmAnalyzer }) }
    var receiver: PlatformStream? { session?.streams.first(where: { $0.isReceiver }) }
    var thresholdDbm: Double? { parseUserNumber(threshold) }
    var usesBundledLegacyHelper: Bool {
        guard !NativeLocalService.isPersonalRC else { return false }
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

    @discardableResult
    func observeVerifiedRuntimeOwner(_ owner: String?) -> Bool {
        guard owner != verifiedRuntimeOwner else { return false }
        disconnect()
        verifiedRuntimeOwner = owner
        return true
    }

    func connect(startPolling: Bool = true, using preparedClient: PlatformClient? = nil) {
        if requireRuntimeOwner {
            guard let verifiedRuntimeOwner, preparedClient?.baseURL.runtimeInstanceID == verifiedRuntimeOwner else {
                errorText = "Требуется подключение к подтверждённому владельцу RC-службы."
                return
            }
        }
        coreReachable = false; lastCoreContactAt = nil
        resetMeasurementHealth()
        archiveHighWatermark = nil; archiveCacheVerified = false
        measurementStream.stop(); measurementSnapshot = nil; eventGapNotice = nil
        negotiatedMeasurementTransport = nil; measurementSource = nil; measurementDisplayIssue = nil
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
            status = "Проверка соединения с ядром…"
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
        coreReachable = false; lastCoreContactAt = nil
        archiveHighWatermark = nil; archiveCacheVerified = false
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
        negotiatedMeasurementTransport = nil; measurementSource = nil; measurementDisplayIssue = nil
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
            recordCoreContact()
            sessions = loaded
            if selectedSessionID.isEmpty || !loaded.contains(where: { PlatformExactID.same($0.id, selectedSessionID) }) {
                selectedSessionID = loaded.first?.id ?? ""
                clearMeasurements()
            }
            await refresh()
        } catch {
            guard requestGeneration == generation, self.client === client else { return }
            if expireAuthorization(error, client: client) { return }
            staleSpectrum = lastRenderedFrame ?? staleSpectrum
            staleSpectrumScale = spectrumPresentation.scale ?? staleSpectrumScale
            resetComparisonScope()
            markCoreUnavailable()
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

    private func invalidateArchiveEvidence(watermark: Int64?, preservingReplay: Bool = false) {
        generation += 1; connectionGeneration += 1; capacityRequestRevision += 1
        archiveCapacity = nil; archiveCapacityError = nil; busy = false
        if preservingReplay { measurementStream.suspend() }
        clearMeasurements(preservingReplay: preservingReplay)
        configureAuxiliaryStreams(client: nil, session: nil)
        if let watermark {
            archiveHighWatermark = watermark
            // A possible write in the old archive cannot confirm evidence in its replacement.
            uncertainReferenceScopes.removeAll(); referenceNoticeScope = nil; referenceWriteNotice = nil
        }
        onArchiveContextReset(watermark)
    }

    /// Capture the applied cursor before awaiting health: a newer event can be
    /// applied while that HTTP response is in flight without implying rollback.
    private func observeArchiveWatermark(_ watermark: Int64, appliedBeforeHealth: UInt64) -> Bool {
        let rolledBack = (archiveHighWatermark.map { watermark < $0 } ?? false)
            || UInt64(watermark) < appliedBeforeHealth
        archiveHighWatermark = watermark
        if rolledBack { invalidateArchiveEvidence(watermark: watermark) }
        archiveCacheVerified = true
        return rolledBack
    }

    private func refreshSession(_ client: PlatformClient) async {
        guard self.client === client else { return }
        if selectedSessionID.isEmpty {
            let requestGeneration = generation
            do {
                _ = try await client.listSessions()
                guard requestGeneration == generation, self.client === client else { return }
                recordCoreContact()
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                if expireAuthorization(error, client: client) { return }
                markCoreUnavailable(); errorText = describeSession(error)
            }
            await refreshTaskScheduler(client)
            return
        }
        sessionRefreshRevision += 1
        let refreshRevision = sessionRefreshRevision
        let id = selectedSessionID
        var requestGeneration = generation
        let schedulerRevision = taskSchedulerCommandRevision
        do {
            if streamingEnabled, negotiatedMeasurementTransport == nil {
                let offered = try await client.measurementTransport()
                let transport = try NativeAcceptanceTransport.resolve(NativeAcceptanceTrace.shared?.profile.transport,offered:offered)
                guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client else { return }
                negotiatedMeasurementTransport = transport
            }
            let retained = streamingEnabled ? measurementStream.measurements : nil
            // Seed before a full snapshot so commits during its load remain replayable.
            var watermark: Int64?
            if retained == nil {
                let applied = measurementStream.appliedSourceCursor
                do { watermark = try await client.eventWatermark() }
                catch {
                    if archiveHighWatermark != nil { throw error }
                    watermark = nil // Preserve legacy REST peers without event health.
                }
                guard refreshRevision == sessionRefreshRevision, requestGeneration == generation,
                      self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                if let watermark, observeArchiveWatermark(watermark, appliedBeforeHealth: applied) {
                    requestGeneration = generation
                }
            }
            var snapshot = try await client.snapshot(sessionID: id, includeArchiveCapacity: false, includeMeasurements: retained == nil)
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client,
                  PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            var latestCache = streamingEnabled ? measurementStream.measurements : nil
            if latestCache != nil, !measurementStream.matches(sessionID: id,
                streamID: snapshot.session.streams.first(where: { $0.isDbmAnalyzer })?.id ?? "",
                receiverStreamID: snapshot.session.streams.first(where: { $0.isReceiver })?.id) {
                measurementStream.stop()
                await refreshSession(client)
                return
            }
            if latestCache != nil {
                let applied = measurementStream.appliedSourceCursor
                let currentWatermark = try await client.eventWatermark()
                guard refreshRevision == sessionRefreshRevision, requestGeneration == generation,
                      self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                if observeArchiveWatermark(currentWatermark, appliedBeforeHealth: applied) {
                    requestGeneration = generation
                    watermark = currentWatermark
                    // The metadata-only response belongs to the previous archive context.
                    snapshot = try await client.snapshot(sessionID: id, includeArchiveCapacity: false, includeMeasurements: true)
                    guard refreshRevision == sessionRefreshRevision, requestGeneration == generation,
                          self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                    latestCache = nil
                } else {
                    latestCache = measurementStream.measurements
                    // Ordinary recovery retains the applied cursor, never the newer health mark.
                    watermark = latestCache?.cursor
                }
            }
            // The stream may already be preparing a newer commit than its last
            // acknowledged cache. A metadata poll must not render the older cache
            // over that work or invalidate its render revision.
            let streamOwnsMeasurements = latestCache != nil && measurementStream.isActive && measurementSnapshot != nil
            if streamOwnsMeasurements, let pending = measurementSnapshot {
                snapshot.sweeps = pending.sweeps
                snapshot.observations = pending.observations
            } else if let cached = latestCache {
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
            if !streamOwnsMeasurements {
                guard await applyMeasurements(snapshot) else { return }
            }
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client,
                  PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            recordCoreContact()
            observeMeasurementTime(snapshot)
            applyWorkflow(snapshot, schedulerRevision: schedulerRevision)
            if streamingEnabled, !measurementStream.isActive, let watermark {
                let streamGeneration = requestGeneration
                let transport = negotiatedMeasurementTransport ?? .serverSentEvents
                eventStatus = "\(transport.label) · ожидание событий после \(measurementStream.appliedSourceCursor > 0 ? String(measurementStream.appliedSourceCursor) : String(watermark))"
                measurementStream.start(client: client, sessionID: id, streamID: analyzer?.id ?? "", receiverStreamID: receiver?.id, watermark: watermark,
                    transport: transport,
                    sweeps: snapshot.sweeps, observation: snapshot.observations.last,
                    receive: { [weak self] sweeps, observation, event, change in
                        guard let self, streamGeneration == self.generation, self.client === client,
                              PlatformThreeSweepThresholds.sameID(id, self.selectedSessionID), var latest = self.measurementSnapshot else { throw CancellationError() }
                        if change == .measurement {
                            latest.sweeps = sweeps; latest.observations = observation.map { [$0] } ?? []
                            self.measurementSnapshot = latest
                            if event.committedEvent?.payload.sweep != nil {
                                guard await self.applyMeasurements(latest) else { throw CancellationError() }
                            } else if !PlatformExactID.same(self.latestObservation?.id, observation?.id) {
                                self.latestObservation = observation
                            }
                        }
                        guard !Task.isCancelled, streamGeneration == self.generation, self.client === client else { throw CancellationError() }
                        self.recordCoreContact()
                        if change == .measurement,
                           self.analyzer == nil || event.committedEvent?.payload.sweep != nil {
                            self.observeMeasurementTime(latest)
                        }
                        self.measurementSource = event.source; self.measurementDisplayIssue = event.displayIssue
                        self.eventStatus = "\(transport.label) · принято событие \(event.sequence)" + (event.displayIssue == nil ? "" : " · исходник")
                        if change == .measurement { self.live = "Закоммичено ядром · \(sweeps.last?.interval.endedAt ?? observation?.interval.endedAt ?? "—")" }
                    }, recover: { [weak self] reason in
                        guard let self, streamGeneration == self.generation, self.client === client else { return }
                        if self.expireAuthorization(reason, client: client) { return }
                        self.markCoreUnavailable()
                        self.eventStatus = "\(transport.label) · восстановление потока: \(describeSession(reason))"
                        if let eventError = reason as? PlatformEventError {
                            self.eventGapNotice = "Пропуск доставки в этот UI. Отображение восстанавливается из архива; охрана продолжает работать."
                            if eventError == .consumerOverrun {
                                self.eventGapNotice = "Буфер отображения переполнен. Продолжение из архива после последнего принятого события; охрана продолжает работать."
                            }
                            // Bound retries for a permanently malformed peer.
                            try? await Task.sleep(nanoseconds: 250_000_000)
                            guard !Task.isCancelled, streamGeneration == self.generation, self.client === client,
                                  PlatformThreeSweepThresholds.sameID(id, self.selectedSessionID) else { return }
                            await self.refresh()
                        } else if transport == .serverSentEvents, case PlatformError.http(status: 404, code: _, message: _) = reason {
                            self.streamingEnabled = false
                            self.measurementStream.stop()
                        }
                    })
            } else if !streamingEnabled || watermark == nil && !measurementStream.isActive {
                eventStatus = "REST · совместимый опрос (SSE не подтверждён)"
            }
            errorText = snapshot.workflowErrors.isEmpty ? nil : snapshot.workflowErrors.joined(separator: " · ")
            live = measurementsStale ? "Последние измерения устарели или ещё не получены" : "Получены свежие измерения ядра"
        } catch {
            guard refreshRevision == sessionRefreshRevision, requestGeneration == generation, self.client === client, PlatformThreeSweepThresholds.sameID(id, selectedSessionID) else { return }
            if expireAuthorization(error, client: client) { return }
            if case PlatformError.http(status: 404, code: "session_not_found", message: _) = error,
               let loaded = try? await client.listSessions() {
                guard refreshRevision == sessionRefreshRevision, requestGeneration == generation,
                      self.client === client, PlatformExactID.same(id, selectedSessionID) else { return }
                if !loaded.contains(where: { PlatformExactID.same($0.id, id) }) {
                    recordCoreContact()
                    sessions = loaded
                    errorText = nil
                    selectSession(loaded.first?.id ?? "")
                    return
                }
            }
            let retainedFrame = lastRenderedFrame ?? staleSpectrum
            let retainedSweeps = lastRenderedSweeps
            let retainedScale = spectrumPresentation.scale ?? staleSpectrumScale
            let retainedMeasurementAt = lastMeasurementAt
            let hasReplay = measurementStream.measurements != nil
            if hasReplay || archiveHighWatermark != nil {
                invalidateArchiveEvidence(watermark: nil, preservingReplay: hasReplay)
            }
            eventStatus = "Поток не подтверждён: \(describeSession(error))"
            sweepAverage.reset()
            threeSweepThresholds.reset()
            sweepContext.reset()
            sweepQuality.reset()
            frequencyRounding.unavailable()
            clearSpectrumPresentation()
            staleSpectrum = retainedFrame; staleSpectrumScale = retainedScale
            lastRenderedSweeps = retainedSweeps
            lastMeasurementAt = retainedMeasurementAt
            markCoreUnavailable()
            errorText = describeSession(error)
        }
    }

    func followFrequency(_ hz: Int64) {
        historyFrequency = hz; showFrequencyHistory = true
    }

    private func resetMeasurementHealth() {
        historyFrequency = nil
        reviewedSweep = nil
        reviewedSweepRequest = UUID()
        if staleSpectrum != nil { waterfallRevision += 1 }
        measurementsStale = true; lastMeasurementAt = nil
        staleSpectrum = nil; staleSpectrumScale = nil; lastRenderedFrame = nil
        lastRenderedSweeps = []
    }

    private func recordCoreContact() {
        coreReachable = true; lastCoreContactAt = Date()
        status = "Ядро доступно; приборные порты остаются у ядра"
        if let lastMeasurementAt, Date().timeIntervalSince(lastMeasurementAt) > Self.measurementFreshnessInterval {
            measurementsStale = true
        }
    }

    private func markCoreUnavailable() {
        coreReachable = false; measurementsStale = true
        status = "Связь с ядром не подтверждена"
        live = "Последние измерения устарели; ожидается восстановление связи"
    }

    private func observeMeasurementTime(_ snapshot: PlatformSnapshot) {
        // The spectrum's freshness belongs to its analyzer, never to a faster
        // receiver sharing the session. Receiver-only sessions use their own stream.
        let times: [String]
        if let analyzer {
            times = snapshot.sweeps.filter {
                PlatformExactID.same($0.sessionId, selectedSessionID) && PlatformExactID.same($0.streamId, analyzer.id)
            }.map { $0.interval.endedAt }
        } else {
            times = snapshot.observations.filter {
                PlatformExactID.same($0.sessionId, selectedSessionID) && PlatformExactID.same($0.streamId, receiver?.id ?? "")
            }.map { $0.interval.endedAt }
        }
        guard let newest = times.compactMap(NativeSpectrumTimeline.date).max() else { measurementsStale = true; return }
        guard lastMeasurementAt.map({ newest > $0 }) ?? true else { return }
        lastMeasurementAt = newest
        let age = Date().timeIntervalSince(newest)
        measurementsStale = age < -1 || age > Self.measurementFreshnessInterval
        staleSpectrum = nil; staleSpectrumScale = nil
    }

    private func expireAuthorization(_ error: Error, client: PlatformClient) -> Bool {
        guard client.hasBearerCredential, case PlatformError.http(status: 401, code: _, message: _) = error else { return false }
        disconnect()
        authorizationRequired = true
        errorText = "Доступ этого Mac отозван или истёк. Выполните сопряжение заново; охрана ядра не изменена."
        return true
    }

    /// Operator-selected exact original; no JSON reconstruction or Core mutation.
    func exportMeasurementSource() {
        guard let source = measurementSource else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [source.encoding == .protobuf ? .data : .json]
        panel.nameFieldStringValue = source.suggestedFilename
        panel.title = "Сохранить исходное событие без преобразований"
        let requestGeneration = generation
        panel.begin { [weak self] result in
            guard result == .OK, let destination = panel.url else { return }
            Task { @MainActor [weak self] in
                do {
                    try await Task.detached(priority: .utility) { try source.originalData.write(to: destination, options: .atomic) }.value
                    guard let self, requestGeneration == self.generation else { return }
                    self.eventStatus = "Исходное событие \(source.sequence) сохранено без преобразований"
                } catch {
                    guard let self, requestGeneration == self.generation else { return }
                    self.errorText = "Не удалось сохранить исходное событие: \(error.localizedDescription)"
                }
            }
        }
    }

    func startSession() { performSessionAction { try await $0.startSession($1) } }
    func stopSession() { performSessionAction { try await $0.stopSession($1) } }
    func startWorkers() { performSessionAction { try await $0.startWorkers($1) } }
    func stopWorkers(mode: PlatformStopMode = .graceful) { performSessionAction { try await $0.stopWorkers($1, mode: mode) } }

    func startStream(_ stream: PlatformStream?) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.startStream(sessionID: $1, streamID: stream.id) }
    }

    func stopStream(_ stream: PlatformStream?, mode: PlatformStopMode = .graceful) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.stopStream(sessionID: $1, streamID: stream.id, mode: mode) }
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

    private func clearMeasurements(preservingReplay: Bool = false) {
        spectrumViewMemory.snapshot = nil
        resetMeasurementHealth()
        if !preservingReplay { measurementStream.stop() }
        archiveCacheVerified = false
        measurementSnapshot = nil
        measurementSource = nil; measurementDisplayIssue = nil
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

    func setSpectrumPaused(_ paused: Bool) {
        if spectrumPresentation.paused != paused { toggleSpectrumPause() }
    }

    func toggleSpectrumPause() {
        if archiveCacheVerified, spectrumPresentation.paused, let cache = measurementStream.measurements, let stream = analyzer?.id,
           PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL) {
            spectrumPresentation.update(scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                                        sweeps: scanBandSweeps(in: cache.sweeps))
            if spectrumPresentation.paused { spectrumPresentation.togglePause() }
        } else { spectrumPresentation.togglePause() }
        renderSpectrumPresentation()
    }
    func fitSpectrum() { spectrumRenderRevision += 1; spectrumPresentation.fit(); spectrumClippedCount = spectrumPresentation.clippedCount }
    func applySpectrumBounds() { spectrumRenderRevision += 1; spectrumPresentation.setBounds(lower: spectrumLower, upper: spectrumUpper); spectrumClippedCount = spectrumPresentation.clippedCount }
    func clearSpectrumPresentation() { spectrumPresentation.reset(); renderSpectrumPresentation() }

    private static func scanContext(_ sweep: PlatformSweep) -> (band: String, plan: String)? {
        guard let bandValue = sweep.settings?["scan_band_id"], case .string(let band) = bandValue,
              let planValue = sweep.settings?["scan_plan_id"], case .string(let plan) = planValue,
              !band.isEmpty, band.utf8.count <= 64, !plan.isEmpty, plan.utf8.count <= 800 else { return nil }
        return (band, plan)
    }
    var availableScanBandIDs: [String] {
        Array(Set((measurementSnapshot?.sweeps ?? []).compactMap { Self.scanContext($0)?.band })).sorted()
    }
    func selectScanBand(_ id: String) {
        guard !PlatformExactID.same(id, selectedScanBand) else { return }
        selectedScanBand = id
        lastRenderedFrame = nil
        lastRenderedSweeps = []
        clearSpectrumPresentation()
        let sourceGeneration = generation, sourceClient = client
        Task { [weak self] in
            guard let self, self.generation == sourceGeneration, self.client === sourceClient,
                  PlatformExactID.same(self.selectedScanBand, id), let snapshot = self.measurementSnapshot else { return }
            _ = await self.applyMeasurements(snapshot)
        }
    }
    private func renderSpectrumPresentation() {
        spectrumRenderRevision += 1
        spectrumClippedCount = spectrumPresentation.clippedCount
        if let frame = try? NativeSpectrumFrame(sweeps: spectrumPresentation.sweeps, threshold: thresholdDbm ?? -80) { publish(frame) }
    }
    private func prepareSpectrum(scope: PlatformSpectrumPresentation.Scope?, incoming: [PlatformSweep]) async -> Bool {
        if selectedScanBand.isEmpty { selectedScanBand = incoming.compactMap { Self.scanContext($0)?.band }.first ?? "" }
        let visible = scanBandSweeps(in: incoming)
        spectrumRenderRevision += 1
        let revision = spectrumRenderRevision, previous = spectrumPresentation, threshold = thresholdDbm ?? -80
        let makeFrame = makeSpectrumFrame
        let trace=NativeAcceptanceTrace.shared,sourceID=visible.last?.id,measure=trace != nil
        if let sourceID {trace?.sourceStage("preparation.queued",id:sourceID)}
        let work = Task.detached(priority: .userInitiated) { () throws -> (PlatformSpectrumPresentation, NativeSpectrumFrame?, Int, UInt64, UInt64) in
            let started=measure ? NativeAcceptanceClock.now():0
            var next = previous
            if let scope { next.update(scope: scope, sweeps: visible) } else { next.reset() }
            let frame = next.paused ? nil : try makeFrame(next.sweeps, threshold)
            return (next, frame, next.clippedCount, started, measure ? NativeAcceptanceClock.now():0)
        }
        let result = try? await withTaskCancellationHandler(operation: { try await work.value }, onCancel: { work.cancel() })
        guard !Task.isCancelled, revision == spectrumRenderRevision, let (next, frame, clipped, started, finished) = result else { return false }
        if let sourceID {
            trace?.sourceStage("preparation.begin",id:sourceID,at:started)
            trace?.sourceStage("prepared",id:sourceID,at:finished)
            trace?.sourceStage("model.resumed",id:sourceID)
        }
        spectrumPresentation = next; spectrumClippedCount = clipped
        if let frame { publish(frame) }
        return true
    }

    private func scanBandSweeps(in sweeps: [PlatformSweep]) -> [PlatformSweep] {
        guard !selectedScanBand.isEmpty else { return sweeps }
        let selected = sweeps.filter { PlatformExactID.same(Self.scanContext($0)?.band, selectedScanBand) }
        guard let newestPlan = selected.last.flatMap(Self.scanContext)?.plan else { return selected }
        return selected.filter { PlatformExactID.same(Self.scanContext($0)?.plan, newestPlan) }
    }

    private func publish(_ frame: NativeSpectrumFrame) {
        if !frame.current.isEmpty {
            lastRenderedFrame = frame
            lastRenderedSweeps = spectrumPresentation.sweeps
        }
        current = frame.current; minimum = frame.minimum; maximum = frame.maximum; rows = frame.rows
        let history = frame.waterfall
        if waterfall.grid != history.grid || waterfall.rows != history.rows || waterfall.capacity != history.capacity {
            waterfall = history; waterfallRevision += 1
        }
    }


}
