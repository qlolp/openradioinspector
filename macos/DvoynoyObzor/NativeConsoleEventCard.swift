import SwiftUI
import SpektrKomandy

struct NativeConsoleEventCard: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let summary: NativeGuardSignal
    let detail: NativeGuardSignal?
    let openReceiver: () -> Void
    @State private var seeded: PlatformExactID?
    @State private var verdict = "unreviewed"
    @State private var comment = ""

    private var signal: NativeGuardSignal { detail ?? summary }

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
                    let receiver = signal.value["analysis"]["verification_evidence"]["receiver"]
                    if receiver != .null { Text(receiver["level"].scalar + " " + receiver["level_unit"].text) }
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
                    Picker("Результат разбора", selection: $verdict) {
                        Text("Не рассмотрено").tag("unreviewed")
                        Text("Разрешённый сигнал").tag("authorized")
                        Text("Ложное срабатывание").tag("false_positive")
                        Text("Исследовать").tag("investigate")
                    }
                    TextField("Комментарий оператора", text: $comment, axis: .vertical).lineLimit(2...4)
                    Button("Сохранить решение") {
                        let id = signal.id, value = verdict, note = comment
                        workspace.perform { await workspace.reviewSignalEvent(eventID: id, disposition: value, comment: note) }
                    }.disabled(detail == nil || workspace.signalPending.contains(.review))
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
        .onChange(of: signal.id, initial: true) { _, _ in seeded = nil; seed() }
        .onChange(of: detail?.id) { _, _ in seed() }
    }

    private func seed() {
        guard let detail, seeded != detail.id else { return }
        seeded = detail.id
        verdict = detail.value["disposition"].text.isEmpty ? "unreviewed" : detail.value["disposition"].text
        comment = detail.value["operator_comment"].text
    }
}
