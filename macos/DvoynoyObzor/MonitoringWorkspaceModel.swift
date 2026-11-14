import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy
import Darwin

protocol MonitoringAudioExportWriting: Sendable {
    func stage(_ bytes: Data, for destination: URL) async throws -> MonitoringAudioExportStage
}

final class MonitoringAudioExportStage: Sendable {
    let temporaryDirectory: URL
    private let file: URL
    fileprivate init(directory: URL, file: URL) { temporaryDirectory = directory; self.file = file }
    func discard() { try? FileManager.default.removeItem(at: temporaryDirectory) }
    deinit { discard() }
    // The private staging directory is on the destination volume. Publication
    // is a single rename after the MainActor revalidates the original request.
    func publish(to destination: URL) throws {
        guard Darwin.rename(file.path, destination.path) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}

struct NativeAudioExportWriter: MonitoringAudioExportWriting {
    func stage(_ bytes: Data, for destination: URL) async throws -> MonitoringAudioExportStage {
        try await Task.detached(priority: .utility) {
            let directory = try FileManager.default.url(for: .itemReplacementDirectory, in: .userDomainMask, appropriateFor: destination.deletingLastPathComponent(), create: true)
            do {
                try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
                let file = directory.appendingPathComponent("audio.wav")
                try bytes.write(to: file, options: .atomic)
                return MonitoringAudioExportStage(directory: directory, file: file)
            } catch {
                try? FileManager.default.removeItem(at: directory)
                throw error
            }
        }.value
    }
}

struct MonitoringAudioRange {
    let projectID: String, eventID: String, artifactID: String, sourceSHA256: String
    let start: Int64, end: Int64
}

@MainActor
final class MonitoringWorkspaceModel: ObservableObject {
    private let audioExportWriter: any MonitoringAudioExportWriting
    init(audioExportWriter: any MonitoringAudioExportWriting = NativeAudioExportWriter()) {
        self.audioExportWriter = audioExportWriter
    }
    @Published var projects: [MonitoringValue] = []
    @Published private(set) var projectPages = MonitoringProjectPageState()
    @Published var selected = "" {
        didSet { if !PlatformExactID.same(oldValue, selected) { projectPages.select(selected); invalidateProject() } }
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
    @Published var baselineConfirmed = false
    @Published var scanPlansAvailable = false
    @Published var audioEvidenceAvailable = false
    @Published var audioEvidenceInfo: MonitoringValue = .null
    @Published var audioMarks: [MonitoringValue] = []
    @Published var audioMarksNext = ""
    @Published var details = "Выберите подготовленный сеанс."
    @Published var notice = ""
    @Published var error = ""
    @Published var audioNotice = ""
    @Published var pending = false
    @Published var hasPlayback = false
    @Published var audioLoading = false
    @Published var leases: [String: MonitoringManualLease] = [:]
    @Published var researchPending: Set<String> = []
    @Published var researchError = ""
    @Published var search: MonitoringValue = .null
    @Published var coverage = ""
    @Published var history: [MonitoringHistoryRow] = []
    @Published var historyNext = ""
    @Published var historyDescription = "История ещё не загружена."
    @Published var selectedSweepID = ""
    @Published var rawSweep: MonitoringHistoryRow?
    @Published var rawDetail: MonitoringValue = .null
    @Published var baselineComparison: MonitoringValue = .null
    @Published var sources: [MonitoringValue] = []
    @Published var sourcesNext = ""
    @Published var antennaNotes = ""
    @Published var receivePathConfirmed = false
    @Published var locationConfirmed = false
    @Published var detectorMargin = "6"
    @Published var detectorImpulse = "12"
    @Published var detectorHysteresis = "3"
    @Published var detectorConfirmations = "3"
    @Published var detectorWindow = "5"
    @Published var detectorDwell = "10"
    private var preparationSeeded = false
    @Published var operationsSettings: MonitoringValue = .null
    @Published var storage: MonitoringValue = .null
    @Published var alerts: [MonitoringValue] = []
    @Published var alertsNext = ""
    @Published var alertsLatest = true
    @Published var operationsPending: Set<String> = []
    @Published var operationsErrors: [String: String] = [:]
    @Published var operationsNotice = ""
    @Published var autostart = false
    @Published var archiveReserve = "268435456"
    @Published var archiveRetention = "3600"
    @Published var webhookEnabled = false
    @Published var webhookURL = ""
    @Published var webhookSecret = ""
    @Published var localSoundEnabled = false
    private var operationsRequests: [String: UUID] = [:]
    private var operationsRevision = 0
    private var alertsPageAfter: String?
    private var soundState = MonitoringAlertSoundState()
    private var researchRequests: [String: UUID] = [:]
    private var historyQuery = MonitoringHistoryQuery()
    var selectedSweep: MonitoringHistoryRow? { rawSweep ?? history.first { PlatformExactID.same($0.id, selectedSweepID) } }
    private var player: AVAudioPlayer?
    private var client: PlatformClient?
    private var scope = ""
    private var revision = 0
    private var requestID = UUID()
    private var playbackRevision = 0
    private var archiveHighWatermark: Int64?
    private var archiveHealthRevision = 0
    let receiverModes = ["AUTO", "FM", "AM", "DSTAR", "YSF", "ALINCO", "NXDN", "P25", "DPMR", "DMR"]

    var project: MonitoringValue { status["project"] }
    var devices: [MonitoringValue] { audioInputs["devices"].items }
    var chosenDevice: MonitoringValue? { devices.first { $0["index"].scalar == selectedInput } }
    var isArmed: Bool { status["active"].bool == true || ["armed", "searching"].contains(project["state"].text) }
    var canConfigure: Bool { !selected.isEmpty && status != .null && !isArmed && !pending && !researchPending.contains("control") && !operationsPending.contains("preparation") && !operationsPending.contains("settings") }
    var configurationDirty: Bool {
        let savedMode = project["receiver_mode"].text.isEmpty ? "AUTO" : project["receiver_mode"].text
        if savedMode != receiverMode || (project["audio"] != .null) != audioEnabled { return true }
        return audioEnabled && (chosenDevice?["index"] != project["audio"]["device"] || chosenDevice?["name"] != project["audio"]["name"])
    }
    var canSaveConfiguration: Bool {
        canConfigure && (!audioEnabled || (audioInputs["available"].bool == true && chosenDevice != nil && audioConfirmed))
    }
    var canArm: Bool { canConfigure && !configurationDirty && !project["baseline_id"].text.isEmpty && (preparationSimulation || (savedPreparationReady && !preparationDirty)) }
    var isCollecting: Bool { project["state"].text == "collecting" || project["coverage"].text == "preparation" }
    var canEditPreparation: Bool { canConfigure && !isCollecting }
    var preparationSimulation: Bool {
        project["profile"]["analyzer"]["settings"]["adapter"].text == "tinysa-sim" &&
        project["profile"]["receiver"]["settings"]["adapter"].text == "ardv1-sim"
    }
    var savedPreparationReady: Bool {
        project["preparation"]["receive_path_confirmed"].bool == true && project["preparation"]["location_confirmed"].bool == true &&
        !project["preparation"]["antenna_notes"].text.isEmpty
    }
    var preparationInputValid: Bool {
        let notes = antennaNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        return !notes.isEmpty && notes.unicodeScalars.count <= 1000
    }
    var preparationDirty: Bool {
        antennaNotes.trimmingCharacters(in: .whitespacesAndNewlines) != project["preparation"]["antenna_notes"].text ||
        receivePathConfirmed != (project["preparation"]["receive_path_confirmed"].bool == true) ||
        locationConfirmed != (project["preparation"]["location_confirmed"].bool == true)
    }
    var canStartPreparation: Bool {
        canEditPreparation && (preparationSimulation || (savedPreparationReady && !preparationDirty))
    }
    var canPlay: Bool { !pending && !selectedEvent.isEmpty && event["analysis"]["audio"] != .null }

    /// Capture the UI's project before an asynchronous button action can begin.
    func perform(_ action: @escaping @MainActor () async -> Void) {
        let generation = revision
        Task { [weak self] in
            guard let self, self.revision == generation else { return }
            await action()
        }
    }

    func configure(_ server: String, client suppliedClient: PlatformClient?) {
        let next = server
        guard !PlatformExactID.same(next, scope) || client !== suppliedClient else { return }
        archiveHighWatermark = nil; archiveHealthRevision += 1
        scope = next; selected = ""; projects = []; projectPages = MonitoringProjectPageState(); audioInputs = .null
        invalidateProject()
        client = server.isEmpty ? nil : suppliedClient
        scanPlansAvailable = false
        audioEvidenceAvailable = false
    }

    func loadScanPlanCapabilities() async {
        guard let client else { return }
        let scope = self.scope
        let value = try? await client.coreCapabilities()
        guard self.client === client, PlatformExactID.same(scope, self.scope), !Task.isCancelled else { return }
        scanPlansAvailable = value?["features"]["monitoring_scan_plans"].bool == true
        audioEvidenceAvailable = value?["features"]["audio_evidence_export"].bool == true
    }

    /// Synchronous notification from the live view, including a failed current-health check.
    /// A nil mark is unverified, so preserve the previous mark for later rollback detection.
    func resetArchiveContext(watermark: Int64?) {
        archiveHealthRevision += 1
        if let watermark { archiveHighWatermark = watermark }
        invalidateProject()
    }

    private func invalidateProject() {
        revision += 1; requestID = UUID(); pending = false
        status = .null; preflight = .null; events = []; selectedEvent = ""; event = .null
        receiverMode = "AUTO"; audioEnabled = false; selectedInput = ""; audioConfirmed = false
        baselineConfirmed = false
        details = "Выберите проект и обновите состояние."; error = ""; notice = ""
        leases = [:]; researchPending = []; researchRequests = [:]; researchError = ""
        search = .null; coverage = ""; history = []; historyNext = ""; selectedSweepID = ""; rawSweep = nil
        rawDetail = .null; baselineComparison = .null; sources = []; sourcesNext = ""
        historyDescription = "История ещё не загружена."
        historyQuery = MonitoringHistoryQuery()
        stopOperations()
        antennaNotes = ""; receivePathConfirmed = false; locationConfirmed = false; preparationSeeded = false
        detectorMargin = "6"; detectorImpulse = "12"; detectorHysteresis = "3"
        detectorConfirmations = "3"; detectorWindow = "5"; detectorDwell = "10"
        operationsSettings = .null; storage = .null; alerts = []; alertsNext = ""; alertsLatest = true
        alertsPageAfter = nil; soundState = MonitoringAlertSoundState(); operationsErrors = [:]; operationsNotice = ""
        autostart = false; archiveReserve = "268435456"; archiveRetention = "3600"
        webhookEnabled = false; webhookURL = ""; webhookSecret = ""; localSoundEnabled = false
        stopAudio()
        resetAudioEvidence()
    }

    private func resetAudioEvidence() {
        audioEvidenceInfo = .null; audioMarks = []; audioMarksNext = ""
        for key in ["audio-evidence", "audio-mark", "audio-export"] {
            researchRequests[key] = nil; researchPending.remove(key)
        }
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
            guard current == revision, requestID == token, PlatformExactID.same(selected, identifier),
                  eventID == nil || PlatformExactID.same(selectedEvent, eventID), !Task.isCancelled else { return false }
            details = result.pretty
            apply(result)
            return true
        } catch {
            if current == revision, requestID == token, PlatformExactID.same(selected, identifier), !Task.isCancelled {
                self.error = error.localizedDescription
            }
            return false
        }
    }

