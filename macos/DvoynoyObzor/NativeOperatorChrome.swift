import SwiftUI
import SpektrKomandy

/// Compact operator controls. Connection setup stays separate from acquisition.
struct NativeOperatorHeader: View {
    @ObservedObject var model: RemoteDualModel
    @Binding var setupExpanded: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("ДВОЙНОЙ ОБЗОР", systemImage: "waveform.path")
                    .font(.system(.caption, design: .monospaced, weight: .semibold)).tracking(2)
                Spacer()
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    Text(Self.utc(context.date)).monospacedDigit().font(.caption)
                        .foregroundStyle(.secondary).accessibilityLabel("Время UTC: \(Self.utc(context.date))")
                }
                Button { setupExpanded.toggle() } label: {
                    Label("Подключение", systemImage: "slider.horizontal.3")
                }.help("Ядро, доступ и настройка сеанса")
            }
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 16) {
                    title
                    Spacer(minLength: 8)
                    sessionPicker.frame(width: 310)
                }
                VStack(alignment: .leading, spacing: 8) { title; sessionPicker }
            }
        }
        .padding(.horizontal, 18).padding(.vertical, 12)
        .background(.background)
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
        }.disabled(!model.connected || model.busy)
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
