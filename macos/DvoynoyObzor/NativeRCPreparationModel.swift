import Foundation
import Combine
import SpektrKomandy

@MainActor
final class NativeRCPreparationModel: ObservableObject {
    @Published var draft = NativeRCPreparationDraft()
    @Published private(set) var projectID = ""
    @Published private(set) var busy = false
    @Published private(set) var location: MonitoringValue = .null
    @Published private(set) var locationError = ""
    @Published private(set) var original: NativeDutyOriginal?
    @Published private(set) var originalError = ""
    private var pendingProject: (session: String, site: String, plan: MonitoringValue?, project: MonitoringValue)?
    private var pendingSession: (name: String, plan: NativeDutyAcquisition, id: String)?
    @Published private(set) var message = "Создание профиля сохраняет настройки и не открывает USB-порты."
    @Published private(set) var error = ""
    private var client: PlatformClient?
    private var endpoint = ""
    private var revision = 0

    func configure(server: String, client: PlatformClient?, workspace: MonitoringWorkspaceModel, sessionID: String) {
        guard !PlatformExactID.same(server, endpoint) || client !== self.client else { return }
        revision += 1; endpoint = server; self.client = client; projectID = ""; busy = false; error = ""
        workspace.configure(server, client: client)
        pendingSession = nil; pendingProject = nil; original = nil; originalError = ""; location = .null; locationError = ""
        let project = workspace.project
        let kind = NativeDutySource(rawValue: project["primary_source_kind"].text)
        let legacyPair = project["profile"]["analyzer"]["settings"]["adapter"].text == "tinysa-serial" &&
            project["profile"]["receiver"]["settings"]["adapter"].text == "ardv1-serial"
        if workspace.includesSession(sessionID), kind != nil || legacyPair {
            draft.source = kind ?? .tinySA
            projectID = workspace.selected; draft.name = project["name"].text; draft.site = project["site"].text
            let profile = project["profile"]
            draft.receiverPort = profile["receiver"]["port"].text
            if draft.source == .tinySA {
                draft.tinySAPort = profile["analyzer"]["port"].text
                draft.tinyUsesGrid = project["scan_plan"]["config"]["bands"].items.first?["step_hz"].integer == 10_000
            } else if draft.source == .rfExplorer {
                draft.explorer.port = profile["analyzer"]["port"].text
            }
            message = "Продолжение выбранного проекта. Приборы не переподключаются при открытии мастера."
        }
    }
    func readLocation() async {
        guard let client else { return }
        let token = revision
        do {
            let value = try await client.nodeArchiveLocation()
            guard current(token) else { return }
            location = value; locationError = ""
        } catch { if current(token) { location = .null; locationError = error.localizedDescription } }
    }

    func cancel() { revision += 1; busy = false }
    func newProfile(workspace: MonitoringWorkspaceModel) async {
        guard owns(workspace), !busy, !workspace.isArmed, !workspace.dutyEnabled else { return }
        let token = revision
        busy = true
        defer { if revision == token { busy = false } }
        if workspace.isCollecting { await workspace.stopPreparation() }
        guard current(token), owns(workspace), !workspace.isCollecting else { return }
        projectID = ""; original = nil; originalError = ""; pendingSession = nil; pendingProject = nil
        message = "Выберите новый диапазон или порты. Предыдущий проект и все измерения сохранены."
    }
    func owns(_ workspace: MonitoringWorkspaceModel) -> Bool {
        !projectID.isEmpty && PlatformExactID.same(projectID, workspace.selected) && workspace.matchesOperations(endpoint: endpoint, projectID: projectID)
    }
    private func current(_ token: Int) -> Bool { revision == token && !Task.isCancelled }

    func create(workspace: MonitoringWorkspaceModel, onSession: (String) -> Void) async {
        guard let client, !busy, !workspace.isArmed, !workspace.dutyEnabled else { return }
        let token = revision, requested = draft
        busy = true; error = ""
        defer { if revision == token { busy = false } }
        do {
            let plan = try requested.validated()
            let scanPlan = try requested.scanPlan()
            let capabilities = try await client.coreCapabilities()
            try NativeRCPreparationDraft.validateCapabilities(capabilities)
            if requested.source != .tinySA || requested.receiverPort.isEmpty {
                guard capabilities["features"]["single_source_duty"].bool == true else {
                    throw PlatformError.invalidCommand("Для выбранного состава нужен узел с поддержкой одиночного дежурства.")
                }
            }
            if scanPlan != nil, capabilities["features"]["scan_plan_grid_10khz"].bool != true {
                throw PlatformError.invalidCommand("Узел не поддерживает сетку tinySA 10 кГц. Выберите одиночный свип или обновите узел.")
            }
            guard current(token) else { return }
            let sessionID: String
            if let pendingSession, pendingSession.plan == plan, pendingSession.name == requested.name {
                sessionID = pendingSession.id
            } else {
                let session = try await plan.create(client: client, name: requested.name)
                guard current(token), !session.id.isEmpty else { return }
                sessionID = session.id
                pendingSession = (requested.name, plan, session.id)
            }
            let project: MonitoringValue
            let retry: Bool
            if let pendingProject, PlatformExactID.same(pendingProject.session, sessionID),
               pendingProject.site == requested.site, pendingProject.plan == scanPlan {
                project = pendingProject.project; retry = true
            } else {
                retry = false
                project = try await client.monitoring(body: ["name": .string(requested.name), "site": .string(requested.site), "session_id": .string(sessionID)])
                guard current(token), !project["id"].text.isEmpty else { throw PlatformError.invalidResponse("Ядро не вернуло ID проекта.") }
                pendingProject = (sessionID, requested.site, scanPlan, project)
            }
            guard current(token) else { return }
            let id = project["id"].text
            guard !id.isEmpty else { throw PlatformError.invalidResponse("Ядро не вернуло ID проекта.") }
            if let scanPlan, case .object(let fields) = scanPlan {
                let existing = retry ? try await client.monitoringProject(id, resource: "scan-plan") : .null
                guard current(token) else { return }
                // After a response timeout the core may already have committed a revision.
                let encoder = JSONEncoder()
                encoder.outputFormatting = [.sortedKeys]
                let expected = try encoder.encode(scanPlan)
                if try encoder.encode(existing["config"]) != expected {
                    let saved = try await client.monitoringProject(id, resource: "scan-plan", body: fields)
                    guard try encoder.encode(saved["config"]) == expected else {
                        throw PlatformError.invalidResponse("Ядро не подтвердило полную сетку tinySA. Проверьте диапазон и повторите сохранение.")
                    }
                }
            }
            guard current(token) else { return }
            pendingSession = nil; pendingProject = nil
            projectID = id
            await workspace.adoptCreatedProject(project)
            guard current(token), owns(workspace) else { return }
            onSession(sessionID)
            message = "Профиль создан. Подтвердите тракт и запустите пробный приём."
        } catch { if current(token) { self.error = error.localizedDescription } }
    }