    func load() async {
        await reloadProjects()
    }

    func reloadProjects() async {
        await loadProjectPage(0, refreshSelection: true)
    }

    func nextProjects() async {
        guard !projectPages.nextCursor.isEmpty else { return }
        await loadProjectPage(projectPages.pageIndex + 1)
    }

    func previousProjects() async {
        guard projectPages.pageIndex > 0 else { return }
        await loadProjectPage(projectPages.pageIndex - 1)
    }

    private func loadProjectPage(_ page: Int, refreshSelection: Bool = false) async {
        let after = projectPages.cursor(for: page)
        guard page == 0 || after != nil else { return }
        let previous = selected
        let loaded = await run(request: { client, _ in try await client.monitoringProjects(after: after) }) { result in
            projectPages.apply(result, page: page)
            projects = projectPages.items
            selected = projectPages.selectedID
        }
        if loaded, !selected.isEmpty, (refreshSelection || previous != selected), !Task.isCancelled {
            await refreshStatus(syncConfiguration: true)
        }
    }

    func createProject(name: String, site: String, sessionID: String) async {
        let created = await run(request: { client, _ in
            try await client.monitoring(body: ["name": .string(name), "site": .string(site), "session_id": .string(sessionID)])
        }) { result in
            projectPages.created(result); projects = projectPages.items; selected = projectPages.selectedID
            notice = "Проект создан. Приборы запускаются только кнопкой постановки на охрану."
        }
        if created, !Task.isCancelled { await refreshStatus(syncConfiguration: true) }
    }

