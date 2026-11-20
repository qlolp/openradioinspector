import SwiftUI
import SpektrKomandy

struct NativeObservationActions: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let prepare: () -> Void
    let settings: (NativeConsoleSettings) -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            let action = NativeConsoleNextAction.resolve(coreReachable: model.coreReachable,
                projectSelected: !workspace.selected.isEmpty, statusCurrent: workspace.projectStatusIsCurrent(),
                armed: workspace.isArmed, writesBlocked: model.displayArchiveCapacity?.measurementWritesBlocked == true)
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(workspace.project["name"].text.isEmpty ? "Наблюдение" : workspace.project["name"].text)
                        .font(.headline).lineLimit(1)
                    Text(instruction(action)).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }.frame(maxWidth: .infinity, alignment: .leading)
                if action == .connection {
                    Button("Подключить ядро…") { settings(.connection) }
                        .buttonStyle(.borderedProminent)
                } else if action == .storage {
                    Button("Проверить хранение…") { settings(.guardSettings) }
                } else if action == .verifyStatus {
                    Button("Проверить состояние") { workspace.perform { await workspace.refreshStatus() } }
                        .disabled(workspace.pending)
                }
                Button(action == .events ? "Подготовка…" : "Подготовить наблюдение…", action: prepare)
                    .disabled(!model.connected)
                    .accessibilityIdentifier("preparation.open")
                    .help(model.connected ? "Проверка приборов, эталона и бюджета; охрана включается явно" : "Сначала подключите ядро в настройках")
                if workspace.isArmed {
                    Button("Снять с охраны") { workspace.perform { await workspace.command("disarm", success: "Охрана выключена. Приём управляется отдельно.") } }
                        .disabled(!model.coreReachable || !workspace.projectStatusIsCurrent() || workspace.pending)
                        .accessibilityIdentifier("observation.disarm")
                }
            }.padding(.horizontal, 12).padding(.vertical, 8)
                .background(.primary.opacity(0.035))
        }
    }

    private func instruction(_ action: NativeConsoleNextAction) -> String {
        switch action {
        case .connection: "Подключите ядро. При потере связи состояние охраны не подтверждено."
        case .preparation: "Проверьте приборы и эталон. Приём и охрана включаются отдельными действиями."
        case .verifyStatus: "Статус проекта устарел. Получите подтверждение ядра перед следующим действием."
        case .storage: "Запись измерений заблокирована. Проверьте доступность и свободное место архива."
        case .events: "Охрана включена. Выберите событие справа и проверьте его исходные данные."
        }
    }
}

struct NativeConsoleSettingsPane<Connection: View>: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @Binding var workspaceMode: String
    let initialSection: NativeConsoleSettings
    var access: NativeNodeAccess?
    @ViewBuilder let connection: () -> Connection
    @State private var section = NativeConsoleSettings.connection
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Настройки наблюдения").font(.title2.weight(.semibold))
                Spacer()
                Button("Готово") { dismiss() }.keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("settings.close")
            }
            Picker("Раздел настроек", selection: $section) {
                ForEach(NativeConsoleSettings.allCases) { Text($0.title).tag($0) }
            }.pickerStyle(.segmented).labelsHidden()
            switch section {
            case .connection: connection()
            case .guardSettings:
                Picker("Режим", selection: $workspaceMode) {
                    ForEach(NativeWorkspace.allCases) { Text($0.title).tag($0.rawValue) }
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 400)
                    .accessibilityIdentifier("workspace.mode-switch")
                MonitoringWorkspacePane(server: model.connected ? model.serverURL : "",
                    client: model.connected ? model.client : nil, sessionID: model.selectedSessionID,
                    workspace: workspace, mode: NativeWorkspace(rawValue: workspaceMode) ?? .monitoring,
                    onProjectSession: { model.selectSession($0) },
                    onResearch: { workspaceMode = NativeWorkspace.research.rawValue })
            case .signals:
                NativeSignalsPage(model: model, workspace: workspace,
                    openSpectrum: { dismiss() }, openGuard: { section = .guardSettings })
            case .receiver:
                ScrollView { RemoteArPane(model: model) }
                DisclosureGroup("Очередь и управление исследованиями") {
                    ScrollView { RemoteWorkflowPane(model: model) }.frame(maxHeight: 300)
                }
            }
            Spacer(minLength: 0)
            Text("Закрытие настроек не останавливает приём или охрану. Изменения применяются только явными командами.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(20).frame(minWidth: 900, idealWidth: 960, minHeight: 580, idealHeight: 650)
            .onAppear { section = initialSection }
            .sheet(item: Binding(get: { workspace.signalOriginal }, set: { _ in workspace.clearSignalOriginal() })) {
                NativeGuardOriginalPane(sweep: $0)
            }
            .sheet(isPresented: $model.showSerialForm) { RemoteSerialSessionPane(model: model) }
            .sheet(isPresented: $model.showFrequencyHistory) { NativeFrequencyHistoryPane(model: model) }
            .sheet(isPresented: $model.showTinySAControl) {
                RemoteTinySAControlPane(server: model.serverURL, sessionID: model.selectedSessionID,
                    streamID: model.analyzer?.id ?? "", client: model.client, access: access)
            }
    }
}
