import Foundation
import SwiftUI
import SpektrKomandy

/// Independent, read-only analysis. Snapshot updates change only the next request's source.
@MainActor
final class SweepThresholdModel: ObservableObject {
    struct Scope: Equatable {
        let server: String
        let session: String
        let stream: String
    }
    @Published var windowPoints = "21"
    @Published var marginDb = "6"
    @Published private(set) var source: PlatformSweep?
    @Published private(set) var result: PlatformSweepThreshold?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, Int, Double) async throws -> PlatformSweepThreshold

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, Int, Double) async throws -> PlatformSweepThreshold = {
             try await $0.sweepThreshold(sessionID: $1, sweepID: $2, windowPoints: $3, marginDb: $4)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.fetch = fetch
    }

    var canAnalyze: Bool { scope != nil && source != nil && client != nil }

    func configure(client: PlatformClient?, scope: Scope?, source: PlatformSweep?) {
        if self.scope != scope || self.client !== client { cancel() }
        self.client = client
        self.scope = scope
        self.source = source.flatMap { sweep in
            guard sweep.sessionId == scope?.session, sweep.streamId == scope?.stream,
                  sweep.levelUnit == "dBm" else { return nil }
            return sweep
        }
    }

    func reset() { configure(client: nil, scope: nil, source: nil) }

    func cancel() {
        revision += 1
        request?.cancel()
        timeout?.cancel()
        request = nil
        timeout = nil
        pending = false
        result = nil
        errorText = nil
    }

    func analyze() {
        guard let client, let scope, let source else { return }
        cancel()
        guard let window = Int(windowPoints.trimmingCharacters(in: .whitespacesAndNewlines)),
              let margin = parseUserNumber(marginDb),
              PlatformSweepThresholdParameters.valid(windowPoints: window, marginDb: margin) else {
            errorText = "Укажите нечётное окно 3–101 и конечный запас 0–120 dB."
            return
        }
        let currentRevision = revision
        pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, source.id, window, margin)
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                guard value.streamId == scope.stream else {
                    throw PlatformError.invalidResponse("Сервер вернул порог другого потока")
                }
                self.result = value
                self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Свип не найден или сервер не поддерживает адаптивный порог (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Свип несовместим с адаптивным порогом (409): \(message)"
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, currentRevision), self.pending else { return }
            self.cancel()
            self.errorText = "Время ожидания адаптивного порога истекло. Можно повторить запрос."
        }
    }

    private func matches(_ scope: Scope, _ client: PlatformClient, _ revision: Int) -> Bool {
        self.scope == scope && self.client === client && self.revision == revision
    }

    private func finish() {
        timeout?.cancel()
        timeout = nil
        request = nil
        pending = false
    }
}

struct RemoteSweepThresholdPane: View {
    @ObservedObject var model: SweepThresholdModel

    var body: some View {
        GroupBox("Адаптивный порог архивного свипа · dBm") {
            VStack(alignment: .leading, spacing: 8) {
                if let source = model.source {
                    Text("Текущий архивный свип: \(source.id)")
                    Text("Поток: \(source.streamId) · UTC \(source.interval.startedAt) — \(source.interval.endedAt)")
                        .font(.caption)
                } else { Text("Нет архивного dBm свипа выбранного потока").foregroundStyle(.secondary) }
                HStack {
                    TextField("Нечётное окно 3–101", text: $model.windowPoints)
                    TextField("Запас 0–120 dB", text: $model.marginDb)
                }.textFieldStyle(.roundedBorder)
                HStack {
                    Button("Рассчитать порог") { model.analyze() }.disabled(!model.canAnalyze || model.pending)
                    Button(model.pending ? "Отмена" : "Очистить результат") { model.cancel() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
                Text("Только чтение и расчёт ядром по кнопке. Локальная медиана включает центральную точку; края окна обрезаются. Это не калибровка и не автоматическое создание кандидатов. Новые свипы и правки параметров не меняют показанный результат.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func resultView(_ result: PlatformSweepThreshold) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Результат: \(result.sweep.id) · сеанс \(result.sessionId) · поток \(result.streamId)")
            Text("UTC: \(result.sweep.interval.startedAt) — \(result.sweep.interval.endedAt)")
            Text("Зафиксированные параметры: rolling_median · окно \(result.parameters.windowPoints) · запас \(result.parameters.marginDb) dB · clip · строго >")
            DisclosureGroup("Записанные настройки источника") {
                Text(result.sweep.settings.keys.sorted().map {
                    "\($0): \(settingText(result.sweep.settings[$0]!))"
                }.joined(separator: "\n"))
            }
            SweepThresholdChart(result: result)
            Text("\(result.points) точек · \(result.exceedancePoints) превышений · \(result.regionCount) непрерывных областей")
            if result.regions.isEmpty { Text("Строгих превышений нет") }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(result.regions) { region in
                        Text("Индексы \(region.startIndex)…\(region.endIndex) · \(region.startFrequencyHz)…\(region.endFrequencyHz) Гц · \(region.points) точек · пик \(region.peakFrequencyHz) Гц (индекс \(region.peakIndex)): \(String(format: "%+.2f", region.peakExcessDb)) dB")
                    }
                }
            }.frame(maxHeight: 180)
        }.font(.caption)
    }

    private func settingText(_ value: PlatformComparisonSetting) -> String {
        switch value {
        case .string(let text): return "\"\(text)\""
        case .number(let number): return String(number)
        case .boolean(let bool): return bool ? "true" : "false"
        case .null: return "null"
        case .array(let values): return "[" + values.map(settingText).joined(separator: ", ") + "]"
        case .object(let values):
            return "{" + values.keys.sorted().map { "\($0): \(settingText(values[$0]!))" }.joined(separator: ", ") + "}"
        }
    }
}

/// Full bin arrays, including duplicated frequency bins, are rendered without decimation.
struct SweepThresholdChart: View {
    let result: PlatformSweepThreshold
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Canvas { context, size in
                let levels = result.sweep.levels + result.thresholdLevels
                let scale = TraceChartVerticalScale(minimum: levels.min()!, maximum: levels.max()!)
                let first = Double(result.frequenciesHz.first!)
                let last = Double(result.frequenciesHz.last!)
                let span = max(last - first, 1)
                func point(_ index: Int, _ level: Double) -> CGPoint {
                    CGPoint(x: 8 + (Double(result.frequenciesHz[index]) - first) / span * max(size.width - 16, 1),
                            y: 8 + (1 - scale.fraction(level)) * max(size.height - 16, 1))
                }
                func path(_ values: [Double]) -> Path {
                    var path = Path()
                    path.move(to: point(0, values[0]))
                    for index in values.indices.dropFirst() { path.addLine(to: point(index, values[index])) }
                    return path
                }
                context.stroke(path(result.sweep.levels), with: .color(.primary), lineWidth: 1.6)
                context.stroke(path(result.thresholdLevels), with: .color(.orange), style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
            }
            .frame(height: 220)
            .background(Color.primary.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            Text("Сплошная: исходные dBm · оранжевый пунктир: производный порог dBm")
            Text("Частота, Гц: \(result.frequenciesHz.first!)…\(result.frequenciesHz.last!) · превышение измеряется отдельно в dB")
        }.font(.caption)
    }
}