    func adoptCreatedProject(_ project: MonitoringValue) async {
        projectPages.created(project); projects = projectPages.items; selected = projectPages.selectedID
        await refreshStatus(syncConfiguration: true)
        await loadOperationsSettings()
    }

    func refreshStatus(syncConfiguration: Bool = false) async {
        guard let client, !selected.isEmpty else { return }
        let generation = revision, projectID = selected
        archiveHealthRevision += 1
        let healthRevision = archiveHealthRevision
        let watermark: Int64?
        do { watermark = try await client.eventWatermark() }
        catch {
            guard generation == revision, healthRevision == archiveHealthRevision,
                  self.client === client, PlatformExactID.same(selected, projectID), !Task.isCancelled else { return }
            if archiveHighWatermark != nil {
                resetArchiveContext(watermark: nil)
                self.error = error.localizedDescription
                return
            }
            watermark = nil // Legacy REST peers need not implement event health.
        }
        guard generation == revision, healthRevision == archiveHealthRevision,
              self.client === client, PlatformExactID.same(selected, projectID), !Task.isCancelled else { return }
        if let watermark {
            let rolledBack = archiveHighWatermark.map { watermark < $0 } ?? false
            archiveHighWatermark = watermark
            if rolledBack { resetArchiveContext(watermark: watermark) }
        }
        await run(request: { client, id in try await client.monitoringProject(id, resource: "status") }) { result in
            status = result
            coverage = result["project"]["coverage"].text
            if !preparationSeeded {
                antennaNotes = project["preparation"]["antenna_notes"].text
                receivePathConfirmed = project["preparation"]["receive_path_confirmed"].bool == true
                locationConfirmed = project["preparation"]["location_confirmed"].bool == true
                preparationSeeded = true
            }
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

    var configurationBody: [String: MonitoringValue]? {
        guard canSaveConfiguration else { return nil }
        let audio: MonitoringValue
        if audioEnabled, let device = chosenDevice {
            audio = .object(["device": device["index"], "name": device["name"], "confirm": .boolean(true)])
        } else { audio = .null }
        return ["receiver_mode": .string(receiverMode), "audio": audio]
    }

    func saveConfiguration(_ body: [String: MonitoringValue]) async {
        guard canConfigure else { return }
        await command("configure", body: body, success: "Настройки приёмника сохранены.", syncConfiguration: true)
    }

    func command(_ resource: String, body: [String: MonitoringValue] = [:], success: String,
                 syncConfiguration: Bool = false) async {
        guard !selected.isEmpty, !pending else { return }
        if resource == "disarm" { researchRequests["control"] = nil; researchPending.remove("control") }
        let current = revision
        let succeeded = await run(request: { client, id in try await client.monitoringProject(id, resource: resource, body: body) }) { _ in
            notice = success
        }
        guard current == revision, !Task.isCancelled else { return }
        if resource == "disarm", succeeded { leases = [:]; search = .null; coverage = "" }
        if resource == "baselines", succeeded { await refreshStatus(); await checkPreflight() }
        if ["arm", "disarm", "configure"].contains(resource) {
            // A failed/timeout command can still have reached the core; query its actual state.
            status = .null
            await refreshStatus(syncConfiguration: syncConfiguration && succeeded)
        }
    }

    func checkPreflight() async {
        guard !selected.isEmpty else { return }
        await operationsRequest("preflight", request: { client, id in try await client.monitoringProject(id, resource: "preflight") }) { result in
            preflight = result
        }
    }

    func loadEvents() async {
        guard !selected.isEmpty else { return }
        await run(request: { client, id in try await client.monitoringProject(id, resource: "events") }) { result in
            events = result["items"].items
            if !events.contains(where: { PlatformExactID.same($0["id"].text, selectedEvent) }) {
                selectedEvent = ""; event = .null; stopAudio(); resetAudioEvidence()
            }
            notice = "Событий в текущей странице: \(events.count)."
        }
    }

    func showEvent(_ id: String) async {
        guard !pending else { return }
        stopAudio(); resetAudioEvidence(); selectedEvent = id; event = .null; sources = []; sourcesNext = ""
        researchRequests["sources"] = nil; researchPending.remove("sources")
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
            guard current == revision, requestID == token, PlatformExactID.same(selected, identifier),
                  PlatformExactID.same(selectedEvent, eventID), playbackRevision == playbackScope, !Task.isCancelled else { return }
            let playback = try AVAudioPlayer(data: bytes)
            guard playback.prepareToPlay(), playback.play() else {
                throw PlatformError.invalidResponse("Не удалось воспроизвести WAV")
            }
            player = playback; hasPlayback = true
            audioNotice = String(format: "WAV загружен: %.1f с · %d байт", playback.duration, bytes.count)
        } catch {
            if current == revision, requestID == token, PlatformExactID.same(selectedEvent, eventID),
               playbackRevision == playbackScope, !Task.isCancelled {
                self.error = error.localizedDescription
            }
        }
    }

    func loadAudioEvidence(after: String? = nil) async {
        guard audioEvidenceAvailable, !selectedEvent.isEmpty, event["analysis"]["audio"] != .null else { return }
        let eventID = selectedEvent
        await researchRequest("audio-evidence", eventID: eventID, request: { client, project in
            let info = try await client.monitoringEventAudioInfo(projectID: project, eventID: eventID)
            let marks = try await client.monitoringAudioMarks(projectID: project, eventID: eventID, after: after)
            return .object(["info": info, "marks": marks])
        }) { result in
            audioEvidenceInfo = result["info"]; audioMarks = result["marks"]["items"].items
            audioMarksNext = result["marks"]["next_cursor"].text
        }
    }

    func audioRange(start: Int64, end: Int64) -> MonitoringAudioRange? {
        guard audioEvidenceAvailable, let count = Int64(audioEvidenceInfo["frame_count"].scalar),
              start >= 0, start < end, end <= count, !selected.isEmpty, !selectedEvent.isEmpty else { return nil }
        return MonitoringAudioRange(projectID: selected, eventID: selectedEvent, artifactID: audioEvidenceInfo["artifact_id"].text,
                                    sourceSHA256: audioEvidenceInfo["sha256"].text, start: start, end: end)
    }
    private func currentAudioRange(_ selection: MonitoringAudioRange) -> Bool {
        audioEvidenceAvailable && PlatformExactID.same(selected, selection.projectID) && PlatformExactID.same(selectedEvent, selection.eventID)
            && PlatformExactID.same(audioEvidenceInfo["artifact_id"].text, selection.artifactID) && audioEvidenceInfo["sha256"].text == selection.sourceSHA256
    }
    func saveAudioMark(_ selection: MonitoringAudioRange, label: String) async {
        guard currentAudioRange(selection) else { return }
        let eventID = selection.eventID, hash = selection.sourceSHA256
        let saved = await researchRequest("audio-mark", eventID: eventID, request: { client, project in
            try await client.monitoringAddAudioMark(projectID: project, eventID: eventID, startFrame: selection.start, endFrame: selection.end, label: label, sourceArtifactID: selection.artifactID, sourceSHA256: hash)
        }) { _ in audioNotice = "Аудиоотметка сохранена; исходная запись не изменена." }
        if saved { await loadAudioEvidence() }
    }

    func exportAudioClip(_ selection: MonitoringAudioRange, to destination: URL) async {
        guard let client, currentAudioRange(selection), !researchPending.contains("audio-export") else { return }
        let generation = revision, projectID = selected, eventID = selectedEvent, token = UUID()
        let hash = selection.sourceSHA256
        researchRequests["audio-export"] = token; researchPending.insert("audio-export"); researchError = ""
        defer { if researchRequests["audio-export"] == token { researchRequests["audio-export"] = nil; researchPending.remove("audio-export") } }
        do {
            let bytes = try await client.monitoringEventAudioClip(projectID: projectID, eventID: eventID, startFrame: selection.start, endFrame: selection.end, sourceArtifactID: selection.artifactID, sourceSHA256: hash)
            guard revision == generation, PlatformExactID.same(selected, projectID), PlatformExactID.same(selectedEvent, eventID),
                  researchRequests["audio-export"] == token, currentAudioRange(selection), !Task.isCancelled else { return }
            let staged = try await audioExportWriter.stage(bytes, for: destination)
            defer { staged.discard() }
            guard revision == generation, PlatformExactID.same(selected, projectID), PlatformExactID.same(selectedEvent, eventID),
                  self.client === client, researchRequests["audio-export"] == token, currentAudioRange(selection), !Task.isCancelled else { return }
            try staged.publish(to: destination)
            audioNotice = "Фрагмент WAV экспортирован с исходными отсчётами и контекстом записи."
        } catch {
            if revision == generation, PlatformExactID.same(selected, projectID), PlatformExactID.same(selectedEvent, eventID),
               researchRequests["audio-export"] == token, !Task.isCancelled { researchError = error.localizedDescription }
        }
    }
    @discardableResult
    private func researchRequest(
        _ channel: String, eventID: String? = nil, sweepID: String? = nil,
        request: (PlatformClient, String) async throws -> MonitoringValue,
        apply: (MonitoringValue) throws -> Void
    ) async -> Bool {
        guard let client, !selected.isEmpty, !researchPending.contains(channel) else { return false }
        let generation = revision, projectID = selected, token = UUID()
        researchRequests[channel] = token; researchPending.insert(channel)
        if channel != "search" { researchError = "" }
        defer {
            if researchRequests[channel] == token { researchRequests[channel] = nil; researchPending.remove(channel) }
        }
        do {
            let result = try await request(client, projectID)
            guard revision == generation, PlatformExactID.same(selected, projectID), researchRequests[channel] == token,
                  eventID == nil || PlatformExactID.same(selectedEvent, eventID), sweepID == nil || PlatformExactID.same(selectedSweepID, sweepID),
                  !Task.isCancelled else { return false }
            try apply(result)
            return true
        } catch {
            if revision == generation, PlatformExactID.same(selected, projectID), researchRequests[channel] == token,
               eventID == nil || PlatformExactID.same(selectedEvent, eventID), sweepID == nil || PlatformExactID.same(selectedSweepID, sweepID),
               !Task.isCancelled { researchError = error.localizedDescription }
            return false
        }
    }

    func manual(_ action: String, role: String) async {
        guard !pending, ["receiver", "analyzer"].contains(role), ["acquire", "renew", "return"].contains(action) else { return }
        let lease = leases[role]
        guard action == "acquire" || lease != nil else { return }
        let body: [String: MonitoringValue] = action == "acquire" ? ["role": .string(role)] : ["token": .string(lease!.token)]
        let current = revision, requestedAt = Date()
        let succeeded = await researchRequest("control", request: { client, id in
            try await client.monitoringControl(projectID: id, action: "manual/" + action, body: body)
        }) { result in
            if action == "return" {
                leases[role] = nil
                notice = "Ручное управление возвращено; восстановление анализатора подтверждается состоянием ядра."
            } else {
                let token = action == "renew" ? lease!.token : result["token"].text
                guard !token.isEmpty, result["role"].text == role,
                      let seconds = result["expires_seconds"].double, seconds > 0, seconds.isFinite else {
                    throw PlatformError.invalidResponse("Ядро не подтвердило ручное управление")
                }
                leases[role] = .init(token: token, expiresAt: requestedAt.addingTimeInterval(seconds))
                notice = "Ручное управление получено. Продлевайте явно до истечения срока."
            }
        }
        if current == revision, succeeded, !Task.isCancelled { await refreshSearch(); await refreshStatus() }
    }

    func tuneManually(frequency: String, mode: String) async {
        guard !pending else { return }
        guard let lease = leases["receiver"], lease.expiresAt > Date() else { researchError = "Нужно действующее ручное управление приёмником."; return }
        guard let hz = Int64(frequency), hz > 0, receiverModes.contains(mode) else { researchError = "Введите целую частоту Гц и режим приёмника."; return }
        await researchRequest("control", request: { client, id in
            try await client.monitoringControl(projectID: id, action: "manual/tune",
                body: ["token": .string(lease.token), "frequency_hz": .integer(hz), "mode": .string(mode)])
        }) { _ in notice = "Команда ручной настройки передана ядру." }
    }

    func startSearch(eventID: String, span: String) async {
        guard !pending else { return }
        guard let lease = leases["analyzer"], lease.expiresAt > Date() else { researchError = "Нужно действующее ручное управление анализатором."; return }
        guard !eventID.isEmpty, PlatformExactID.same(selectedEvent, eventID), let hz = Int64(span), hz > 0 else { researchError = "Выберите событие и целую положительную полосу Гц."; return }
        let current = revision
        let succeeded = await researchRequest("control", eventID: eventID, request: { client, id in
            try await client.monitoringControl(projectID: id, action: "search/start",
                body: ["token": .string(lease.token), "event_id": .string(eventID), "span_hz": .integer(hz)])
        }) { _ in notice = "Поиск источника запрошен. Широкий обзор приостановлен; проверяйте покрытие." }
        if current == revision, succeeded, !Task.isCancelled { await refreshSearch(); await refreshStatus() }
    }

    func refreshSearch() async {
        await researchRequest("search", request: { client, id in try await client.monitoringSearchStatus(projectID: id) }) { result in
            search = result; coverage = result["coverage"].text
        }
    }

    func markSearch(label: String) async {
        guard !pending else { return }
        guard let lease = leases["analyzer"], lease.expiresAt > Date(), !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            researchError = "Нужны действующее ручное управление анализатором и подпись метки."; return
        }
        let current = revision
        let succeeded = await researchRequest("control", request: { client, id in
            try await client.monitoringControl(projectID: id, action: "search/mark", body: ["token": .string(lease.token), "label": .string(label)])
        }) { _ in notice = "Метка сохранена ядром." }
        if current == revision, succeeded, !Task.isCancelled { await refreshSearch() }
    }

