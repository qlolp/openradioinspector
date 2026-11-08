import Foundation
import SwiftUI
import SpektrKomandy

/// Value-type snapshots are local only. Polling changes the next target, never a pin or completed result.
@MainActor
final class SweepAverageModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (left: Self, right: Self) -> Bool {
            PlatformThreeSweepThresholds.sameID(left.server, right.server)
                && PlatformThreeSweepThresholds.sameID(left.session, right.session)
                && PlatformThreeSweepThresholds.sameID(left.stream, right.stream)
        }
    }
    @Published var countText = "3" { didSet { if countText != oldValue { clear() } } }
    @Published var minimumIncreaseText = "6" { didSet { if minimumIncreaseText != oldValue { cancelRequest() } } }
    @Published var minimumBinsText = "2" { didSet { if minimumBinsText != oldValue { cancelRequest() } } }
    @Published private(set) var sources: [PlatformSweep] = []
    @Published private(set) var target: PlatformSweep?
    @Published private(set) var result: PlatformSweepAverageChangeDetection?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var window: [PlatformSweep] = []
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, [String], String, Double, Int) async throws -> PlatformSweepAverageChangeDetection

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, [String], String, Double, Int) async throws -> PlatformSweepAverageChangeDetection = {
             try await $0.sweepAverageChangeDetection(
                sessionID: $1, referenceIDs: $2, targetID: $3,
                minimumIncreaseDb: $4, minimumBins: $5,
             )
         }) { self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch }

    var count: Int? {
        guard let count = Int(countText.trimmingCharacters(in: .whitespacesAndNewlines)), (2...16).contains(count) else { return nil }
        return count
    }
    var receivedCount: Int { window.count }
    var canPin: Bool { client != nil && scope != nil && count.map { window.count >= $0 + 1 } == true }
    var canCompare: Bool { client != nil && scope != nil && !sources.isEmpty && target != nil }
    var pinnedUTC: String {
        guard let interval = PlatformSweepAverageComparison.sourceInterval(sources) else { return "—" }
        return "\(interval.startedAt) — \(interval.endedAt)"
    }

    func configure(client: PlatformClient?, scope: Scope?, sweeps: [PlatformSweep]) {
        if self.scope != scope || self.client !== client { clear() }
        self.client = client; self.scope = scope
        window = sweeps.filter {
            PlatformThreeSweepThresholds.sameID($0.sessionId, scope?.session)
                && PlatformThreeSweepThresholds.sameID($0.streamId, scope?.stream) && $0.levelUnit == "dBm"
        }
        target = window.last
    }
    func reset() { configure(client: nil, scope: nil, sweeps: []) }
    private func cancelRequest() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil
    }
    func clear() { cancelRequest(); sources = [] }
    func pin() {
        clear()
        guard let count else { errorText = "N должно быть целым числом 2–16."; return }
        guard canPin, let target else {
            errorText = "Нужно не менее \(count + 1) dBm свипов выбранного потока в полученном окне: N предыдущих и отдельный последний."
            return
        }
        let frozen = Array(window.dropLast().suffix(count))
        guard PlatformSweepAverageComparison.validSources(frozen, target: target) else {
            errorText = "Нужны разные совместимые dBm свипы с известными настройками, общей сеткой и UTC."
            return
        }
        sources = frozen
    }
    func compare() {
        guard !pending, let client, let scope, let target, !sources.isEmpty else { return }
        cancelRequest()
        guard let minimumIncreaseDb = Double(minimumIncreaseText.trimmingCharacters(in: .whitespacesAndNewlines)),
              minimumIncreaseDb.isFinite, (0...120).contains(minimumIncreaseDb),
              let minimumBins = Int(minimumBinsText.trimmingCharacters(in: .whitespacesAndNewlines)),
              (1...16_384).contains(minimumBins) else {
            errorText = "Минимальный прирост должен быть 0–120 dB, ширина — целым числом 1–16384 точек."
            return
        }
        let frozen = sources
        guard PlatformSweepAverageComparison.validSources(frozen, target: target) else {
            errorText = "Последний свип несовместим с эталоном или уже входит в него. Закрепите новый набор."
            return
        }
        let token = revision; pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(
                    client, scope.session, frozen.map(\.id), target.id,
                    minimumIncreaseDb, minimumBins,
                )
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                try value.validate(
                    sources: frozen, target: target,
                    minimumIncreaseDb: minimumIncreaseDb, minimumBins: minimumBins,
                )
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Свипы не найдены или сервер не поддерживает обнаружение изменений (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Несовместимые источники усреднённого эталона (409): \(message)"
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, token), self.pending else { return }
            self.cancelRequest(); self.errorText = "Время ожидания сравнения истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ token: Int) -> Bool {
        self.scope == scope && self.client === client && self.revision == token
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteSweepAveragePane: View {
    @ObservedObject var model: SweepAverageModel
    var body: some View {
        GroupBox("Локальный усреднённый эталон · mean_power") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("N: 2–16", text: $model.countText).textFieldStyle(.roundedBorder).frame(width: 75)
                    TextField("Прирост dB: 0–120", text: $model.minimumIncreaseText).textFieldStyle(.roundedBorder).frame(width: 130)
                    TextField("Точек: 1–16384", text: $model.minimumBinsText).textFieldStyle(.roundedBorder).frame(width: 120)
                    Button("PIN · закрепить N предыдущих") { model.pin() }.disabled(model.pending)
                    Button("Сравнить и найти изменения") { model.compare() }.disabled(!model.canCompare || model.pending)
                    Button(model.pending ? "Отмена и очистка" : "Очистить эталон") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                Text("Получено dBm свипов: \(model.receivedCount). Требуется N+1; последний не входит в эталон.")
                Text("Следующая цель: \(model.target?.id ?? "нет")")
                if !model.sources.isEmpty {
                    Text("Закреплено: \(model.sources.count) · source_ids: \(model.sources.map(\.id).joined(separator: ", "))")
                    Text("UTC эталона: \(model.pinnedUTC)")
                    DisclosureGroup("Зафиксированные источники и настройки") {
                        ForEach(model.sources) { source in
                            Text("\(source.id) · UTC \(source.interval.startedAt) — \(source.interval.endedAt)\n\(averageSettingsText(source.settings ?? [:]))")
                        }
                    }
                }
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
                Text("Равновесное среднее линейной мощности отображается в dBm; ядро находит непрерывные области, где target превышает опорный на заданную величину в dB. Только чтение и расчёт по кнопке: кандидаты, события и архив не меняются. Новые polling/SSE данные не меняют закреплённые источники или результат.")
                    .foregroundStyle(.secondary)
            }.font(.caption).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
    private func resultView(_ result: PlatformSweepAverageChangeDetection) -> some View {
        let comparison = result.comparison
        return VStack(alignment: .leading, spacing: 5) {
            Text("Результат ядра · сеанс \(comparison.sessionId) · поток \(comparison.streamId) · \(comparison.points) точек")
            Text("mean_power · N=\(comparison.reference.count) · источники: \(comparison.reference.sourceIds.joined(separator: ", "))")
            Text("UTC эталона: \(comparison.reference.interval.startedAt) — \(comparison.reference.interval.endedAt)")
            Text("Цель: \(comparison.target.id) · UTC \(comparison.target.interval.startedAt) — \(comparison.target.interval.endedAt)")
            DisclosureGroup("Настройки цели") { Text(averageSettingsText(comparison.target.settings)) }
            SweepAverageChart(frequencies: comparison.frequenciesHz, traces: [comparison.reference.levels, comparison.target.levels], unit: "dBm", labels: "Оранжевый □: среднее мощности · синий ●: исходная цель")
            SweepAverageChart(
                frequencies: comparison.frequenciesHz, traces: [comparison.deltaLevels],
                unit: "dB", labels: "Разность со знаком: цель − среднее мощности",
                regions: result.detection.regions,
            )
            Text("Макс. изменение: \(String(format: "%+.3f", comparison.maxIncrease.deltaDb)) dB @ \(comparison.maxIncrease.frequencyHz) Гц · мин.: \(String(format: "%+.3f", comparison.minChange.deltaDb)) dB @ \(comparison.minChange.frequencyHz) Гц")
            Text("Порог ≥ \(result.detection.minimumIncreaseDb) dB · ширина ≥ \(result.detection.minimumBins) точек · областей: \(result.detection.count)")
            ForEach(Array(result.detection.regions.enumerated()), id: \.offset) { item in
                let region = item.element
                Text("\(region.startFrequencyHz)…\(region.endFrequencyHz) Гц · пик +\(String(format: "%.3f", region.peakIncreaseDb)) dB @ \(region.peakFrequencyHz) Гц · \(region.binCount) точек")
            }
        }
    }
}

private func averageSettingsText(_ settings: [String: PlatformComparisonSetting]) -> String {
    func text(_ value: PlatformComparisonSetting) -> String {
        switch value {
        case .string(let string): return "\"\(string)\""
        case .number(let number): return String(number)
        case .boolean(let bool): return bool ? "true" : "false"
        case .null: return "null"
        case .array(let items): return "[" + items.map(text).joined(separator: ", ") + "]"
        case .object(let items): return "{" + averageSettingsText(items) + "}"
        }
    }
    return settings.keys.sorted().map { "\($0): \(text(settings[$0]!))" }.joined(separator: ", ")
}

/// No decimation; every bin is rendered, including one-bin and duplicate-frequency measurements.
struct SweepAverageChart: View {
    let frequencies: [Int64]
    let traces: [[Double]]
    let unit: String
    let labels: String
    var regions: [PlatformSweepChangeRegion] = []
    var body: some View {
        let levels = traces.flatMap { $0 }
        VStack(alignment: .leading, spacing: 4) {
            Text("\(labels) · \(unit) · диапазон \(String(format: "%+.3f", levels.min()!))…\(String(format: "%+.3f", levels.max()!))")
            Canvas { context, size in
                let scale = TraceChartVerticalScale(minimum: levels.min()!, maximum: levels.max()!)
                let first = Double(frequencies.first!), last = Double(frequencies.last!)
                func point(_ index: Int, _ value: Double) -> CGPoint {
                    CGPoint(x: first == last ? size.width / 2 : 8 + (Double(frequencies[index]) - first) / (last - first) * max(size.width - 16, 1),
                            y: 8 + (1 - scale.fraction(value)) * max(size.height - 16, 1))
                }
                for region in regions {
                    let start = point(region.startIndex, traces[0][region.startIndex]).x
                    let end = point(region.endIndex, traces[0][region.endIndex]).x
                    let rect = CGRect(x: min(start, end) - 2, y: 4, width: max(4, abs(end - start) + 4), height: max(size.height - 8, 1))
                    context.fill(Path(rect), with: .color(.green.opacity(0.13)))
                }
                for (index, trace) in traces.enumerated() {
                    let color = index == 0 ? Color.orange : Color.blue
                    var path = Path(); path.move(to: point(0, trace[0]))
                    for bin in trace.indices.dropFirst() { path.addLine(to: point(bin, trace[bin])) }
                    context.stroke(path, with: .color(color), lineWidth: 1.5)
                    // Markers keep a single bin and coincident/duplicated frequencies visible.
                    for bin in trace.indices {
                        let p = point(bin, trace[bin])
                        let marker = SweepAverageMarker.bounds(center: p, reference: index == 0)
                        if index == 0 { context.stroke(Path(marker), with: .color(color), lineWidth: 1.5) }
                        else { context.fill(Path(ellipseIn: marker), with: .color(color)) }
                    }
                }
            }.frame(height: 180).background(Color.primary.opacity(0.04))
            Text("Частота, Гц: \(frequencies.first!)…\(frequencies.last!) · все \(frequencies.count) точки")
        }
    }
}

/// A larger hollow reference square remains visible behind a coincident filled target circle.
enum SweepAverageMarker {
    static func bounds(center: CGPoint, reference: Bool) -> CGRect {
        let radius: CGFloat = reference ? 4 : 2
        return CGRect(x: center.x - radius, y: center.y - radius, width: 2 * radius, height: 2 * radius)
    }
}
