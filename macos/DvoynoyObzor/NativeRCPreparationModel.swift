import Foundation
import Combine
import SpektrKomandy

struct NativeRCPreparationDraft {
    var name = "Радиомониторинг · Mac", site = "Объект проверки"
    var tinySAPort = "", receiverPort = ""
    var startHz = "88000000", stopHz = "108000000"
    var points = 201, rbwHz = 10_000
    func validated() throws -> PlatformSerialReceiveConfiguration {
        let nativePort: (String) -> Bool = { path in
            path.hasPrefix("/dev/cu.") && path.utf8.count <= 1024 && !path.contains("\0") && !path.dropFirst(5).contains("/")
        }
        guard nativePort(tinySAPort), nativePort(receiverPort), tinySAPort != receiverPort else {
            throw PlatformError.invalidCommand("Выберите два разных порта /dev/cu.…; симуляторы в этом мастере не используются.")
        }
        for text in [name, site] {
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.unicodeScalars.count <= 200 else {
                throw PlatformError.invalidCommand("Название и объект: 1…200 символов.")
            }
        }
        guard !startHz.isEmpty, !stopHz.isEmpty, startHz.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
              stopHz.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }), let start = Int(startHz), let stop = Int(stopHz) else {
            throw PlatformError.invalidCommand("Частоты задаются целыми Гц.")
        }
        return try PlatformSerialReceiveConfiguration(startHz: start, stopHz: stop, points: points, rbwHz: rbwHz)
    }
    static func validateCapabilities(_ value: MonitoringValue) throws {
        guard value["candidate"]["mode"].text == "live", value["features"]["explicit_serial_adapters"].bool == true,
              value["features"]["monitoring_backup_reserve"].bool == true else {
            throw PlatformError.invalidResponse("Мастеру нужна live Rust-служба с serial и проверкой резерва backup. Переключение на replay или Python не выполняется.")
        }
    }
}

struct NativeRCReadiness {
    let preflight: MonitoringValue
    var baselineCollected: Bool {
        let collection = preflight["baseline_collection"]
        return collection["ready"].bool == true && (collection["sweeps"].integer ?? 0) >= 20 && (collection["elapsed_seconds"].double ?? 0) >= 60
    }
    var budgetReady: Bool {
        let storage = preflight["storage"]
        guard let rate = storage["bytes_per_second"].double, rate.isFinite, rate > 0 else { return false }
        return storage["ready"].bool == true && storage["write_probe"].bool == true && storage["backup_reserve_requested"].bool == true
            && (storage["required_retention_seconds"].integer ?? 0) >= 43_200
    }
}

@MainActor
final class NativeRCPreparationModel: ObservableObject {
    @Published var draft = NativeRCPreparationDraft()
    @Published private(set) var projectID = ""
    @Published private(set) var busy = false
    @Published private(set) var message = "Создание профиля сохраняет настройки и не открывает USB-порты."
    @Published private(set) var error = ""
    private var client: PlatformClient?
    private var endpoint = ""
    private var revision = 0

    func configure(server: String, client: PlatformClient?, workspace: MonitoringWorkspaceModel) {
        guard !PlatformExactID.same(server, endpoint) || client !== self.client else { return }
        revision += 1; endpoint = server; self.client = client; projectID = ""; busy = false; error = ""
        workspace.configure(server, client: client)
        let paths = Array(SerialPort.availablePaths().prefix(256))
        draft.tinySAPort = paths.first(where: { $0.contains("usbmodem") }) ?? ""
        draft.receiverPort = paths.first(where: { $0 != draft.tinySAPort && $0.contains("usbserial") }) ?? ""
        let project = workspace.project
        if !workspace.selected.isEmpty, project["profile"]["analyzer"]["settings"]["adapter"].text == "tinysa-serial",
           project["profile"]["receiver"]["settings"]["adapter"].text == "ardv1-serial" {
            projectID = workspace.selected; draft.name = project["name"].text; draft.site = project["site"].text
            message = "Продолжение выбранного проекта. Приборы не переподключаются при открытии мастера."
        }
    }
    func cancel() { revision += 1; busy = false }
    func newProfile(workspace: MonitoringWorkspaceModel) async {
        guard owns(workspace), !busy, !workspace.isArmed else { return }
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
        guard let client, !busy, !workspace.isArmed else { return }
        let token = revision, requested = draft
        busy = true; error = ""
        defer { if revision == token { busy = false } }
        do {
            let receive = try requested.validated()
            try NativeRCPreparationDraft.validateCapabilities(await client.coreCapabilities())
            guard current(token) else { return }
            let session = try await client.createSerialSession(name: requested.name, tinySAPort: requested.tinySAPort,
                ardv1Port: requested.receiverPort, receive: receive, monitorReceiverProfile: true)
            guard current(token) else { return }
            let project = try await client.monitoring(body: ["name": .string(requested.name), "site": .string(requested.site), "session_id": .string(session.id)])
            guard current(token) else { return }
            let id = project["id"].text
            guard !id.isEmpty else { throw PlatformError.invalidResponse("Ядро не вернуло ID проекта.") }
            _ = try await client.monitoringSettings(projectID: id, body: ["autostart": .boolean(false), "archive": .object([
                "minimum_free_bytes": .integer(268_435_456), "minimum_retention_seconds": .integer(43_200), "reserve_backup": .boolean(false)])])
            guard current(token) else { return }
            _ = try await client.monitoringProject(id, resource: "configure", body: ["audio": .null, "receiver_mode": .string("AUTO")])
            guard current(token) else { return }
            projectID = id
            await workspace.adoptCreatedProject(project)
            guard current(token), owns(workspace) else { return }
            onSession(session.id)
            message = "Профиль создан. Подтвердите тракт, затем явно подключите оба прибора."
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
        message = "Ожидайте ответы модели/прошивки и минимум 60 секунд / 20 полных совместимых свипов."
    }

    func saveBaseline(workspace: MonitoringWorkspaceModel) async {
        guard let client, owns(workspace), !busy, workspace.baselineConfirmed,
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
