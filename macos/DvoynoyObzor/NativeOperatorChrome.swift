import SwiftUI
import SpektrKomandy

/// Compact operator controls. Connection setup stays separate from acquisition.
struct NativeOperatorHeader: View {
    @Bindable var model: RemoteDualModel
    @Binding var setupExpanded: Bool
    let workspace: NativeWorkspace
    let openWorkspace: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Button(action: openWorkspace) { Label(workspace.title, systemImage: workspace.symbol) }
                .accessibilityIdentifier("workbench.space")
                .help("Рабочее пространство этого клиента; переключение не останавливает охрану")
            sessionPicker.frame(maxWidth: 400)
            Spacer()
            TimelineView(.periodic(from: .now, by: 1)) { context in
                Text(Self.utc(context.date)).monospacedDigit().font(.caption).foregroundStyle(.secondary)
            }
            Button("Подключение") { setupExpanded = true }
        }.controlSize(.small).padding(.horizontal, 10).padding(.vertical, 3)
    }

    private var title: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("Радиоэлектронная обстановка").font(.title2.weight(.semibold))
            Text(model.coreReachable ? "Наблюдение через ядро" : "Подключите ядро для наблюдения")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var sessionPicker: some View {
        Picker("Сеанс", selection: Binding(get: { model.selectedSessionID }, set: model.selectSession).exactID) {
            Text("Не выбран").tag(PlatformExactID(""))
            ForEach(model.sessions, id: \.exactID) { Text($0.name).tag($0.exactID) }
        }.disabled(!model.connected || model.busy).accessibilityIdentifier("workbench.session")
    }

    private static func utc(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "HH:mm:ss 'UTC'"
        return formatter.string(from: date)
    }
}

struct NativeOperatorMetric: View {
    let title: String
    let value: String
    var symbol: String
    var tint: Color = .secondary

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Label(title, systemImage: symbol).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.system(.callout, design: .monospaced, weight: .medium))
                .foregroundStyle(tint).textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .combine)
    }
}

struct NativeSpectrumReadout: View {
    let points: [PlotPoint]

    var body: some View {
        let first = points.first?.x
        let last = points.last?.x
        let peak = points.max { $0.y < $1.y }
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 8)], spacing: 8) {
            NativeOperatorMetric(title: "Начало · МГц", value: frequency(first), symbol: "arrow.right.to.line")
            NativeOperatorMetric(title: "Конец · МГц", value: frequency(last), symbol: "arrow.left.to.line")
            NativeOperatorMetric(title: "Пик отсчётов · дБм", value: peak.map { String(format: "%.1f", $0.y) } ?? "—", symbol: "waveform.path", tint: .primary)
            NativeOperatorMetric(title: "Частота пика · МГц", value: frequency(peak?.x), symbol: "scope")
        }
    }

    private func frequency(_ value: Double?) -> String {
        value.map { String(format: "%.5f", $0) } ?? "—"
    }
}