    func loadHistory(start: String, end: String, minHz: String, maxHz: String, eventID: String?) async {
        func optional(_ value: String) -> String? { value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : value.trimmingCharacters(in: .whitespacesAndNewlines) }
        let lower = optional(minHz), upper = optional(maxHz)
        guard lower == nil || Int64(lower!) != nil, upper == nil || Int64(upper!) != nil else { researchError = "Границы истории задаются целыми Гц."; return }
        let query = MonitoringHistoryQuery(limit: 64, start: optional(start), end: optional(end),
            minHz: lower.flatMap(Int64.init), maxHz: upper.flatMap(Int64.init), eventID: eventID)
        await historyPage(query: query, after: nil)
    }

    func nextHistoryPage() async { guard !historyNext.isEmpty else { return }; await historyPage(query: historyQuery, after: historyNext) }

    private func historyPage(query: MonitoringHistoryQuery, after: String?) async {
        await researchRequest("history", request: { client, id in try await client.monitoringHistory(projectID: id, query: query, after: after) }) { result in
            let rows = try result["items"].items.map { try MonitoringHistoryRow($0) }
            historyQuery = query; history = rows.sorted { $0.startedAt == $1.startedAt ? $0.id < $1.id : $0.startedAt < $1.startedAt }
            historyNext = result["next_cursor"].text
            selectSweep(history.first?.id ?? "")
            historyDescription = "UTC \(query.start ?? "начало") — \(query.end ?? "конец") · \(query.minHz.map(String.init) ?? "min")…\(query.maxHz.map(String.init) ?? "max") Гц · \(rows.count) свипов в странице · событие: \(query.eventID ?? "все")"
        }
    }

