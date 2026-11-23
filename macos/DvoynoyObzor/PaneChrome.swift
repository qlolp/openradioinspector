import Foundation
import SpektrKomandy
import SwiftUI

struct PortFields<Speed: View>: View {
    @Binding var path: String
    var connected: Bool
    var ports: [String]
    var onRefresh: () -> Void
    @ViewBuilder var speed: () -> Speed

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("/dev/cu…", text: $path)
                .textFieldStyle(.roundedBorder)
                .disabled(connected)
            HStack {
                Menu("Найденные порты") {
                    if ports.isEmpty {
                        Button("Порты cu.* не найдены") {}
                            .disabled(true)
                    } else {
                        ForEach(ports, id: \.self) { port in
                            Button(port) { path = port }
                        }
                    }
                }
                .disabled(connected)
                Button("Обновить", action: onRefresh)
                    .disabled(connected)
                Spacer()
            }
            speed()
        }
    }
}

struct ErrorLine: View {
    var text: String?

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.callout)
                .foregroundStyle(.red)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct ReplyLog: View {
    var text: String

    var body: some View {
        Text(text.isEmpty ? "—" : text)
            .font(.system(.caption, design: .monospaced))
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(8)
            .background(Color.primary.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

func parseUserNumber(_ text: String) -> Double? {
    let normalized = text
        .trimmingCharacters(in: .whitespacesAndNewlines)
        .replacingOccurrences(of: ",", with: ".")
    if normalized.isEmpty { return nil }
    return Double(normalized)
}

func describeSession(_ error: Error) -> String {
    if let problem = error as? SessionProblem {
        return problem.errorDescription ?? "Ошибка сеанса."
    }
    let text = error.localizedDescription
    if text.isEmpty {
        return "Ошибка сеанса."
    }
    return text
}

func tracesFolder() throws -> URL {
    guard let base = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first else {
        throw SessionProblem.cannotWriteTrace("нет каталога документов")
    }
    let folder = base.appendingPathComponent("Двойной обзор", isDirectory: true)
    do {
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    } catch {
        throw SessionProblem.cannotWriteTrace(error.localizedDescription)
    }
    return folder
}

func clipReply(_ text: String, limit: Int) -> String {
    let flat = text.replacingOccurrences(of: "\r", with: " ").replacingOccurrences(of: "\n", with: " ")
    if flat.count <= limit { return flat }
    return String(flat.prefix(limit)) + "…"
}


struct RemoteWorkflowPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            HStack(alignment: .top, spacing: 16) {
                VStack(spacing: 12) {
                    candidates
                    RemoteFrequencyRoundingPane(model: model.frequencyRounding)
                }.frame(width: 480, alignment: .leading)
                investigations.frame(width: 430, alignment: .leading)
                VStack(spacing: 12) {
                    RemoteTaskSchedulerPane(model: model)
                    RemoteAnalyticsPane(model: model)
                    RemoteCompletionPolicyPane(model: model)
                    RemoteDetectionPolicyPane(model: model)
                    RemoteHandoffPolicyPane(model: model)
                }
                .frame(width: 470, alignment: .leading)
            }
            .padding(16)
        }
        .background(Color.primary.opacity(0.025))
    }

    private var candidates: some View {
        GroupBox("Кандидаты · фактические измерения") {
            VStack(alignment: .leading, spacing: 8) {
                Button("Создать из фактического dBm пика") { model.createCandidateFromLatestPeak() }
                    .disabled(model.analyzer == nil || model.current.isEmpty || model.busy || model.spectrumPresentation.paused)
                Picker("Кандидат", selection: $model.selectedCandidateID.exactID) {
                    Text("Не выбран").tag(PlatformExactID(""))
                    ForEach(model.candidates, id: \.exactID) { candidate in
                        Text("\(candidate.id) · \(candidate.state)").tag(candidate.exactID)
                    }
                }
                if let candidate = model.selectedCandidate {
                    Text(workflowFrequency(candidate.centerFrequencyHz))
                    Text("Уровень: \(candidate.peakLevel.map { String(format: "%.2f", $0) } ?? "—") \(candidate.levelUnit)")
                    Text("Состояние: \(candidate.state) · источник: \(candidate.sourceStreamId)")
                        .font(.caption).foregroundStyle(.secondary)
                    Text("Sweep: \(candidate.observationIds.joined(separator: ", "))")
                        .font(.caption).textSelection(.enabled).foregroundStyle(.secondary)
                    HStack {
                        TextField("Приоритет", text: $model.investigationPriority)
                            .textFieldStyle(.roundedBorder).frame(width: 100)
                        Button("В очередь") { model.enqueueSelectedCandidate() }
                            .disabled(!["new", "queued"].contains(candidate.state) || model.receiver == nil || model.busy)
                        Button("Отклонить") { model.dismissSelectedCandidate() }
                            .disabled(!["new", "queued"].contains(candidate.state) || model.busy)
                    }
                } else {
                    Text("Кандидатов пока нет").foregroundStyle(.secondary)
                }
                Text("Клиент не выдумывает полосу или класс сигнала: используются частота, интервал, уровень и ID архивного sweep.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }

    private var investigations: some View {
        GroupBox("Очередь и исследования") {
            VStack(alignment: .leading, spacing: 8) {
                if let next = model.nextInvestigation {
                    Text("Следующее: \(next.id) · приоритет \(next.priority)")
                        .textSelection(.enabled)
                    Button("Подтвердить запуск следующего") { model.startNextInvestigation() }
                        .disabled(model.session?.state != "running" || model.busy)
                } else {
                    Text("Рекомендация отсутствует: очередь пуста").foregroundStyle(.secondary)
                }
                Picker("Исследование", selection: $model.selectedInvestigationID.exactID) {
                    Text("Не выбрано").tag(PlatformExactID(""))
                    ForEach(model.investigations, id: \.exactID) { investigation in
                        Text("\(investigation.id) · \(investigation.state)").tag(investigation.exactID)
                    }
                }
                if let investigation = model.selectedInvestigation {
                    Text(workflowFrequency(investigation.targetFrequencyHz))
                    Text("Состояние: \(investigation.state) · приоритет \(investigation.priority) · режим \(investigation.mode ?? "—")")
                    Text("Receiver: \(investigation.targetStreamId)")
                        .font(.caption).textSelection(.enabled).foregroundStyle(.secondary)
                    if let error = investigation.error, !error.isEmpty {
                        Text(error).font(.caption).foregroundStyle(.red)
                    }
                    HStack {
                        Button("Start") { model.startSelectedInvestigation() }
                            .disabled(investigation.state != "queued" || model.session?.state != "running" || model.busy)
                        Button("Complete") { model.completeSelectedInvestigation() }
                            .disabled(!["listening", "decoding"].contains(investigation.state) || model.busy)
                        Button("Cancel") { model.cancelSelectedInvestigation() }
                            .disabled(!workflowOpenStates.contains(investigation.state) || model.busy)
                    }
                    HStack {
                        TextField("Причина fail", text: $model.failureReason)
                            .textFieldStyle(.roundedBorder)
                        Button("Fail") { model.failSelectedInvestigation() }
                            .disabled(!workflowOpenStates.contains(investigation.state) || model.busy)
                    }
                    Text("Наблюдений результата: \(investigation.resultObservationIds.count). Complete будет отклонён ядром, если подходящих наблюдений ещё нет.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text("Start выбирает явное исследование; подтверждение выше использует актуальный ID рекомендации. Automatic completion настраивается отдельным opt-in.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

private let workflowOpenStates: Set<String> = ["queued", "tuning", "listening", "decoding"]

struct RemoteTaskSchedulerPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Control tasks · глобальный runtime-only opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                if let status = model.taskSchedulerStatus {
                    Text("\(status.enabled ? "Включён" : "Выключен") · thread \(status.schedulerAlive ? "active" : "stopped")")
                    Text("Всего \(status.tasks.total) · в работе \(status.tasks.running) · готово \(status.tasks.completed)")
                    if let result = status.runtime.lastResult {
                        Text("Результат: \(result) · задача \(status.runtime.lastTaskName ?? "—")")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    if let error = status.runtime.lastError, !error.isEmpty {
                        Text(error).font(.caption).foregroundStyle(.red)
                    }
                } else {
                    Text(model.taskSchedulerError ?? "Состояние scheduler запрашивается у ядра…")
                        .foregroundStyle(.secondary)
                }
                if let error = model.taskSchedulerError, !error.isEmpty {
                    Text(error).font(.caption).foregroundStyle(.red)
                }
                Text("TaskBook · публичный снимок").font(.caption).foregroundStyle(.secondary)
                if let error = model.taskSchedulerTasksError, !error.isEmpty {
                    Text("Список ControlTask недоступен: \(error)")
                        .font(.caption).foregroundStyle(.red)
                } else if model.taskSchedulerTasks.isEmpty {
                    Text("В TaskBook нет задач.").font(.caption).foregroundStyle(.secondary)
                }
                if !model.taskSchedulerTasks.isEmpty {
                    ScrollView(.vertical) {
                        LazyVStack(alignment: .leading, spacing: 8) {
                            ForEach(model.taskSchedulerTasks.indices, id: \.self) { index in
                                let task = model.taskSchedulerTasks[index]
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(task.name).font(.caption).bold()
                                    Text("\(task.status) · \(task.startAt ?? "время не задано")")
                                        .font(.caption)
                                    if !task.outcome.isEmpty {
                                        Text(task.outcome).font(.caption).foregroundStyle(.secondary)
                                    }
                                    Text(task.equipment.isEmpty ? "Оборудование не указано" : task.equipment)
                                        .font(.caption).foregroundStyle(.secondary)
                                    if task.status == "В очереди на выполнение" {
                                        Button("Отменить") { model.cancelTask(name: task.name) }
                                            .disabled(model.taskCancelPending)
                                    }
                                }
                                .textSelection(.enabled)
                            }
                        }
                    }
                    .frame(maxHeight: 180)
                }
                Button(model.showTaskCreate ? "Скрыть создание" : "Новая ControlTask…") {
                    model.showTaskCreate.toggle()
                }
                .disabled(model.taskCreatePending)
                if model.showTaskCreate {
                    VStack(alignment: .leading, spacing: 5) {
                        TextField("Имя", text: $model.newTaskName).textFieldStyle(.roundedBorder)
                        TextField("task_file", text: $model.newTaskFile).textFieldStyle(.roundedBorder)
                        TextField("Начало ISO 8601", text: $model.newTaskStartAt).textFieldStyle(.roundedBorder)
                        TextField("Длительность, с", text: $model.newTaskDuration).textFieldStyle(.roundedBorder)
                        TextField("Оборудование", text: $model.newTaskEquipment).textFieldStyle(.roundedBorder)
                        Button("Создать задачу") { model.createTask() }
                            .disabled(model.taskCreatePending)
                    }
                }
                Text(model.newTaskStatus).font(.caption).foregroundStyle(.secondary)
                Text(model.taskCancelStatus).font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Включить scheduler") { model.enableTaskScheduler() }
                        .disabled(model.taskSchedulerStatus == nil || model.taskSchedulerStatus?.enabled == true || model.busy)
                    Button("Выключить") { model.disableTaskScheduler() }
                        .disabled(model.taskSchedulerStatus?.enabled != true || model.busy)
                }
                Text("Глобальный scheduler сервера выполняет существующую TaskBook и не связан с выбранным сеансом. После перезапуска требуется новое явное включение. Клиент создаёт и отменяет queued-задачи только через API и не открывает приборные порты.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

struct RemoteArchiveCapacityPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Ёмкость всего архива · сервер") {
            VStack(alignment: .leading, spacing: 4) {
                if let capacity = model.displayArchiveCapacity {
                    HStack {
                        Label(stateTitle(capacity.state), systemImage: stateIcon(capacity.state))
                            .foregroundStyle(stateColor(capacity.state))
                        Text(capacity.storageMode == .sqlite ? "SQLite" : "В памяти")
                        Text("Размер: \(bytes(capacity.databaseBytes)) · лимит: \(capacity.maxDatabaseBytes.map { bytes($0) } ?? "не задан")")
                        if let remaining = capacity.remainingBytes {
                            Text("Осталось: \(bytes(remaining))")
                        }
                    }
                    if capacity.measurementWritesBlocked {
                        Text("Запись новых измерений заблокирована; сохранённые данные не удаляются.")
                            .foregroundStyle(.red)
                    }
                    if capacity.state == .warning {
                        Text("Архив заполнен не менее чем на \(Int(capacity.warningThreshold * 100))% лимита.")
                            .foregroundStyle(.orange)
                    }
                    if capacity.storageMode == .memory {
                        Text("Архив в памяти: размер SQLite и контроль его лимита недоступны.")
                    }
                } else {
                    Text(model.archiveCapacityError.map { "Ёмкость недоступна: \($0)" }
                         ?? (model.connected ? "Ожидание сведений о ёмкости…" : "Подключитесь к серверу"))
                        .foregroundStyle(.secondary)
                }
                Text("Логический размер SQLite без WAL/SHM, не свободное место диска. Метаданные могут расти сверх лимита. Лимит задаётся CLI сервера; автоматического удаления нет.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func bytes(_ value: Int64?) -> String {
        value.map { ByteCountFormatter.string(fromByteCount: $0, countStyle: .binary) } ?? "недоступен"
    }

    private func stateTitle(_ state: PlatformArchiveCapacity.State) -> String {
        switch state {
        case .unlimited: return "Без ограничения"
        case .ok: return "В пределах лимита"
        case .warning: return "Архив близок к лимиту"
        case .full: return "Лимит архива достигнут"
        case .unavailable: return "Ёмкость недоступна"
        }
    }

    private func stateIcon(_ state: PlatformArchiveCapacity.State) -> String {
        switch state {
        case .full, .warning: return "exclamationmark.triangle.fill"
        case .unavailable: return "questionmark.circle"
        default: return "externaldrive"
        }
    }

    private func stateColor(_ state: PlatformArchiveCapacity.State) -> Color {
        switch state {
        case .full: return .red
        case .warning: return .orange
        case .unavailable: return .secondary
        default: return .primary
        }
    }
}

struct RemoteAnalyticsPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Сводка всего архива · read-only") {
            VStack(alignment: .leading, spacing: 6) {
                Button(model.archiveAnalyticsPending ? "Запрос сводки…" : "Запросить сводку") {
                    model.refreshArchiveAnalytics()
                }
                .disabled(model.session == nil || model.archiveAnalyticsPending)
                if model.archiveAnalyticsPending {
                    Text("Сводка запрашивается один раз; обычный polling её не выполняет")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Button(model.archiveExportPending ? "Экспорт NDJSON…" : "Скачать NDJSON snapshot…") {
                    model.exportArchive()
                }
                .disabled(model.session == nil || model.archiveExportPending)
                Text(model.archiveExportStatus).font(.caption).foregroundStyle(.secondary)
                if let summary = model.archiveAnalytics {
                    Text("Sweeps: \(summary.sweeps.count) · \(summary.sweeps.levelUnits.joined(separator: ", "))")
                    Text("Observations: \(summary.observations.count) · \(summary.observations.levelUnits.joined(separator: ", "))")
                    if let ratio = summary.observations.squelchOpenRatio {
                        Text("Шумодав открыт: \(String(format: "%.1f", ratio * 100))% · \(summary.observations.squelchOpen) из \(summary.observations.squelchKnown)")
                    } else {
                        Text("Состояние шумодава неизвестно")
                    }
                    if let peak = summary.sweeps.latestPeak {
                        Text("Пик последнего sweep: \(workflowFrequency(peak.frequencyHz)) · \(String(format: "%.2f", peak.level)) \(peak.levelUnit)")
                            .textSelection(.enabled)
                    } else {
                        Text("Спектральных свипов нет").foregroundStyle(.secondary)
                    }
                    Text("archive_wide=\(String(summary.archiveWide)) · исходные единицы не преобразуются")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text(model.archiveAnalyticsError ?? "Сводка появится после выбора сеанса")
                        .foregroundStyle(.secondary)
                }
            }
            .padding(4)
        }
    }
}

struct RemoteCompletionPolicyPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Automatic completion · независимый opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                WorkflowPolicyStatus(status: model.automaticCompletionStatus)
                if let runtime = model.automaticCompletionStatus?.runtime {
                    Text("completed=\(runtime.completedInvestigations ?? 0) · skipped=\(runtime.skippedInvestigations ?? 0) · последнее=\(runtime.lastInvestigationId ?? "—")")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Picker("Receiver", selection: $model.completionReceiverStreamID.exactID) {
                    Text("Любой receiver сеанса").tag(PlatformExactID(""))
                    if let receiver = model.receiver {
                        Text("\(receiver.device) · \(receiver.id)").tag(receiver.exactID)
                    }
                }
                HStack {
                    WorkflowField(title: "Период, с", text: $model.completionInterval)
                    WorkflowField(title: "Мин. прослушивания, с", text: $model.completionMinimumListening)
                    WorkflowField(title: "Мин. наблюдений", text: $model.completionMinimumObservations)
                }
                HStack {
                    Button("Сохранить") { model.saveAutomaticCompletion() }
                        .disabled(model.automaticCompletionStatus == nil || model.busy)
                    Button("Включить явно") { model.enableAutomaticCompletion() }
                        .disabled(model.session?.state != "running" || model.automaticCompletionStatus == nil || model.busy || model.automaticCompletionStatus?.policy.enabled == true)
                    Button("Выключить") { model.disableAutomaticCompletion() }
                        .disabled(model.automaticCompletionStatus?.policy.enabled != true || model.busy)
                }
                Text("Завершаются только listening/decoding после времени и нужного числа наблюдений receiver. Automatic fail/cancel отсутствует.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

struct RemoteDetectionPolicyPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Automatic detection · независимый opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                WorkflowPolicyStatus(status: model.automaticDetectionStatus)
                Picker("Поток dBm", selection: $model.detectionStreamID.exactID) {
                    Text("Любой dBm analyzer сеанса").tag(PlatformExactID(""))
                    if let analyzer = model.analyzer {
                        Text("\(analyzer.device) · \(analyzer.id)").tag(analyzer.exactID)
                    } else {
                        Text("Нет dBm analyzer").tag(PlatformExactID(""))
                    }
                }
                HStack {
                    WorkflowField(title: "Период, с", text: $model.detectionInterval)
                    WorkflowField(title: "Порог, dBm", text: $model.detectionThreshold)
                }
                HStack {
                    WorkflowField(title: "Dedup, Гц", text: $model.detectionDedupBandwidth)
                    WorkflowField(title: "Cooldown, с", text: $model.detectionCooldown)
                    WorkflowField(title: "Макс./sweep", text: $model.detectionMaximumCandidates)
                }
                HStack {
                    Button("Сохранить") { model.saveAutomaticDetection() }
                        .disabled(model.automaticDetectionStatus == nil || model.busy)
                    Button("Включить явно") { model.enableAutomaticDetection() }
                        .disabled(model.session?.state != "running" || model.analyzer == nil || model.automaticDetectionStatus == nil || model.busy || model.automaticDetectionStatus?.policy.enabled == true)
                    Button("Выключить") { model.disableAutomaticDetection() }
                        .disabled(model.automaticDetectionStatus?.policy.enabled != true || model.busy)
                }
                Text("Создаёт только new-кандидатов из новых dBm sweeps. Не ставит исследования в очередь и не обрабатывает S-метр.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

struct RemoteHandoffPolicyPane: View {
    @Bindable var model: RemoteDualModel

    var body: some View {
        GroupBox("Automatic handoff · независимый opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                WorkflowPolicyStatus(status: model.automaticHandoffStatus)
                Picker("Receiver", selection: $model.handoffReceiverStreamID.exactID) {
                    Text("Любой receiver сеанса").tag(PlatformExactID(""))
                    if let receiver = model.receiver {
                        Text("\(receiver.device) · \(receiver.id)").tag(receiver.exactID)
                    }
                }
                HStack {
                    WorkflowField(title: "Период, с", text: $model.handoffInterval)
                    WorkflowField(title: "Мин. приоритет", text: $model.handoffMinimumPriority)
                }
                HStack {
                    WorkflowField(title: "Сбоев до off", text: $model.handoffMaximumFailures)
                    WorkflowField(title: "Retry, с", text: $model.handoffRetryDelay)
                }
                HStack {
                    Button("Сохранить") { model.saveAutomaticHandoff() }
                        .disabled(model.automaticHandoffStatus == nil || model.busy)
                    Button("Включить явно") { model.enableAutomaticHandoff() }
                        .disabled(model.session?.state != "running" || model.automaticHandoffStatus == nil || model.busy || model.automaticHandoffStatus?.policy.enabled == true)
                    Button("Выключить") { model.disableAutomaticHandoff() }
                        .disabled(model.automaticHandoffStatus?.policy.enabled != true || model.busy)
                }
                Text("Автоматизирует только queued → tuning → listening. Выключение не отменяет активное исследование и не меняет detector.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

struct WorkflowPolicyStatus: View {
    let status: PlatformAutomaticStatus?

    var body: some View {
        if let status {
            Text("enabled=\(String(status.policy.enabled)) · scheduler=\(String(status.schedulerAlive)) · результат=\(status.runtime.lastResult ?? "—")")
                .font(.caption)
            if let error = status.runtime.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            Text("runtime-only · persistent=\(String(status.policyPersistent)) · после restart нужен новый opt-in=\(String(status.restartRequiresOptIn))")
                .font(.caption).foregroundStyle(.secondary)
        } else {
            Text("Политика ещё не загружена").font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct WorkflowField: View {
    let title: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(title, text: $text).textFieldStyle(.roundedBorder).frame(width: 130)
        }
    }
}

func workflowFrequency(_ hertz: Int64) -> String {
    String(format: "%.6f МГц", Double(hertz) / 1_000_000) + " · \(hertz) Гц"
}


struct RemoteSerialSessionPane: View {
    @Bindable var model: RemoteDualModel
    var onRFExplorerConnected: () -> Void = {}
    @State private var rfExplorer = false
    @State private var features: Set<String>?
    @State private var supportError: String?
    @State private var draft = NativeRFExplorerDraft()
    @State private var ports: [String] = []

    private var configuration: Result<PlatformRFExplorerConfiguration, any Error> {
        Result { try draft.configuration() }
    }
    private var supportIssue: String? {
        supportError ?? features.flatMap { PlatformRFExplorerConfiguration.supportIssue(in: $0, survey: draft.mode == .survey) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Подключить приборы")
                .font(.title2.weight(.semibold))
            Picker("Спектроанализатор", selection: $rfExplorer) {
                Text("tinySA Ultra").tag(false)
                Text(PlatformRFExplorerConfiguration.device).tag(true)
            }.pickerStyle(.segmented).disabled(model.busy)
            if rfExplorer {
                explorerFields
            } else {
                tinySAFields
            }
            HStack {
                Button("Отмена") { model.showSerialForm = false }.disabled(model.busy)
                Spacer()
                if rfExplorer {
                    Button(draft.mode == .survey ? "Начать автообзор 10 кГц" : "Подключить и начать приём") {
                        if case .success(let selected) = configuration {
                            Task {
                                if await model.connectRFExplorer(selected) { onRFExplorerConnected() }
                            }
                        }
                    }.buttonStyle(.borderedProminent)
                        .disabled(model.busy || features == nil || supportIssue != nil || (try? configuration.get()) == nil)
                        .accessibilityIdentifier("rf-explorer.connect")
                } else {
                    Button("Создать сеанс") { model.createSerialSession() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.busy)
                }
            }
            if model.busy { ProgressView().controlSize(.small) }
            ErrorLine(text: model.errorText)
        }
        .padding(20)
        .frame(width: 540)
        .interactiveDismissDisabled(model.busy)
        .onAppear { model.refreshSerialPorts() }
        .task(id: model.connectionGeneration) {
            features = nil
            supportError = nil
            draft.port = ""
            ports = []
            guard let client = model.client else {
                supportError = "Подключитесь к ядру, чтобы проверить доступные режимы приёма."
                return
            }
            ports = client.baseURL.isLoopback ? SerialPort.availablePaths() : []
            do {
                let advertised = try await client.workbenchFeatures()
                guard !Task.isCancelled, model.client === client else { return }
                features = advertised
            } catch {
                guard !Task.isCancelled, model.client === client else { return }
                supportError = describeSession(error)
            }
        }
    }

    @ViewBuilder private var explorerFields: some View {
        Picker("Режим приёма", selection: $draft.mode) {
            Text("Окно прибора").tag(NativeRFExplorerDraft.Mode.instrument)
            Text("Задать окно").tag(NativeRFExplorerDraft.Mode.window)
            Text("Автообзор 10 кГц").tag(NativeRFExplorerDraft.Mode.survey)
        }.pickerStyle(.segmented).disabled(model.busy)
        if draft.mode == .survey {
            Text("Ядро автоматически перестраивает окна и сохраняет отсчёты через каждые 10 кГц. По завершении диапазона начинается следующий проход.")
                .font(.callout)
            Picker("Диапазон обзора", selection: $draft.customSurveyRange) {
                Text("Весь активный вход").tag(false)
                Text("Свои границы").tag(true)
            }.pickerStyle(.segmented).disabled(model.busy)
        } else if draft.mode == .instrument {
            Text("Частотное окно будет считано с прибора.").font(.caption).foregroundStyle(.secondary)
        }
        NativeRFExplorerRangeFields(draft: $draft).disabled(model.busy)
        if !draft.needsPort, model.analyzer?.isRFExplorer3G == true, model.analyzer?.port == draft.port,
           let source = model.displayInspectorSweeps.last, let readback = NativeRFExplorerReadback(source.settings) {
            Text("Последний свип этого порта · \(readback.label)").font(.caption)
        } else {
            Text("Плата, вход SMA и пределы активного модуля будут известны после чтения с прибора.")
                .font(.caption).foregroundStyle(.secondary)
        }
        LabeledContent("Порт RF Explorer") {
            TextField("Порт на компьютере ядра", text: $draft.port)
                .textFieldStyle(.roundedBorder).accessibilityIdentifier("rf-explorer.port")
            if model.client?.baseURL.isLoopback == true {
                Menu("Выбрать порт") {
                    ForEach(ports, id: \.self) { path in Button(path) { draft.port = path } }
                    Button("Обновить список") { ports = SerialPort.availablePaths() }
                }
            }
        }.disabled(model.busy)
        if draft.needsPort {
            Text(model.client?.baseURL.isLoopback == true
                 ? "Для запуска выберите USB-порт RF Explorer в меню «Выбрать порт» или введите путь. Если порта нет в списке, подключите прибор и нажмите «Обновить список»."
                 : "Для запуска введите USB-порт RF Explorer на компьютере ядра, например /dev/ttyUSB0 или COM3.")
                .font(.callout).foregroundStyle(.secondary).accessibilityIdentifier("rf-explorer.port-hint")
        }
        if let supportIssue {
            ErrorLine(text: supportIssue)
        } else if features == nil {
            ProgressView("Проверка возможностей ядра…").controlSize(.small)
        } else if !draft.needsPort, case .failure(let error) = configuration {
            ErrorLine(text: describeSession(error))
        }
    }

    @ViewBuilder private var tinySAFields: some View {
        Text("Выберите tinySA. Приёмник AR-DV1 можно добавить, если он подключён. Создание сеанса не запускает измерения.")
            .foregroundStyle(.secondary)
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Порт tinySA:").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if let modem = SerialPort.availablePaths().first(where: { $0.contains("usbmodem") }) {
                    Button("Найдено: \(modem)") {
                        model.tinySAPort = modem
                    }
                    .buttonStyle(.borderless)
                    .font(.caption)
                }
            }
            TextField("Порт tinySA (напр. /dev/cu.usbmodem4001)", text: $model.tinySAPort)
                .textFieldStyle(.roundedBorder)
        }
        VStack(alignment: .leading, spacing: 4) {
            Text("Порт AR-DV1 (необязательно):").font(.caption).foregroundStyle(.secondary)
            TextField("Порт AR-DV1 (оставьте пустым, если не подключён)", text: $model.receiverPort)
                .textFieldStyle(.roundedBorder)
        }
        Text("Порты компьютера ядра: COM3/COM4 на Windows или /dev/… на Linux/macOS. Если AR-DV1 не подключён, оставьте поле пустым — будет создан сеанс только со спектроанализатором tinySA. Приборы не должны использовать sim://.")
            .font(.caption).foregroundStyle(.secondary)
    }
}
