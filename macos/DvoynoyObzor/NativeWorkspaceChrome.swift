import SwiftUI
import SpektrKomandy

enum NativeWorkspace: String, CaseIterable, Identifiable {
    case monitoring, research
    var id: String { rawValue }
    var title: String { self == .monitoring ? "Радиомониторинг" : "Исследование" }
    var symbol: String { self == .monitoring ? "waveform.path" : "clock.arrow.circlepath" }
}

struct NativeConnectionPane: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var connection: NativeNodeConnectionModel
    var localServiceRequired = false
    var localServiceReady = true
    var onLocalConnect: (() -> Void)?
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Двойной обзор").font(.title2.weight(.semibold))
                Spacer()
                Label(model.coreReachable ? "Ядро доступно" : (model.connected ? "Связь с ядром потеряна" : "Нет подключения"), systemImage: model.coreReachable ? "network" : "network.slash")
                    .foregroundStyle(model.coreReachable ? Color.green : Color.secondary)
            }
            HStack {
                TextField("Адрес ядра", text: $model.serverURL).textFieldStyle(.roundedBorder).disabled(localServiceRequired || model.connected || connection.isBusy)
                if model.connected {
                    Button("Отключить интерфейс") { connection.cancel(); model.disconnect() }
                    Button("Обновить") { Task { await model.reloadSessions() } }
                } else {
                    Button("Подключить") {
                        if localServiceRequired { onLocalConnect?() }
                        else {
                            let endpoint = model.serverURL
                            Task { accept(await connection.preflight(server: endpoint)) }
                        }
                    }.disabled(connection.isBusy || !localServiceReady)
                }
                Picker("Сеанс", selection: Binding(get: { model.selectedSessionID }, set: model.selectSession).exactID) {
                    Text("Не выбран").tag(PlatformExactID(""))
                    ForEach(model.sessions, id: \.exactID) { Text("\($0.name) · \($0.state)").tag($0.exactID) }
                }.disabled(!model.connected || model.busy)
            }
            if !localServiceRequired { securityControls }
            ErrorLine(text: model.errorText)
            if model.usesBundledLegacyHelper {
                Label("Локальное вспомогательное ядро: завершается вместе с приложением. Для автономной охраны подключите отдельную службу ядра.", systemImage: "desktopcomputer")
                    .font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Сеанс и приборы") {
                HStack {
                    Button("Пустой сеанс") { model.createSession(simulated: false) }
                    Button("Учебный сеанс") { model.createSession(simulated: true) }
                    Button("Подключить приборы…") { model.showSerialForm = true }
                    Spacer()
                    Button("Экран и управление tinySA") { model.showTinySAControl = true }.disabled(model.analyzer == nil)
                }.disabled(!model.connected || model.busy)
                HStack {
                    Button("Запустить сеанс") { model.startSession() }
                    Button("Остановить сеанс") { model.stopSession() }
                    Button(model.session?.streams.count == 1 ? "Начать приём" : "Начать приём на обоих") { model.startWorkers() }
                    Button(model.session?.streams.count == 1 ? "Завершить приём" : "Завершить приём на обоих") { model.stopWorkers() }
                }.disabled(model.session == nil || model.busy)
                Text(model.status).font(.caption).foregroundStyle(.secondary)
            }
        }.padding(.horizontal, 16).padding(.vertical, 10)
            .onChange(of: PlatformExactID(model.serverURL)) { _, _ in if !localServiceRequired { connection.cancel() } }
            .onChange(of: model.authorizationRequired) { _, expired in
                if expired { connection.authenticationExpired(server: model.serverURL) }
            }
    }

    @ViewBuilder private var securityControls: some View {
        if connection.isBusy {
            HStack {
                ProgressView("Проверка доступа к узлу…").controlSize(.small)
                Button("Отмена") { connection.cancel() }
            }
        }
        if connection.phase == .pairingRequired {
            Text("Установите CA узла из доверенного источника в системную связку ключей. Оператор открывает короткое окно сопряжения на ядре; приложение не открывает его автоматически.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                SecureField("Код сопряжения", text: $connection.pairingCode).frame(maxWidth: 220)
                TextField("Имя этого клиента", text: $connection.label).frame(maxWidth: 220)
                Button("Сопрячь этот Mac") {
                    let endpoint = model.serverURL
                    Task { accept(await connection.pair(server: endpoint)) }
                }
                    .disabled(connection.pairingCode.isEmpty || connection.label.isEmpty)
            }.textFieldStyle(.roundedBorder)
        }
        if connection.phase == .legacyConfirmation {
            Button("Подключиться к старому API без сопряжения") { accept(connection.confirmLegacy(server: model.serverURL)) }
        }
        HStack {
            if !connection.message.isEmpty {
                Text(connection.message).font(.caption).foregroundStyle(connection.phase == .failed ? Color.red : Color.secondary)
            }
            Spacer()
            if connection.authorizedNode?.access == .pairedTLS, model.connected {
                Button("Отозвать это сопряжение", role: .destructive) {
                    let endpoint = model.serverURL
                    let generation = model.connectionGeneration
                    Task {
                        if await connection.revokeCurrent(server: endpoint) {
                            model.disconnectIfCurrent(connection: generation, server: endpoint)
                        }
                    }
                }.disabled(connection.isBusy)
            }
            if connection.phase != .idle {
                Button("Забыть на этом Mac") {
                    let endpoint = model.serverURL
                    connection.cancel()
                    model.disconnect()
                    Task { _ = await connection.forget(server: endpoint) }
                }.disabled(connection.isBusy)
            }
        }
    }
    private func accept(_ node: NativeAuthorizedNode?) {
        guard let node, connection.accepts(node), node.server == PlatformExactID(model.serverURL) else { return }
        model.connect(using: node.client)
    }
}