    func selectSweep(_ id: String) {
        selectedSweepID = id; rawSweep = nil; rawDetail = .null; baselineComparison = .null
        researchRequests["sweep"] = nil; researchPending.remove("sweep")
    }

    func loadSelectedSweep(sweepID: String, comparison: Bool) async {
        guard !sweepID.isEmpty, PlatformExactID.same(selectedSweepID, sweepID) else { return }
        if comparison, !researchPending.contains("sweep") { baselineComparison = .null }
        await researchRequest("sweep", sweepID: sweepID, request: { client, id in
            try await client.monitoringHistorySweep(projectID: id, sweepID: sweepID, comparison: comparison)
        }) { result in
            if comparison { baselineComparison = result }
            else { rawSweep = try MonitoringHistoryRow(result, raw: true); rawDetail = result }
        }
    }

    func loadSources(eventID: String, next: Bool) async {
        let after = next ? sourcesNext : nil
        guard !eventID.isEmpty, PlatformExactID.same(selectedEvent, eventID), !next || after?.isEmpty == false else { return }
        await researchRequest("sources", eventID: eventID, request: { client, id in
            try await client.monitoringEventSources(projectID: id, eventID: eventID, limit: 64, after: after)
        }) { result in sources = result["items"].items; sourcesNext = result["next_cursor"].text }
    }

