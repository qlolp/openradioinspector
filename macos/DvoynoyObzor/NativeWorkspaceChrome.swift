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
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Двойной обзор").font(.title2.weight(.semibold))
                Spacer()
                Label(model.connected ? "Ядро подключено" : "Нет подключения", systemImage: model.connected ? "network" : "network.slash")
                    .foregroundStyle(model.connected ? Color.green : Color.secondary)
            }
            HStack {
                TextField("Адрес ядра", text: $model.serverURL).textFieldStyle(.roundedBorder).disabled(model.connected)
                if model.connected {
                    Button("Отключить UI") { model.disconnect() }
                    Button("Обновить") { Task { await model.reloadSessions() } }
                } else { Button("Подключить") { model.connect() } }
                Picker("Сеанс", selection: Binding(get: { model.selectedSessionID }, set: model.selectSession).exactID) {
                    Text("Не выбран").tag(PlatformExactID(""))
                    ForEach(model.sessions, id: \.exactID) { Text("\($0.name) · \($0.state)").tag($0.exactID) }
                }.disabled(!model.connected || model.busy)
            }
            if model.usesBundledLegacyHelper {
                Label("Локальное вспомогательное ядро: завершается вместе с приложением. Для автономной охраны подключите отдельную службу ядра.", systemImage: "desktopcomputer")
                    .font(.caption).foregroundStyle(.secondary)
            }
            DisclosureGroup("Сеанс и приборы") {
                HStack {
                    Button("Новый draft") { model.createSession(simulated: false) }
                    Button("Новый с симуляторами") { model.createSession(simulated: true) }
                    Button("Новый serial…") { model.showSerialForm = true }
                    Spacer()
                    Button("tinySA · lab / экран") { model.showTinySAControl = true }.disabled(model.analyzer == nil)
                }.disabled(!model.connected || model.busy)
                HStack {
                    Button("Запустить сеанс") { model.startSession() }
                    Button("Остановить сеанс") { model.stopSession() }
                    Button(model.session?.streams.count == 1 ? "Запустить поток" : "Запустить оба потока") { model.startWorkers() }
                    Button(model.session?.streams.count == 1 ? "Остановить поток" : "Остановить оба потока") { model.stopWorkers() }
                }.disabled(model.session == nil || model.busy)
                Text(model.status).font(.caption).foregroundStyle(.secondary)
            }
        }.padding(.horizontal, 16).padding(.vertical, 10)
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
            }.font(.caption).foregroundStyle(.secondary)
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
