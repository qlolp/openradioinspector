import SwiftUI
import SpektrKomandy

struct NativeRCPreparationPane: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @StateObject private var flow = NativeRCPreparationModel()
    @State private var ports: [String] = []
    @State private var stage = 0
    @State private var startMHz = "88"
    @State private var stopMHz = "108"
    private let stages = ["Проект и приборы", "Подключение", "Эталон", "Готовность"]
    @Environment(\.dismiss) private var dismiss
    private var verified: Bool { model.coreReachable && workspace.preflightIsCurrent() }
    private var readiness: NativeRCReadiness { .init(preflight: verified ? workspace.preflight : .null) }
    private var ownsProject: Bool { flow.owns(workspace) }
    private var busy: Bool { flow.busy || workspace.pending || !workspace.operationsPending.isEmpty }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in content }
    }

    private var content: some View {
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
                    HStack {
                        ForEach(stages.indices, id: \.self) { index in
                            Button("\(index + 1) · \(stages[index])") { stage = index }
                                .disabled(index > 0 && !ownsProject)
                                .buttonStyle(.bordered)
                                .tint(stage == index ? Color.accentColor : Color.secondary)
                                .accessibilityIdentifier("preparation.step.\(index)")
                        }
                    }
                    if stage == 0 { profile }
                    if ownsProject {
                        if stage == 1 { connection }
                        if stage == 2 { baseline }
                        if stage == 3 { acceptance }
                    }
                    if stage > 0 {
                        HStack {
                            Button("Назад") { stage -= 1 }
                            Spacer()
                            if stage < 3 { Button("Далее") { stage += 1 } }
                        }
                    }
                    if busy { ProgressView("Ожидание подтверждения ядра…").controlSize(.small) }
                    Text(flow.message).font(.callout)
                    ErrorLine(text: flow.error).accessibilityIdentifier("preparation.error")
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
            startMHz = NativeAnalyzerDraft.mhz(Int64(flow.draft.startHz) ?? 88_000_000)
            stopMHz = NativeAnalyzerDraft.mhz(Int64(flow.draft.stopHz) ?? 108_000_000)
            if ownsProject { stage = workspace.isArmed ? 3 : 1 }
            while !Task.isCancelled, model.connected {
                if ownsProject { await workspace.refreshStatus(); await workspace.checkPreflight() }
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
        .onChange(of: ownsProject) { _, owns in if owns { stage = workspace.isArmed ? 3 : 1 } else { stage = 0 } }
        .onChange(of: startMHz) { _, value in flow.draft.startHz = NativeConsoleFrequency.hertz(value) ?? "" }
        .onChange(of: stopMHz) { _, value in flow.draft.stopHz = NativeConsoleFrequency.hertz(value) ?? "" }
        .onDisappear { flow.cancel() }
    }

    private var validationReason: String? {
        do { _ = try flow.draft.validated(); return nil }
        catch { return error.localizedDescription }
    }

    private var profile: some View {
        GroupBox("1 · Объект, порты и диапазон") {
            VStack(alignment: .leading, spacing: 8) {
                HStack { TextField("Название проекта", text: $flow.draft.name); TextField("Объект", text: $flow.draft.site) }
                port("tinySA Ultra", selection: $flow.draft.tinySAPort, identifier: "preparation.tinysa-port")
                port("AOR AR-DV1", selection: $flow.draft.receiverPort, identifier: "preparation.receiver-port")
                HStack {
                    TextField("Начало · МГц", text: $startMHz)
                        .accessibilityIdentifier("preparation.start-mhz")
                    TextField("Конец · МГц", text: $stopMHz)
                        .accessibilityIdentifier("preparation.stop-mhz")
                    Stepper("\(flow.draft.points) точек", value: $flow.draft.points, in: 2...290).frame(width: 150)
                    Picker("RBW", selection: $flow.draft.rbwHz) { Text("10 кГц").tag(10_000); Text("30 кГц").tag(30_000) }.frame(width: 150)
                }.monospacedDigit()
                Text("Приёмный профиль: 0,1–800 МГц. Предложенный порт ещё не подтверждает модель. При подключении ядро проверит прошивку; несовместимый прибор будет отклонён.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Частоты в МГц: точка или запятая, до шести знаков после разделителя. В ядро передаются целые Гц.")
                    .font(.caption).foregroundStyle(.secondary)
                if let reason = validationReason { Text(reason).font(.caption).foregroundStyle(.secondary) }
                Button("Создать профиль и проект") {
                    Task { await flow.create(workspace: workspace) { id in model.selectSession(id); Task { await model.reloadSessions() } } }
                }.disabled(!model.connected || busy || workspace.isArmed || (try? flow.draft.validated()) == nil)
                    .accessibilityIdentifier("preparation.create")
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
    private func port(_ name: String, selection: Binding<String>, identifier: String) -> some View {
        HStack {
            Text(name).frame(width: 120, alignment: .leading)
            TextField("/dev/cu.…", text: selection).accessibilityIdentifier(identifier)
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
                    Button("Подключить приборы и собрать эталон") {
                        Task { await flow.connect(workspace: workspace); if workspace.isCollecting { stage = 2 } }
                    }
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
        let progress = NativeBaselineProgress(preflight: verified ? workspace.preflight : .null)
        return GroupBox("3 · Эталон и бюджет восьми часов") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Совместимые полные свипы: \(collection["sweeps"].scalar) / 20 · интервал: \(collection["elapsed_seconds"].scalar) / 60 с").monospacedDigit()
                ProgressView("Время эталона · минимум 60 секунд", value: progress.timeFraction)
                    .accessibilityIdentifier("preparation.baseline.time")
                ProgressView("Полные совместимые свипы · минимум 20", value: progress.sweepFraction)
                    .accessibilityIdentifier("preparation.baseline.sweeps")
                if let remaining = progress.estimatedRemainingSeconds, !progress.ready {
                    Text("Оценка оставшегося времени: \(Int(min(remaining.rounded(.up), Double(Int.max / 2)))) с. Зависит от фактического периода обзора.")
                        .font(.caption).foregroundStyle(.secondary)
                } else if !progress.ready {
                    Text("Ожидаются измеренный период обзора и подтверждение совместимости ядром. Одной минуты недостаточно без 20 полных свипов.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Toggle("Подтверждаю пригодность эталона обычной обстановки", isOn: $workspace.baselineConfirmed)
                Button("Подтвердить эталон и резерв копии") {
                    Task { await flow.saveBaseline(workspace: workspace); if readiness.budgetReady { stage = 3 } }
                }
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
                if verified {
                    ForEach(Array(workspace.preflight["checks"].items.enumerated()), id: \.offset) { _, check in
                        Label(check["detail"].text, systemImage: check["ok"].bool == true ? "checkmark.circle" : "exclamationmark.circle").font(.caption)
                    }
                } else {
                    Label("Готовность не подтверждена. Обновите состояние ядра и проекта.", systemImage: "exclamationmark.circle")
                    Button("Обновить готовность") { workspace.perform { await workspace.refreshStatus(); await workspace.checkPreflight() } }
                        .disabled(!model.coreReachable || busy)
                }
                HStack {
                    Button("Поставить на охрану") { guard verified else { return }; workspace.perform { await workspace.command("arm", success: "Охрана включена.") } }
                        .buttonStyle(.borderedProminent)
                        .disabled(!verified || busy || !workspace.canArm || workspace.preflight["ready"].bool != true || !readiness.budgetReady)
                        .accessibilityIdentifier("preparation.arm")
                    Button("Снять с охраны") { workspace.perform { await workspace.command("disarm", success: "Охрана выключена.") } }
                        .disabled(!model.coreReachable || !workspace.projectStatusIsCurrent() || busy || !workspace.isArmed)
                    Text(!workspace.projectStatusIsCurrent() ? "Состояние охраны не подтверждено." : (workspace.isArmed ? "Охрана работает независимо от окна." : "Автопостановка выключена.")).font(.caption)
                }
                Text("Аппаратная приёмка остаётся отдельной: 30 минут нагрузки и 8 часов с приборами на фактической macOS 27.3 ещё требуются.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