    func stopOperations() {
        operationsRevision += 1; operationsRequests = [:]; operationsPending = []
        webhookSecret = ""
    }

    func matchesOperations(endpoint: String, projectID: String) -> Bool { PlatformExactID.same(scope, endpoint) && PlatformExactID.same(selected, projectID) }

    @discardableResult
    private func operationsRequest(
        _ channel: String, eventID: String? = nil,
        request: (PlatformClient, String) async throws -> MonitoringValue,
        apply: (MonitoringValue) -> Void
    ) async -> Bool {
        guard let client, !selected.isEmpty, !operationsPending.contains(channel) else { return false }
        let generation = revision, viewGeneration = operationsRevision, projectID = selected, token = UUID()
        operationsRequests[channel] = token; operationsPending.insert(channel)
        defer {
            if operationsRequests[channel] == token { operationsRequests[channel] = nil; operationsPending.remove(channel) }
        }
        do {
            let result = try await request(client, projectID)
            guard revision == generation, operationsRevision == viewGeneration, PlatformExactID.same(selected, projectID),
                  operationsRequests[channel] == token, eventID == nil || PlatformExactID.same(selectedEvent, eventID), !Task.isCancelled else { return false }
            operationsErrors[channel] = nil
            // These responses never write the general diagnostic JSON, especially settings.
            apply(result)
            return true
        } catch {
            if revision == generation, operationsRevision == viewGeneration, PlatformExactID.same(selected, projectID),
               operationsRequests[channel] == token, eventID == nil || PlatformExactID.same(selectedEvent, eventID), !Task.isCancelled {
                operationsErrors[channel] = error.localizedDescription
                if channel == "storage" { storage = .null }
            }
            return false
        }
    }

