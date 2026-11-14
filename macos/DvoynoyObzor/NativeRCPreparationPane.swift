import SwiftUI
import SpektrKomandy

struct NativeRCPreparationPane: View {
    @ObservedObject var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @StateObject private var flow = NativeRCPreparationModel()
    @State private var ports: [String] = []
    @Environment(\.dismiss) private var dismiss
    private var readiness: NativeRCReadiness { .init(preflight: workspace.preflight) }
    private var ownsProject: Bool { flow.owns(workspace) }
    private var busy: Bool { flow.busy || workspace.pending || !workspace.operationsPending.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Подготовка tinySA Ultra + AR-DV1").font(.title2.weight(.semibold))
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Ядро владеет USB-портами и архивом. Открытие мастера не подключает приборы и не останавливает охрану.")
                .font(.callout).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    profile
                    if ownsProject {
                        connection
                        baseline
                        acceptance
                    }
                    if busy { ProgressView("Ожидание подтверждения ядра…").controlSize(.small) }
                    Text(flow.message).font(.callout)
                    ErrorLine(text: flow.error)
                    ErrorLine(text: workspace.error)
                    ForEach(workspace.operationsErrors.keys.sorted(), id: \.self) { ErrorLine(text: workspace.operationsErrors[$0]) }
                }.padding(4)
            }
            Text("Закрытие мастера отключает только его опрос. Приём и охрана продолжаются; остановка выполняется отдельными кнопками. Аудиозахват и Python/DuckDB в RC1 недоступны.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20).frame(minWidth: 740, idealWidth: 800, minHeight: 600, idealHeight: 720)
        .task(id: model.connectionGeneration) {
            flow.configure(server: model.connected ? model.serverURL : "", client: model.connected ? model.client : nil, workspace: workspace)
            ports = Array(SerialPort.availablePaths().prefix(256))
            while !Task.isCancelled, model.connected {
                if ownsProject { await workspace.refreshStatus(); await workspace.checkPreflight() }
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
        .onDisappear { flow.cancel() }
    }

    private var profile: some View {
        GroupBox("1 · Объект, порты и диапазон") {
            VStack(alignment: .leading, spacing: 8) {
                HStack { TextField("Название проекта", text: $flow.draft.name); TextField("Объект", text: $flow.draft.site) }
                port("tinySA Ultra", selection: $flow.draft.tinySAPort)
                port("AOR AR-DV1", selection: $flow.draft.receiverPort)
                HStack {
                    TextField("Начало, Гц", text: $flow.draft.startHz)
                    TextField("Конец, Гц", text: $flow.draft.stopHz)
                    Stepper("\(flow.draft.points) точек", value: $flow.draft.points, in: 2...290).frame(width: 150)
                    Picker("RBW", selection: $flow.draft.rbwHz) { Text("10 кГц").tag(10_000); Text("30 кГц").tag(30_000) }.frame(width: 150)
                }.monospacedDigit()
                Text("Приёмный профиль: 0,1–800 МГц. Предложенный порт ещё не подтверждает модель. При подключении ядро проверит прошивку; несовместимый прибор будет отклонён.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Создать профиль и проект") {
                    Task { await flow.create(workspace: workspace) { id in model.selectSession(id); Task { await model.reloadSessions() } } }
                }.disabled(!model.connected || busy || workspace.isArmed || (try? flow.draft.validated()) == nil)
            }.textFieldStyle(.roundedBorder)
                .disabled(ownsProject)
            if ownsProject {
                HStack {
                    Text("Проект: \(workspace.project["name"].text). Новый диапазон требует нового эталона.").font(.caption)
                    Spacer()
                    Button("Новый профиль") { Task { await flow.newProfile(workspace: workspace) } }.disabled(busy || workspace.isArmed)
                }
            }
        }
    }
    private func port(_ name: String, selection: Binding<String>) -> some View {
        HStack {
            Text(name).frame(width: 120, alignment: .leading)
            TextField("/dev/cu.…", text: selection)
            Menu("Выбрать порт") {
                ForEach(ports, id: \.self) { path in Button(path) { selection.wrappedValue = path } }
                Button("Обновить список") { ports = Array(SerialPort.availablePaths().prefix(256)) }
            }
        }
    }
    private var connection: some View {
        GroupBox("2 · Приёмный тракт и подключение") {
            VStack(alignment: .leading, spacing: 8) {
                TextField("Антенна, кабель, тракт и расположение", text: $workspace.antennaNotes, axis: .vertical).lineLimit(2...4).disabled(!workspace.canEditPreparation)
                Toggle("Антенна и приёмный тракт проверены мною", isOn: $workspace.receivePathConfirmed).disabled(!workspace.canEditPreparation)
                Toggle("Расположение и обычная обстановка подтверждены мною", isOn: $workspace.locationConfirmed).disabled(!workspace.canEditPreparation)
                HStack {
                    Button("Подключить приборы и собрать эталон") { Task { await flow.connect(workspace: workspace) } }
                        .disabled(busy || !workspace.canEditPreparation || !workspace.preparationInputValid || !workspace.receivePathConfirmed || !workspace.locationConfirmed)
                    Button("Остановить сбор") { workspace.perform { await workspace.stopPreparation() } }
                        .disabled(busy || !workspace.isCollecting || workspace.isArmed)
                }
                Text("Генератор tinySA выключается при подготовке, затем подтверждается mode input. Это подтверждение команд, а не физический RF-readback.")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Array(workspace.preflight["workers"].items.enumerated()), id: \.offset) { _, worker in
                    HStack {
                        Text(worker["device"].text.isEmpty ? worker["stream_id"].text : worker["device"].text)
                        Text(worker["state"].text).foregroundStyle(.secondary)
                        if !worker["current"]["receiver_firmware"].text.isEmpty { Text("прошивка \(worker["current"]["receiver_firmware"].text)") }
                    }.font(.caption)
                    if !worker["last_error"].text.isEmpty { ErrorLine(text: worker["last_error"].text) }
                }
                if let sweep = model.spectrumPresentation.sweeps.last,
                   PlatformExactID.same(sweep.sessionId, workspace.project["active_session_id"].text),
                   case .object(let identity)? = sweep.settings?["device_identity"], case .string(let firmware)? = identity["firmware"] {
                    Text("tinySA · прошивка из исходного свипа: \(firmware)").font(.caption)
                }
                Text("Среднее время свипа: \(workspace.preflight["performance"]["mean_sweep_seconds"].scalar) с · период обзора: \(workspace.preflight["performance"]["mean_observed_cycle_seconds"].scalar) с")
                    .font(.caption).monospacedDigit()
            }.disabled(workspace.isArmed)
        }
    }
    private var baseline: some View {
        let collection = workspace.preflight["baseline_collection"]
        return GroupBox("3 · Эталон и бюджет восьми часов") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Совместимые полные свипы: \(collection["sweeps"].scalar) / 20 · интервал: \(collection["elapsed_seconds"].scalar) / 60 с").monospacedDigit()
                ProgressView(value: min(60, max(0, collection["elapsed_seconds"].double ?? 0)), total: 60)
                Toggle("Подтверждаю пригодность эталона обычной обстановки", isOn: $workspace.baselineConfirmed)
                Button("Зафиксировать эталон и включить резерв backup") { Task { await flow.saveBaseline(workspace: workspace) } }
                    .disabled(busy || workspace.isArmed || !readiness.baselineCollected || !workspace.baselineConfirmed || !workspace.savedPreparationReady || workspace.preparationDirty)
                let storage = workspace.preflight["storage"]
                Text("Поток архива: \(storage["bytes_per_second"].scalar) байт/с · резерв будущей копии: \(storage["backup_reserve_bytes"].scalar) байт").font(.caption).monospacedDigit()
                Text(readiness.budgetReady ? "Бюджет подтверждён ядром: 8 часов + 50%, место для будущей backup-копии и служебный резерв." : "Бюджет ещё не подтверждён. Нужны измеренная скорость записи, запас 50% и место для будущей backup-копии.")
                    .font(.callout).foregroundStyle(readiness.budgetReady ? Color.green : Color.secondary)
                Text("Исходники не удаляются автоматически. Несовместимые настройки требуют нового эталона.").font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private var acceptance: some View {
        GroupBox("4 · Постановка на охрану") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(workspace.preflight["checks"].items.enumerated()), id: \.offset) { _, check in
                    Label(check["detail"].text, systemImage: check["ok"].bool == true ? "checkmark.circle" : "exclamationmark.circle").font(.caption)
                }
                HStack {
                    Button("Поставить на охрану") { workspace.perform { await workspace.command("arm", success: "Охрана включена.") } }
                        .buttonStyle(.borderedProminent)
                        .disabled(busy || !workspace.canArm || workspace.preflight["ready"].bool != true || !readiness.budgetReady)
                    Button("Снять с охраны") { workspace.perform { await workspace.command("disarm", success: "Охрана выключена.") } }
                        .disabled(busy || !workspace.isArmed)
                    Text(workspace.isArmed ? "Охрана работает независимо от окна." : "Автопостановка выключена.").font(.caption)
                }
                Text("Аппаратная приёмка остаётся отдельной: 30 минут нагрузки и 8 часов с приборами на фактической macOS 27.3 ещё требуются.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
