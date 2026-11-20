import SwiftUI
import SpektrKomandy

struct NativeConsoleEventCard: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let summary: NativeGuardSignal
    let detail: NativeGuardSignal?
    let openReceiver: () -> Void
    var remote: RemoteDualModel?
    @State private var queueTask: Task<Void, Never>?
    @State private var loadingSource = false
    @State private var queueError = ""

    private var signal: NativeGuardSignal { detail ?? summary }
    private var investigationScope: NativeConsoleInvestigation.Scope? {
        guard let remote, let client = remote.client, let source = detail?.source, let receiver = remote.receiver,
              signal.id == workspace.signalQueue.selectedID, workspace.signalEvent.exactID == signal.id,
              remote.coreReachable, source.sessionID == PlatformExactID(remote.selectedSessionID),
              source.streamID == PlatformExactID(remote.analyzer?.id ?? "") else { return nil }
        return .init(server: client.baseURL.root.absoluteString, project: workspace.selected, event: signal.id.rawValue,
                     session: remote.selectedSessionID, analyzer: source.streamID.rawValue,
                     receiver: receiver.id, source: source.sweepID.rawValue, runtime: client.baseURL.runtimeInstanceID ?? "")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            Text(signal.detectorKind).font(.headline)
            Text(signal.detectorState).font(.caption).foregroundStyle(.secondary)
            GroupBox("Измерено") {
                VStack(alignment: .leading, spacing: 5) {
                    Text(NativeGuardSignal.frequency(signal.value["start_frequency_hz"]) + "–" +
                         NativeGuardSignal.frequency(signal.value["end_frequency_hz"]) + " МГц")
                    Text("Пик tinySA: " + NativeGuardSignal.level(signal.value["peak_level"], unit: "dBm"))
                    Text("Над фоном: " + NativeGuardSignal.level(signal.value["peak_excess_db"], unit: "dB"))
                    Text(signal.receiver)
                    if let reason = signal.receiverReason { Text(reason).foregroundStyle(.secondary) }
                    let analysis = signal.value["analysis"]
                    let matched = analysis["verification_evidence"]["receiver"]
                    let reading = matched != .null ? matched : analysis["receiver_status"].items.last ?? .null
                    if reading != .null {
                        let unit = reading["level_unit"].text == "s_meter_count" ? "отсч. S-метра" : reading["level_unit"].text
                        Text((matched != .null ? "Связанное измерение AR-DV1 · " : "Последнее измерение AR-DV1 · ") +
                             reading["level"].scalar + " " + unit)
                            .accessibilityIdentifier("observation.receiver-level")
                    }
                    Button("В очередь AR-DV1") { enqueueOriginal() }
                        .disabled(investigationScope == nil || loadingSource || remote?.consoleInvestigation.pending == true ||
                                  remote?.consoleInvestigation.canEnqueue != true)
                        .help("По пику исходного свипа внутри полосы события. Постановка в очередь не означает начало приёма.")
                        .accessibilityIdentifier("observation.enqueue")
                    if loadingSource || remote?.consoleInvestigation.pending == true {
                        ProgressView("Проверка исходника и постановка в очередь…").controlSize(.small)
                    }
                    if let message = remote?.consoleInvestigation.message, !message.isEmpty { Text(message) }
                    ErrorLine(text: queueError)
                    ErrorLine(text: remote?.consoleInvestigation.error ?? "")
                    Button("Очередь и управление AR-DV1…", action: openReceiver)
                        .accessibilityIdentifier("observation.receiver")
                }.font(.callout).monospacedDigit().frame(maxWidth: .infinity, alignment: .leading)
            }
            DisclosureGroup("Справка и гипотезы") {
                Text("Частотный план и совпадение с каталогом не подтверждают идентификацию сигнала.")
                Text(signal.recognition)
                ForEach(Array(signal.value["recognition"]["hypotheses"].items.enumerated()), id: \.offset) { _, value in
                    Text(value["label"].text + " · " + value["reason"].text)
                }
            }.font(.caption)
            GroupBox("Решение оператора") {
                VStack(alignment: .leading, spacing: 6) {
                    Picker("Результат разбора", selection: $workspace.consoleVerdict.disposition) {
                        Text("Не рассмотрено").tag("unreviewed")
                        Text("Разрешённый сигнал").tag("authorized")
                        Text("Ложное срабатывание").tag("false_positive")
                        Text("Исследовать").tag("investigate")
                    }.disabled(workspace.signalPending.contains(.review))
                    TextField("Комментарий оператора", text: $workspace.consoleVerdict.comment, axis: .vertical).lineLimit(2...4)
                        .disabled(workspace.signalPending.contains(.review))
                    Text("\(workspace.consoleVerdict.comment.unicodeScalars.count) / 1000 символов Unicode")
                        .font(.caption).foregroundStyle(workspace.consoleVerdict.valid ? Color.secondary : Color.red)
                    if workspace.consoleVerdict.dirty { Text("Есть несохранённые изменения").font(.caption).foregroundStyle(.secondary) }
                    Button("Сохранить решение") {
                        let id = signal.id, value = workspace.consoleVerdict.disposition, note = workspace.consoleVerdict.comment
                        workspace.perform { await workspace.reviewSignalEvent(eventID: id, disposition: value, comment: note) }
                    }.disabled(detail == nil || !workspace.consoleVerdict.valid || workspace.signalPending.contains(.review))
                        .accessibilityIdentifier("observation.save-review")
                    if workspace.signalPending.contains(.review) { ProgressView("Сохранение…").controlSize(.small) }
                    ErrorLine(text: workspace.signalErrors[.review] ?? "")
                }.controlSize(.small)
            }
            DisclosureGroup("Идентификаторы исходника") {
                Text(signal.id.rawValue)
                if let source = signal.source {
                    Text(source.sweepID.rawValue)
                    Text(source.sessionID.rawValue + " / " + source.streamID.rawValue)
                }
            }.font(.caption).textSelection(.enabled)
        }
        .onChange(of: investigationScope, initial: true) { _, scope in
            queueTask?.cancel(); queueTask = nil; loadingSource = false; queueError = ""
            remote?.consoleInvestigation.configure(scope)
        }
        .onChange(of: remote?.connectionGeneration) { _, _ in
            queueTask?.cancel(); loadingSource = false
            remote?.consoleInvestigation.configure(nil)
            remote?.consoleInvestigation.configure(investigationScope)
        }
        .onDisappear { queueTask?.cancel() }
    }

    private func enqueueOriginal() {
        guard !loadingSource, let scope = investigationScope, let remote, let client = remote.client,
              let start = signal.value["start_frequency_hz"].integer,
              let stop = signal.value["end_frequency_hz"].integer else { return }
        loadingSource = true; queueError = ""
        let mode = remote.mode
        queueTask = Task { @MainActor in
            defer { if scope == investigationScope { loadingSource = false } }
            do {
                let original = try await client.archiveOriginalSweep(sessionID: scope.session.rawValue,
                    streamID: scope.analyzer.rawValue, sweepID: scope.source.rawValue)
                try Task.checkCancellation()
                guard scope == investigationScope, client === remote.client else { return }
                try await remote.consoleInvestigation.enqueue(scope: scope, original: original, startHz: start, stopHz: stop,
                    mode: mode, create: { draft in
                        guard scope == investigationScope, client === remote.client, !Task.isCancelled else { throw CancellationError() }
                        return try await client.createCandidate(sessionID: scope.session.rawValue, draft: draft).id
                    }, enqueue: { candidate, draft in
                        guard scope == investigationScope, client === remote.client, !Task.isCancelled else { throw CancellationError() }
                        return try await client.enqueueInvestigation(sessionID: scope.session.rawValue, candidateID: candidate, draft: draft).id
                    })
                if scope == investigationScope { await remote.refresh() }
            } catch is CancellationError { }
              catch { if scope == investigationScope { queueError = error.localizedDescription } }
        }
    }
}
