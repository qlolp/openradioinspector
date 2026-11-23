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
        pendingSession = nil; location = .null; locationError = ""
        let project = workspace.project
        let kind = NativeDutySource(rawValue: project["primary_source_kind"].text)
        let legacyPair = project["profile"]["analyzer"]["settings"]["adapter"].text == "tinysa-serial" &&
            project["profile"]["receiver"]["settings"]["adapter"].text == "ardv1-serial"
        if workspace.includesSession(sessionID), kind != nil || legacyPair {
            draft.source = kind ?? .tinySA
            projectID = workspace.selected; draft.name = project["name"].text; draft.site = project["site"].text
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
        projectID = ""
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
            let capabilities = try await client.coreCapabilities()
            try NativeRCPreparationDraft.validateCapabilities(capabilities)
            if requested.source != .tinySA || requested.receiverPort.isEmpty {
                guard capabilities["features"]["single_source_duty"].bool == true else {
                    throw PlatformError.invalidCommand("Для выбранного состава нужен узел с поддержкой одиночного дежурства.")
                }
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
            let project = try await client.monitoring(body: ["name": .string(requested.name), "site": .string(requested.site), "session_id": .string(sessionID)])
            guard current(token) else { return }
            let id = project["id"].text
            guard !id.isEmpty else { throw PlatformError.invalidResponse("Ядро не вернуло ID проекта.") }
            pendingSession = nil
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
        guard current(token), owns(workspace), workspace.canStartPreparation else { return }
        await workspace.startPreparation()
        guard current(token), owns(workspace) else { return }
        message = "Ожидаются первая запись и накопление совместимых измерений для эталона."
    }

    func saveBaseline(workspace: MonitoringWorkspaceModel) async {
        guard let client, owns(workspace), !busy, workspace.baselineConfirmed, workspace.preflightIsCurrent(),
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
            await workspace.command("baselines", body: ["name": .string(draft.name), "location": .string(draft.site), "confirm": .boolean(true)], success: "Эталон сохранён.")
            guard current(token), owns(workspace) else { return }
            await workspace.loadOperationsSettings()
            await workspace.checkPreflight()
            if current(token) { message = "Проверьте бюджет и результаты preflight. Охрана включается отдельной кнопкой." }
        } catch { if current(token) { self.error = error.localizedDescription } }
    }
}
