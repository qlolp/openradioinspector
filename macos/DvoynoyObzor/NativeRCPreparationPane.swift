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
    private let stages = ["Задание", "Пробный приём", "Эталон", "Дежурство"]
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
                Text(ownsProject ? "Подготовка дежурства" : "Первое дежурство").font(.title2.weight(.semibold))
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Узел приёма · " + (model.client?.baseURL.root.absoluteString ?? model.serverURL))
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
            Text("Ядро продолжает дежурство после закрытия окна.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(20).frame(minWidth: 740, idealWidth: 800, minHeight: 600, idealHeight: 720)
        .task(id: model.connectionGeneration) {
            flow.configure(server: model.connected ? model.serverURL : "", client: model.connected ? model.client : nil, workspace: workspace, sessionID: model.selectedSessionID)
            ports = localPorts
            await flow.readLocation()
            startMHz = NativeAnalyzerDraft.mhz(Int64(flow.draft.startHz) ?? 88_000_000)
            stopMHz = NativeAnalyzerDraft.mhz(Int64(flow.draft.stopHz) ?? 108_000_000)
            if ownsProject { stage = (workspace.isArmed || workspace.dutyEnabled) ? 3 : 1 }
            while !Task.isCancelled, model.connected {
                if ownsProject { await workspace.refreshStatus(); await workspace.checkPreflight() }
                do { try await Task.sleep(for: .seconds(2)) } catch { return }
            }
        }
        .onChange(of: ownsProject) { _, owns in if owns { stage = (workspace.isArmed || workspace.dutyEnabled) ? 3 : 1 } else { stage = 0 } }
        .onChange(of: startMHz) { _, value in flow.draft.startHz = (try? NativeHistoryFrequencyInput.hz(value)).map(String.init) ?? "" }
        .onChange(of: stopMHz) { _, value in flow.draft.stopHz = (try? NativeHistoryFrequencyInput.hz(value)).map(String.init) ?? "" }
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
                Picker("Основной прибор", selection: $flow.draft.source) {
                    ForEach(NativeDutySource.allCases, id: \.self) { Text($0.title).tag($0) }
                }.pickerStyle(.segmented).accessibilityIdentifier("duty.primary")
                if flow.draft.source == .tinySA {
                    port("tinySA Ultra", selection: $flow.draft.tinySAPort, identifier: "preparation.tinysa-port")
                }
                if flow.draft.source == .rfExplorer {
                    port("RF Explorer", selection: $flow.draft.explorer.port, identifier: "duty.rf-explorer-port")
                }
                port(flow.draft.source == .ardv1 ? "AOR AR-DV1" : "AR-DV1 · опция",
                     selection: $flow.draft.receiverPort, identifier: "preparation.receiver-port")
                if flow.draft.source != .ardv1 {
                    Text("Оставьте порт AR-DV1 пустым для дежурства с одним анализатором.").font(.caption)
                }
                if flow.draft.source == .rfExplorer {
                    Picker("План RF Explorer", selection: $flow.draft.explorer.mode) {
                        Text("Окно прибора").tag(NativeRFExplorerDraft.Mode.instrument)
                        Text("Заданное окно").tag(NativeRFExplorerDraft.Mode.window)
                        Text("Автообзор 10 кГц").tag(NativeRFExplorerDraft.Mode.survey)
                    }
                    if flow.draft.explorer.mode == .survey {
                        Toggle("Задать границы вместо всей активной платы", isOn: $flow.draft.explorer.customSurveyRange)
                    }
                    NativeRFExplorerRangeFields(draft: $flow.draft.explorer)
                    Text("Для эталона выберите на приборе детектор Normal. Пробный свип покажет фактическую плату, вход и режим.").font(.caption)
                } else {
                    HStack {
                        LabeledContent("Начало, МГц") { TextField("МГц", text: $startMHz).accessibilityIdentifier("preparation.start-mhz") }
                        LabeledContent("Конец, МГц") { TextField("МГц", text: $stopMHz).accessibilityIdentifier("preparation.stop-mhz") }
                        if flow.draft.source == .tinySA {
                            Stepper("\(flow.draft.points) точек", value: $flow.draft.points, in: 2...290).frame(width: 140)
                            Picker("RBW", selection: $flow.draft.rbwHz) { Text("10 кГц").tag(10_000); Text("30 кГц").tag(30_000) }
                        } else {
                            LabeledContent("Шаг, Гц") { TextField("10000", text: $flow.draft.stepHz).frame(width: 80) }
                        }
                    }.monospacedDigit()
                    if flow.draft.source == .ardv1 {
                        HStack {
                            Button("0,1–1300 МГц") { startMHz = "0,1"; stopMHz = "1300"; flow.draft.stepHz = "10000" }
                            Button("100–400 МГц") { startMHz = "100"; stopMHz = "400"; flow.draft.stepHz = "10000" }
                            if let grid = try? flow.draft.receiverGrid() { Text("\(grid.pointCount) точек за проход").monospacedDigit() }
                        }
                        Text("Последовательный FM-приём с проверкой RX. Период возврата рассчитывается по архивным наблюдениям.").font(.caption)
                    }
                }
                nodeStorage
                if let reason = validationReason { Text(reason).font(.caption).foregroundStyle(.secondary) }
                Button("Создать профиль и проект") {
                    Task { await flow.create(workspace: workspace) { id in model.selectSession(id); Task { await model.reloadSessions() } } }
                }.disabled(!model.connected || busy || workspace.isArmed || workspace.dutyEnabled || (try? flow.draft.validated()) == nil)
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
            TextField("Порт на узле ядра", text: selection).accessibilityIdentifier(identifier)
            if localNode { Menu("Порты Mac") {
                ForEach(ports, id: \.self) { path in Button(path) { selection.wrappedValue = path } }
                Button("Обновить список") { ports = localPorts }
            } }
        }
    }
    private var localNode: Bool { ["localhost", "127.0.0.1", "::1", "[::1]"].contains(model.client?.baseURL.root.host ?? "") }
    private var localPorts: [String] { localNode ? Array(SerialPort.availablePaths().prefix(256)) : [] }

    private var nodeStorage: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text("Архив на узле").font(.headline)
            if flow.location != .null {
                Text(flow.location["label"].text + " · " + flow.location["database"].text)
                if let free = flow.location["available_bytes"].integer {
                    Text("Доступно \(ByteCountFormatter.string(fromByteCount: free, countStyle: .file))")
                }
                Text("Каталог выбирается на узле при остановленной службе. На личном Mac откройте «Подключение» → «Папка архива».").font(.caption)
            } else if !flow.locationError.isEmpty {
                Text("Узел не сообщил расположение архива. " + flow.locationError).font(.caption)
            } else { Text("Запрос к узлу…").font(.caption) }
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
                if flow.draft.source == .tinySA {
                    Text("Ядро проверяет режим входа tinySA при подготовке.").font(.caption)
                }
                sampleEvidence
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
                    Text("Прошивка из исходного свипа · \(firmware)").font(.caption)
                }
                Text("Среднее время свипа: \(workspace.preflight["performance"]["mean_sweep_seconds"].scalar) с · период обзора: \(workspace.preflight["performance"]["mean_observed_cycle_seconds"].scalar) с")
                    .font(.caption).monospacedDigit()
            }.disabled(workspace.isArmed)
        }
    }
    @ViewBuilder private var sampleEvidence: some View {
        let session = workspace.project["active_session_id"].text
        if flow.draft.source == .ardv1, let sample = model.latestObservation,
           PlatformExactID.same(sample.sessionId, session), PlatformExactID.same(sample.streamId, model.receiver?.id),
           let level = sample.level {
            Text("Архивная точка · \(sample.frequencyHz) Гц · \(level.formatted()) \(sample.levelUnit) · \(sample.interval.endedAt)")
                .textSelection(.enabled)
        } else if flow.draft.source != .ardv1, let sweep = model.spectrumPresentation.sweeps.last,
                  PlatformExactID.same(sweep.sessionId, session), PlatformExactID.same(sweep.streamId, model.analyzer?.id) {
            Text("Архивный свип · \(sweep.frequenciesHz.count) точек · \(sweep.levelUnit) · \(sweep.interval.endedAt)").textSelection(.enabled)
            if let readback = NativeRFExplorerReadback(sweep.settings) { Text(readback.label).font(.caption) }
            if case .string(let mode)? = sweep.settings?["detector"] { Text("Детектор · " + mode).font(.caption) }
        } else { Text("Ожидается первая архивная запись выбранного потока.").font(.callout) }
    }

    private var baseline: some View {
        let collection = workspace.preflight["baseline_collection"]
        let progress = NativeBaselineProgress(preflight: verified ? workspace.preflight : .null)
        return GroupBox("3 · Эталон и бюджет восьми часов") {
            VStack(alignment: .leading, spacing: 8) {
                let noun = progress.usesPasses ? "Совместимые проходы" : "Совместимые свипы"
                Text("\(noun) · \(progress.sweeps.map(String.init) ?? "—") / \(progress.minimum) · \(collection["elapsed_seconds"].scalar) / \(Int(progress.minimumSeconds)) с").monospacedDigit()
                ProgressView("Продолжительность записи", value: progress.timeFraction).accessibilityIdentifier("preparation.baseline.time")
                ProgressView(noun, value: progress.sweepFraction).accessibilityIdentifier("preparation.baseline.sweeps")
                if let coverage = progress.coverage {
                    Text("\(coverage.label) · \(coverage.ready) / \(coverage.total)").monospacedDigit()
                }
                if let remaining = progress.estimatedRemainingSeconds, !progress.ready {
                    Text("Расчётное время до готовности · \(Int(min(remaining.rounded(.up), Double(Int.max / 2)))) с").font(.caption)
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
                if workspace.schedulesAvailable {
                    NativeDutyControls(workspace: workspace, verified: verified,
                        canStart: verified && !busy && workspace.canArm && workspace.preflight["ready"].bool == true && readiness.budgetReady)
                        .id(PlatformExactID(workspace.selected))
                } else {
                    HStack {
                        Button("Поставить на охрану") { guard verified else { return }; workspace.perform { await workspace.command("arm", success: "Охрана включена.") } }
                            .buttonStyle(.borderedProminent)
                            .disabled(!verified || busy || !workspace.canArm || workspace.preflight["ready"].bool != true || !readiness.budgetReady)
                            .accessibilityIdentifier("preparation.arm")
                        Button("Снять с охраны") { workspace.perform { await workspace.command("disarm", success: "Охрана выключена.") } }
                            .disabled(!model.coreReachable || !workspace.projectStatusIsCurrent() || busy || !workspace.isArmed)
                    }
                }
                if !workspace.operationsNotice.isEmpty { Text(workspace.operationsNotice).font(.caption) }
            }
        }
    }
}