    func loadOperationsSettings() async {
        await operationsRequest("settings", request: { client, id in try await client.monitoringSettings(projectID: id) }) { result in
            applySettings(result)
        }
    }

    private func applySettings(_ value: MonitoringValue, syncDetector: Bool = true) {
        operationsSettings = value
        autostart = value["autostart"].bool == true
        archiveReserve = value["archive"]["minimum_free_bytes"].scalar
        archiveRetention = value["archive"]["minimum_retention_seconds"].scalar
        webhookEnabled = value["webhook"]["enabled"].bool == true
        webhookURL = value["webhook"]["url"].text
        if syncDetector { applyDetectorDraft(value["detector"]) }
    }

    var operationsSettingsValidation: String? {
        guard let reserve = Int64(archiveReserve), (1_048_576...1_000_000_000_000_000).contains(reserve) else {
            return "Резерв: целые байты от 1048576 до 1000000000000000."
        }
        guard let retention = Int64(archiveRetention), (60...31_536_000).contains(retention) else {
            return "Срок хранения: целые секунды от 60 до 31536000."
        }
        if webhookSecret.unicodeScalars.count > 1024 { return "Новый секрет должен содержать не более 1024 символов." }
        if webhookURL.unicodeScalars.count > 2048 { return "Адрес webhook должен содержать не более 2048 символов." }
        if !webhookURL.isEmpty {
            guard let url = URLComponents(string: webhookURL), ["http", "https"].contains(url.scheme ?? ""),
                  !(url.host ?? "").isEmpty, url.user == nil, url.password == nil, url.fragment == nil else {
                return "Для webhook нужен HTTP(S) URL без учётных данных и fragment."
            }
        }
        if webhookEnabled && (webhookURL.isEmpty || (webhookSecret.isEmpty && operationsSettings["webhook"]["secret_configured"].bool != true)) {
            return "Для включения webhook укажите адрес и новый или уже сохранённый секрет."
        }
        return nil
    }

    var operationsSettingsBody: [String: MonitoringValue]? {
        guard operationsSettingsValidation == nil,
              let reserve = Int64(archiveReserve), let retention = Int64(archiveRetention) else { return nil }
        var webhook: [String: MonitoringValue] = ["enabled": .boolean(webhookEnabled), "url": .string(webhookURL)]
        if !webhookSecret.isEmpty { webhook["secret"] = .string(webhookSecret) }
        return ["autostart": .boolean(autostart),
            "archive": .object(["minimum_free_bytes": .integer(reserve), "minimum_retention_seconds": .integer(retention)]),
            "webhook": .object(webhook)]
    }

    func saveOperationsSettings(body: [String: MonitoringValue]) async {
        let current = revision, viewGeneration = operationsRevision
        let secret = body["webhook"]?["secret"].text ?? ""
        let saved = await operationsRequest("settings", request: { client, id in try await client.monitoringSettings(projectID: id, body: body) }) { result in
            applySettings(result, syncDetector: false); webhookSecret = ""
            operationsNotice = "Настройки проекта сохранены."
        }
        if current == revision, viewGeneration == operationsRevision {
            // Clear the entered credential after an attempt, including a rejected save.
            if webhookSecret == secret { webhookSecret = "" }
            if saved, !Task.isCancelled { await refreshStorage() }
        }
    }

    func savePreparation(notes: String, receivePath: Bool, location: Bool) async {
        guard canEditPreparation else { return }
        let current = revision
        let saved = await operationsRequest("preparation", request: { client, id in
            try await client.monitoringSavePreparation(projectID: id, antennaNotes: notes,
                receivePathConfirmed: receivePath, locationConfirmed: location)
        }) { _ in operationsNotice = "Подготовка сохранена как заявление оператора; аппаратная проверка не заявляется." }
        if current == revision, saved, !Task.isCancelled { await refreshStatus(); await checkPreflight() }
    }