struct NativeContinuityBar: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @State private var details = false
    @State private var showBands = false
    private func coverage(verified: Bool) -> String {
        let value = workspace.project["coverage"].text.isEmpty ? workspace.coverage : workspace.project["coverage"].text
        return NativeContinuityLabels.coverage(value, coreReachable: model.coreReachable,
            statusVerified: verified)
    }
    private var archiveLabel: String {
        guard model.coreReachable else { return "Архив · состояние не подтверждено" }
        guard let capacity = model.displayArchiveCapacity else { return "Состояние архива неизвестно" }
        if capacity.measurementWritesBlocked { return "Запись в архив заблокирована" }
        let state = ["unlimited": "без лимита", "ok": "норма", "warning": "мало места", "full": "заполнен", "unavailable": "недоступен"][capacity.state.rawValue] ?? capacity.state.rawValue
        return "Архив · " + state
    }
    private var recordingLabel: String {
        guard model.coreReachable, let capacity = model.displayArchiveCapacity else { return "Запись не подтверждена" }
        if capacity.measurementWritesBlocked { return "Запись заблокирована" }
        let workers = [model.analyzer?.id, model.receiver?.id].compactMap { model.worker(for: $0) }
        guard !workers.isEmpty else { return "Запись не подтверждена" }
        let active = workers.filter(\.alive)
        guard !active.isEmpty else { return "Запись остановлена" }
        if active.contains(where: { $0.state == "running" && ($0.measurements ?? 0) > 0 }) { return "Запись идёт" }
        if active.allSatisfy({ $0.state == "running" && $0.measurements == 0 }) { return "Ожидается первая запись" }
        return "Запись не подтверждена"
    }
    private func activity(_ stream: String?) -> String {
        guard let stream else { return "не выбран" }
        guard model.coreReachable else { return "связь потеряна" }
        guard let worker = model.worker(for: stream) else { return "приём не подтверждён" }
        if worker.stopping == true { return "останавливается" }
        if worker.state == "degraded" || worker.state == "error" { return "ошибка приёма" }
        return worker.alive ? "приём идёт" : "остановлен"
    }
    private func guardLabel(verified: Bool) -> String {
        NativeContinuityLabels.guardState(workspace.project["state"].text,
            coreReachable: model.coreReachable, selected: !workspace.selected.isEmpty,
            statusVerified: verified, armed: workspace.isArmed)
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let verified = workspace.projectStatusIsCurrent()
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 16) {
                    HStack(spacing: 4) {
                        Image(systemName: "network").accessibilityHidden(true)
                        Text(model.coreReachable ? "Ядро на связи" : "Нет связи с ядром")
                            .accessibilityIdentifier("continuity.core")
                    }
                        .foregroundStyle(model.coreReachable ? Color.green : Color.orange)
                    Text(recordingLabel)
                        .accessibilityIdentifier("continuity.recording")
                    Text(archiveLabel)
                        .accessibilityIdentifier("continuity.archive")
                    Text(guardLabel(verified: verified))
                        .accessibilityIdentifier("continuity.guard")
                    Spacer(minLength: 0)
                    Button("Диагностика") { details.toggle() }.controlSize(.mini)
                        .popover(isPresented: $details) {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(coverage(verified: verified))
                                Text(model.eventStatus).textSelection(.enabled)
                                if model.measurementSource != nil { Button("Исходное событие…") { model.exportMeasurementSource() } }
                            }.padding().frame(width: 380)
                        }
                }.font(.caption).lineLimit(1)
                HStack(spacing: 16) {
                    Text("tinySA: " + activity(model.analyzer?.id))
                        .accessibilityIdentifier("continuity.analyzer")
                    Text("AR-DV1: " + activity(model.receiver?.id))
                        .accessibilityIdentifier("continuity.receiver")
                    Text(coverage(verified: verified)).help(coverage(verified: verified))
                        .accessibilityIdentifier("continuity.coverage")
                    Button("По полосам") { showBands.toggle() }
                        .controlSize(.mini).accessibilityIdentifier("continuity.bands")
                        .popover(isPresented: $showBands) {
                            ScrollView {
                                NativeBandCoveragePane(status: workspace.status, sessionID: model.selectedSessionID,
                                                       verified: model.coreReachable && verified)
                                    .padding(14).frame(maxWidth: .infinity, alignment: .leading)
                            }.frame(width: 410, height: 280)
                        }
                    Spacer(minLength: 0)
                    if let time = model.acquisitionSweepTime {
                        let age = context.date.timeIntervalSince(time)
                        Text(age >= 0 ? "Последний свип: \(Int(min(age, Double(Int.max / 2)))) с назад" : "Последний свип: время в будущем")
                    } else {
                        Text("Последний свип: нет данных")
                    }
                }.font(.caption).lineLimit(1)
                if model.displayArchiveCapacity?.measurementWritesBlocked == true {
                    Label("Запись измерений заблокирована. Проверьте хранилище.", systemImage: "exclamationmark.octagon.fill")
                        .font(.callout).foregroundStyle(.red)
                }
                if let issue = model.measurementDisplayIssue { Text(issue.message).font(.caption).foregroundStyle(.orange) }
                if let notice = model.eventGapNotice {
                    HStack {
                        Label(notice, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                        Button("Понятно") { model.eventGapNotice = nil }
                    }.font(.caption)
                }
            }.padding(.horizontal, 10).padding(.vertical, 3)
        }
    }
}
