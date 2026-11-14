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
    private var acquisition: String {
        guard model.coreReachable else { return "Не подтверждён" }
        let active = model.workers.filter { $0.alive }
        if active.contains(where: { $0.stopping == true }) { return "Завершение свипа" }
        if active.contains(where: { $0.state == "degraded" || $0.state == "error" }) { return "Есть ошибки" }
        return active.isEmpty ? "Остановлен" : "Ведётся"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 145), spacing: 8)], spacing: 8) {
                NativeOperatorMetric(title: "Ядро", value: model.coreReachable ? "На связи" : "Нет связи", symbol: "network", tint: model.coreReachable ? .green : .orange)
                NativeOperatorMetric(title: "Приём", value: acquisition, symbol: "antenna.radiowaves.left.and.right", tint: .primary)
                NativeOperatorMetric(title: "Хранилище", value: model.coreReachable ? archiveLabel : "Не подтверждено", symbol: "externaldrive", tint: model.archiveCapacity?.measurementWritesBlocked == true ? .red : .primary)
                NativeOperatorMetric(title: "Охрана", value: model.coreReachable && workspace.isArmed ? "Включена" : "Не подтверждена", symbol: "shield", tint: model.coreReachable && workspace.isArmed ? .green : .secondary)
            }
            if model.archiveCapacity?.measurementWritesBlocked == true {
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
            DisclosureGroup("Покрытие и доставка данных") {
                VStack(alignment: .leading, spacing: 5) {
                    Text(coverage)
                    Text(model.eventStatus).textSelection(.enabled)
                    if model.measurementSource != nil { Button("Исходное событие…") { model.exportMeasurementSource() } }
                    Text("Пауза отображения не управляет охраной или записью. Состояние ядра показано отдельно.")
                }.font(.caption).foregroundStyle(.secondary)
            }.font(.caption)
        }.padding(.horizontal, 16).padding(.vertical, 8)
    }
}