    func startPreparation() async {
        guard canStartPreparation else { return }
        let current = revision
        let started = await operationsRequest("preparation", request: { client, id in try await client.monitoringStartPreparation(projectID: id) }) { _ in
            operationsNotice = "Ядро получило команду запуска приёмных потоков. Дождитесь полного набора данных эталона; генератор не включается."
        }
        if current == revision, started, !Task.isCancelled { await refreshStatus(); await checkPreflight() }
    }

    func stopPreparation() async {
        guard !selected.isEmpty, !isArmed else { return }
        let current = revision
        let stopped = await operationsRequest("preparation", request: { client, id in try await client.monitoringStopPreparation(projectID: id) }) { _ in
            operationsNotice = "Сбор данных эталона остановлен по команде оператора. Архивные данные сохранены."
        }
        if current == revision, stopped, !Task.isCancelled { await refreshStatus(); await checkPreflight() }
    }

    private func applyDetectorDraft(_ value: MonitoringValue) {
        detectorMargin = value["margin_db"].double.map { String($0) } ?? ""
        detectorImpulse = value["impulse_db"].double.map { String($0) } ?? ""
        detectorHysteresis = value["hysteresis_db"].double.map { String($0) } ?? ""
        detectorConfirmations = value["confirmations"].integer.map { String($0) } ?? ""
        detectorWindow = value["window_sweeps"].integer.map { String($0) } ?? ""
        detectorDwell = value["dwell_seconds"].double.map { String($0) } ?? ""
    }

    func balancedDetectorDraft() {
        applyDetectorDraft(.object(MonitoringDetectorSettings().body))
        operationsNotice = "Сбалансированные инженерные значения заполнены. Сохраните их явно после проверки."
    }

    func detectorDraft() throws -> MonitoringDetectorSettings {
        guard let margin = Double(detectorMargin), let impulse = Double(detectorImpulse), let hysteresis = Double(detectorHysteresis),
              let confirmations = Int64(detectorConfirmations), let window = Int64(detectorWindow), let dwell = Double(detectorDwell) else {
            throw PlatformError.invalidCommand("Введите числовые значения с десятичной точкой; подтверждения и окно — целые числа.")
        }
        return try MonitoringDetectorSettings(marginDB: margin, impulseDB: impulse, hysteresisDB: hysteresis,
            confirmations: confirmations, windowSweeps: window, dwellSeconds: dwell)
    }

    var detectorValidation: String? {
        do { _ = try detectorDraft(); return nil } catch { return error.localizedDescription }
    }

    func saveDetector(_ draft: MonitoringDetectorSettings) async {
        guard canConfigure else { return }
        let current = revision
        let saved = await operationsRequest("settings", request: { client, id in
            try await client.monitoringSettings(projectID: id, body: ["detector": .object(draft.body)])
        }) { result in
            operationsSettings = result; applyDetectorDraft(result["detector"])
            operationsNotice = "Параметры детектора сохранены. Инженерные пороги не являются аппаратной калибровкой."
        }
        if current == revision, saved, !Task.isCancelled { await refreshStatus(); await checkPreflight() }
    }

    func refreshStorage() async {
        await operationsRequest("storage", request: { client, id in try await client.monitoringStorage(projectID: id) }) { storage = $0 }
    }

    func pollAlerts() async {
        await operationsRequest("alerts-live", request: { client, id in try await client.monitoringAlerts(projectID: id) }) { result in
            let items = result["items"].items
            if soundState.observe(items, enabled: localSoundEnabled) { NSSound.beep() }
            if alertsLatest { alerts = items; alertsNext = result["next_cursor"].text; alertsPageAfter = nil }
        }
    }

    func showLatestAlerts() async { alertsLatest = true; await pollAlerts() }

    func alertPage(after: String?) async {
        if after == nil { await showLatestAlerts(); return }
        alertsLatest = false
        await operationsRequest("alerts-page", request: { client, id in try await client.monitoringAlerts(projectID: id, after: after) }) { result in
            alerts = result["items"].items; alertsNext = result["next_cursor"].text; alertsPageAfter = after
        }
    }

    func acknowledgeAlert(_ alertID: String) async {
        let current = revision
        let saved = await operationsRequest("alerts-ack", request: { client, id in try await client.monitoringAcknowledgeAlert(projectID: id, alertID: alertID) }) { _ in
            operationsNotice = "Оповещение подтверждено оператором."
        }
        guard current == revision, saved, !Task.isCancelled else { return }
        if alertsLatest { await pollAlerts() } else { await alertPage(after: alertsPageAfter) }
    }

    func reviewEvent(eventID: String, disposition: String, comment: String) async {
        guard PlatformExactID.same(selectedEvent, eventID) else { return }
        await operationsRequest("event-review", eventID: eventID, request: { client, id in
            try await client.monitoringReviewEvent(projectID: id, eventID: eventID, disposition: disposition, comment: comment)
        }) { result in
            event = result
            if let index = events.firstIndex(where: { PlatformExactID.same($0["id"].text, eventID) }) { events[index] = result }
            operationsNotice = "Решение оператора сохранено."
        }
    }

}

struct MonitoringManualLease {
    let token: String
    let expiresAt: Date
}
