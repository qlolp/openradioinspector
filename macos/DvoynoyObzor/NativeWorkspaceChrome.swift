import SwiftUI
import SpektrKomandy

enum NativeWorkspace: String, CaseIterable, Identifiable {
    case monitoring, research
    var id: String { rawValue }
    var title: String { self == .monitoring ? "Радиомониторинг" : "Исследование" }
    var symbol: String { self == .monitoring ? "waveform.path" : "clock.arrow.circlepath" }
}

struct NativeConnectionPane: View {
    @ObservedObject var model: RemoteDualModel
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
    @ObservedObject var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    private var coverage: String {
        let value = workspace.project["coverage"].text.isEmpty ? workspace.coverage : workspace.project["coverage"].text
        return ["wide": "Широкий обзор", "narrow": "Узкая полоса · широкий обзор приостановлен", "restoring": "Восстановление широкого обзора", "preparation": "Подготовка эталона", "stopped": "Покрытие остановлено", "paused": "Покрытие на паузе", "unavailable": "Покрытие недоступно", "error": "Ошибка покрытия"][value] ?? "Покрытие не подтверждено"
    }
    private var archiveLabel: String {
        guard let capacity = model.archiveCapacity else { return "Состояние архива неизвестно" }
        if capacity.measurementWritesBlocked { return "Запись в архив заблокирована" }
        let state = ["unlimited": "без лимита", "ok": "норма", "warning": "мало места", "full": "заполнен", "unavailable": "недоступен"][capacity.state.rawValue] ?? capacity.state.rawValue
        return "Архив · " + state
    }
    private func deviceState(_ value: String) -> String {
        ["connected": "подключён", "running": "работает", "stopped": "остановлен", "disconnected": "отключён", "error": "ошибка", "busy": "занят", "draft": "подготовка"][value] ?? value
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 16) {
                Label(workspace.isArmed ? "Охрана включена" : "Охрана не подтверждена", systemImage: workspace.isArmed ? "shield.fill" : "shield")
                    .foregroundStyle(workspace.isArmed ? Color.green : Color.secondary)
                Text(coverage)
                Spacer()
                if let capacity = model.archiveCapacity {
                    Label(archiveLabel, systemImage: "externaldrive")
                        .foregroundStyle(capacity.measurementWritesBlocked ? Color.red : Color.secondary)
                } else { Label("Состояние архива неизвестно", systemImage: "externaldrive.badge.questionmark") }
            }
            HStack {
                ForEach(model.session?.streams ?? [], id: \.exactID) { stream in
                    Text("\(stream.device) · \(deviceState(stream.state))")
                }
                Spacer()
                Text(model.eventStatus)
                if model.measurementSource != nil { Button("Исходное событие…") { model.exportMeasurementSource() } }
            }.font(.caption).foregroundStyle(.secondary)
            if let issue = model.measurementDisplayIssue { Text(issue.message).font(.caption).foregroundStyle(.orange) }
            if let notice = model.eventGapNotice {
                HStack {
                    Label(notice, systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
                    Button("Понятно") { model.eventGapNotice = nil }
                }.font(.caption)
            }
            Text(model.usesBundledLegacyHelper
                 ? "История и пауза графика не останавливают охрану. Вспомогательное ядро работает, пока запущено приложение."
                 : "Просмотр истории и пауза графика действуют только в этом клиенте. Охрана и запись продолжаются в ядре.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(.horizontal, 16).padding(.vertical, 8).background(Color.primary.opacity(0.04))
    }
}
