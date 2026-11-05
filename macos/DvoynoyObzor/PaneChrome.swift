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
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        ScrollView([.horizontal, .vertical]) {
            HStack(alignment: .top, spacing: 16) {
                candidates.frame(width: 390, alignment: .leading)
                investigations.frame(width: 430, alignment: .leading)
                VStack(spacing: 12) {
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
                    .disabled(model.analyzer == nil || model.current.isEmpty || model.busy)
                Picker("Кандидат", selection: $model.selectedCandidateID) {
                    Text("Не выбран").tag("")
                    ForEach(model.candidates) { candidate in
                        Text("\(candidate.id) · \(candidate.state)").tag(candidate.id)
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
                Picker("Исследование", selection: $model.selectedInvestigationID) {
                    Text("Не выбрано").tag("")
                    ForEach(model.investigations) { investigation in
                        Text("\(investigation.id) · \(investigation.state)").tag(investigation.id)
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
                Text("Start выбирает явное исследование; подтверждение выше использует актуальный ID рекомендации. Автоматического complete нет.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .padding(4)
        }
    }
}

private let workflowOpenStates: Set<String> = ["queued", "tuning", "listening", "decoding"]

struct RemoteDetectionPolicyPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        GroupBox("Automatic detection · независимый opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                WorkflowPolicyStatus(status: model.automaticDetectionStatus)
                Picker("Поток dBm", selection: $model.detectionStreamID) {
                    if let analyzer = model.analyzer {
                        Text("\(analyzer.device) · \(analyzer.id)").tag(analyzer.id)
                    } else {
                        Text("Нет dBm analyzer").tag("")
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
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        GroupBox("Automatic handoff · независимый opt-in") {
            VStack(alignment: .leading, spacing: 7) {
                WorkflowPolicyStatus(status: model.automaticHandoffStatus)
                Picker("Receiver", selection: $model.handoffReceiverStreamID) {
                    Text("Любой receiver сеанса").tag("")
                    if let receiver = model.receiver {
                        Text("\(receiver.device) · \(receiver.id)").tag(receiver.id)
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
            Text("enabled=\(status.policy.enabled) · scheduler=\(status.schedulerAlive) · результат=\(status.runtime.lastResult ?? "—")")
                .font(.caption)
            if let error = status.runtime.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red)
            }
            Text("runtime-only · persistent=\(status.policyPersistent) · после restart нужен новый opt-in=\(status.restartRequiresOptIn)")
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