    func connect(workspace: MonitoringWorkspaceModel) async {
        guard owns(workspace), !busy, workspace.canEditPreparation else { return }
        let token = revision
        busy = true; error = ""
        defer { if revision == token { busy = false } }
        await workspace.savePreparation(notes: workspace.antennaNotes, receivePath: workspace.receivePathConfirmed, location: workspace.locationConfirmed)
        guard current(token), owns(workspace) else { return }
        guard workspace.canStartPreparation else {
            let reasons = workspace.preflight["checks"].items.filter { $0["ok"].bool == false }.map { $0["detail"].text }
            error = reasons.isEmpty ? "Узел не подтвердил готовность к пробному приёму. Обновите проверку." : reasons.joined(separator: " · ")
            return
        }
        await workspace.startPreparation()
        guard current(token), owns(workspace) else { return }
        message = "Ожидаются первая запись и накопление совместимых измерений для эталона."
    }

    func verifiedOriginal(_ workspace: MonitoringWorkspaceModel) -> NativeDutyOriginal? {
        guard owns(workspace), let original, original.run == NativeDutyRun(workspace.project) else { return nil }
        return original
    }

    func readOriginal(workspace: MonitoringWorkspaceModel) async {
        guard let client, owns(workspace), let run = NativeDutyRun(workspace.project),
              verifiedOriginal(workspace) == nil else { return }
        let token = revision
        do {
            let record = try await NativeDutyOriginal.read(run: run, client: client)
            guard current(token), owns(workspace), run == NativeDutyRun(workspace.project) else { return }
            original = record; originalError = ""
        } catch {
            guard current(token), owns(workspace), run == NativeDutyRun(workspace.project) else { return }
            if case PlatformError.http(status: 404, code: _, message: _) = error { originalError = "" }
            else { originalError = error.localizedDescription }
        }
    }

    func saveBaseline(workspace: MonitoringWorkspaceModel) async {
        guard let client, verifiedOriginal(workspace) != nil, !busy, workspace.baselineConfirmed, workspace.preflightIsCurrent(),
              workspace.savedPreparationReady, !workspace.preparationDirty,
              NativeRCReadiness(preflight: workspace.preflight).baselineCollected else { return }
        let token = revision, id = projectID
        busy = true; error = ""
        defer { if revision == token { busy = false } }
        do {
            // Enable authoritative dynamic reservation before a baseline can
            // make this project armable, including from another client.
            _ = try await client.monitoringSettings(projectID: id, body: ["autostart": .boolean(false), "archive": .object([
                "minimum_retention_seconds": .integer(43_200), "minimum_free_bytes": .integer(268_435_456), "reserve_backup": .boolean(true)])])
            guard current(token), owns(workspace) else { return }
            let progress = NativeBaselineProgress(preflight: workspace.preflight)
            let base: [String: MonitoringValue] = ["name": .string(draft.name), "location": .string(draft.site), "confirm": .boolean(true)]
            if progress.bands.isEmpty {
                _ = try await client.monitoringProject(id, resource: "baselines", body: base)
            } else {
                for band in progress.eligibleBands where band["confirmed_windows"].integer != band["eligible_windows"].integer {
                    guard current(token), verifiedOriginal(workspace) != nil else { return }
                    var body = base
                    body["band_id"] = band["band_id"]
                    _ = try await client.monitoringProject(id, resource: "baselines", body: body)
                }
            }
            guard current(token), owns(workspace) else { return }
            await workspace.refreshStatus()
            await workspace.loadOperationsSettings()
            await workspace.checkPreflight()
            if current(token) { message = "Проверьте бюджет и результаты preflight. Охрана включается отдельной кнопкой." }
        } catch { if current(token) { self.error = error.localizedDescription } }
    }
}
