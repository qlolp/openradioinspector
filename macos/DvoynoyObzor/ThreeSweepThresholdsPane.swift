import Foundation
import SwiftUI
import SpektrKomandy

/// Polling advances only the next source. Each explicit analysis captures the full value-type sweep.
@MainActor
final class ThreeSweepThresholdsModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (left: Self, right: Self) -> Bool {
            PlatformThreeSweepThresholds.sameID(left.server, right.server)
                && PlatformThreeSweepThresholds.sameID(left.session, right.session)
                && PlatformThreeSweepThresholds.sameID(left.stream, right.stream)
        }
    }
    @Published var line1Text = "-90" { didSet { if line1Text != oldValue { clear() } } }
    @Published var line2Text = "-80" { didSet { if line2Text != oldValue { clear() } } }
    @Published var line3Text = "-70" { didSet { if line3Text != oldValue { clear() } } }
    @Published private(set) var source: PlatformSweep?
    @Published private(set) var result: PlatformThreeSweepThresholds?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, [Double]) async throws -> PlatformThreeSweepThresholds

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, [Double]) async throws -> PlatformThreeSweepThresholds = {
             try await $0.threeSweepThresholds(sessionID: $1, sweepID: $2, levelsDbm: $3)
         }) { self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch }

    var levels: [Double]? {
        let values = [line1Text, line2Text, line3Text].compactMap(parseUserNumber)
        return PlatformThreeSweepThresholds.validLevels(values) ? values : nil
    }
    var canAnalyze: Bool { client != nil && scope != nil && source != nil }

    func configure(client: PlatformClient?, scope: Scope?, source: PlatformSweep?) {
        if self.scope != scope || self.client !== client { clear() }
        self.client = client; self.scope = scope
        self.source = source.flatMap {
            PlatformThreeSweepThresholds.sameID($0.sessionId, scope?.session)
                && PlatformThreeSweepThresholds.sameID($0.streamId, scope?.stream) && $0.levelUnit == "dBm" ? $0 : nil
        }
    }
    func reset() { configure(client: nil, scope: nil, source: nil) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil
    }
    func analyze() {
        guard !pending, let client, let scope, let source else { return }
        clear()
        guard let levels else { errorText = "Введите ровно три конечных уровня dBm. Порядок произвольный, равные уровни допустимы."; return }
        guard PlatformThreeSweepThresholds.validSource(source) else {
            errorText = "Нужен архивный dBm свип с известными настройками, целыми частотами и корректным UTC."
            return
        }
        let token = revision; pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, source.id, levels)
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                try value.validate(source: source, levelsDbm: levels)
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Свип не найден или сервер не поддерживает три порога (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Свип несовместим с тремя порогами (409): \(message)"
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, token), self.pending else { return }
            self.clear(); self.errorText = "Время ожидания трёх порогов истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ token: Int) -> Bool {
        self.scope == scope && self.client === client && revision == token
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteThreeSweepThresholdsPane: View {
    @ObservedObject var model: ThreeSweepThresholdsModel
    var body: some View {
        GroupBox("Три горизонтальных порога · dBm") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Следующий архивный свип: \(model.source?.id ?? "нет dBm свипа выбранного потока")")
                HStack {
                    input("line-1 ◇", text: $model.line1Text)
                    input("line-2 ○", text: $model.line2Text)
                    input("line-3 □", text: $model.line3Text)
                }
                Text("−90 / −80 / −70 dBm: только начальные примеры, не нормативные пределы. Ровно три конечных числа, любой порядок, равные уровни допустимы.")
                    .foregroundStyle(.secondary)
                HStack {
                    Button("Анализировать последний") { model.analyze() }.disabled(!model.canAnalyze || model.pending)
                    Button(model.pending ? "Отмена и очистка" : "Очистить результат") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
                Text("Расчёт ядром только по кнопке: three_horizontal · strictly_greater. Каждая линия независима; общих OR-счётчиков нет. Новые свипы не меняют результат. Правки уровней очищают его. Это не калибровка, не маска и не автоматический детектор; записи в архив нет.")
                    .foregroundStyle(.secondary)
            }.font(.caption).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
    private func input(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading) {
            Text("\(title) · dBm")
            TextField(title, text: text).textFieldStyle(.roundedBorder).accessibilityLabel("\(title), уровень dBm")
        }
    }
    private func resultView(_ result: PlatformThreeSweepThresholds) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Зафиксировано: \(result.sweep.id) · сеанс \(result.sessionId) · поток \(result.streamId)")
            Text("UTC: \(result.sweep.interval.startedAt) — \(result.sweep.interval.endedAt)")
            Text("Уровни dBm: " + result.lines.map { "\($0.id) = \(ThreeThresholdDisplay.number($0.levelDbm))" }.joined(separator: " · "))
            DisclosureGroup("Полные записанные настройки, включая sequence") {
                Text(threeSettingsText(result.sweep.settings))
            }
            ThreeSweepThresholdsChart(result: result, excess: false)
            ThreeSweepThresholdsChart(result: result, excess: true)
            ForEach(result.lines) { line in
                Text("\(line.id) · \(ThreeThresholdDisplay.number(line.levelDbm)) dBm · \(line.exceedancePoints) строгих превышений · \(line.regionCount) непрерывных областей")
                    .fontWeight(.semibold)
                if line.regions.isEmpty { Text("Строгих превышений нет") }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 5) {
                        ForEach(line.regions) { region in
                            Text("Индексы \(region.startIndex)…\(region.endIndex) · \(region.startFrequencyHz)…\(region.endFrequencyHz) Гц · \(region.points) точек · первый пик \(region.peakFrequencyHz) Гц (индекс \(region.peakIndex)): \(ThreeThresholdDisplay.number(region.peakExcessDb)) dB")
                        }
                    }
                }.frame(maxHeight: 150)
                DisclosureGroup("Все \(result.points) превышения dB: \(line.id)") {
                    ScrollView {
                        LazyVStack(alignment: .leading) {
                            ForEach(line.excessLevels.indices, id: \.self) { index in
                                Text("[\(index)] \(result.frequenciesHz[index]) Гц: \(ThreeThresholdDisplay.number(line.excessLevels[index])) dB")
                            }
                        }
                    }.frame(height: 150)
                }
            }
        }
    }
}

private func threeSettingsText(_ settings: [String: PlatformComparisonSetting]) -> String {
    func text(_ value: PlatformComparisonSetting) -> String {
        switch value {
        case .string(let string): return "\"\(string)\""
        case .number(let number): return String(number)
        case .boolean(let bool): return bool ? "true" : "false"
        case .null: return "null"
        case .array(let items): return "[" + items.map(text).joined(separator: ", ") + "]"
        case .object(let items): return "{" + threeSettingsText(items) + "}"
        }
    }
    return settings.keys.sorted().map { "\($0): \(text(settings[$0]!))" }.joined(separator: ", ")
}

/// Avoid SwiftUI's default localized Double precision hiding the submitted threshold or returned excess.
enum ThreeThresholdDisplay {
    static func number(_ value: Double) -> String { String(value) }
}

/// Four nested shapes preserve identities at coincident levels and one-bin/collapsed grids.
enum ThreeThresholdMarker {
    static let radii: [CGFloat] = [2, 4, 7, 10]
    static func bounds(center: CGPoint, identity: Int) -> CGRect {
        let radius = radii[identity]
        return CGRect(x: center.x - radius, y: center.y - radius, width: 2 * radius, height: 2 * radius)
    }
    static func path(center: CGPoint, identity: Int) -> Path {
        let rect = bounds(center: center, identity: identity)
        if identity == 0 || identity == 2 { return Path(ellipseIn: rect) }
        if identity == 3 { return Path(rect) }
        var path = Path(); path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY)); path.addLine(to: CGPoint(x: rect.midX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.midY)); path.closeSubpath(); return path
    }
}

struct ThreeSweepThresholdsChart: View {
    let result: PlatformThreeSweepThresholds
    let excess: Bool
    static func traces(_ result: PlatformThreeSweepThresholds, excess: Bool) -> [[Double]] {
        excess ? result.lines.map(\.excessLevels)
            : [result.sweep.levels] + result.lines.map { Array(repeating: $0.levelDbm, count: result.points) }
    }
    nonisolated static func x(_ frequency: Int64, first: Int64, last: Int64, width: CGFloat) -> CGFloat {
        first == last ? width / 2 : 12 + CGFloat(Double(frequency - first) / Double(last - first)) * max(width - 24, 1)
    }
    var body: some View {
        let traces = Self.traces(result, excess: excess)
        let levels = traces.flatMap { $0 }
        VStack(alignment: .leading, spacing: 4) {
            Text(excess ? "Превышения со знаком, dB: исходный уровень − соответствующий порог" : "Исходный свип и три горизонтальных порога, dBm")
            Text("Диапазон \(ThreeThresholdDisplay.number(levels.min()!))…\(ThreeThresholdDisplay.number(levels.max()!)) \(excess ? "dB" : "dBm")")
            Canvas { context, size in
                let scale = TraceChartVerticalScale(minimum: levels.min()!, maximum: levels.max()!)
                func point(_ index: Int, _ value: Double) -> CGPoint {
                    CGPoint(x: Self.x(result.frequenciesHz[index], first: result.frequenciesHz.first!, last: result.frequenciesHz.last!, width: size.width),
                            y: 12 + (1 - scale.fraction(value)) * max(size.height - 24, 1))
                }
                let colors: [Color] = [.primary, .orange, .blue, .purple]
                let dashes: [[CGFloat]] = [[], [8, 4], [3, 3], [10, 3, 2, 3]]
                for index in traces.indices.reversed() {
                    let identity = excess ? index + 1 : index, values = traces[index]
                    var path = Path()
                    if !excess && identity > 0 {
                        let y = point(0, values[0]).y
                        path.move(to: CGPoint(x: 12, y: y)); path.addLine(to: CGPoint(x: max(size.width - 12, 12), y: y))
                    } else {
                        path.move(to: point(0, values[0]))
                        for bin in values.indices.dropFirst() { path.addLine(to: point(bin, values[bin])) }
                    }
                    context.stroke(path, with: .color(colors[identity]), style: StrokeStyle(lineWidth: 1.3, dash: dashes[identity]))
                    // No bin sampling: every raw bin and each line's marker remains represented.
                    for bin in values.indices {
                        let marker = ThreeThresholdMarker.path(center: point(bin, values[bin]), identity: identity)
                        if identity == 0 { context.fill(marker, with: .color(colors[identity])) }
                        else { context.stroke(marker, with: .color(colors[identity]), lineWidth: 1.3) }
                    }
                }
            }.frame(height: 220).background(Color.primary.opacity(0.04))
            Text("\(excess ? "" : "● исходный: сплошная · ")◇ line-1: оранжевый длинный пунктир · ○ line-2: синий короткий пунктир · □ line-3: фиолетовый штрихпунктир")
            Text("Частота, Гц: \(result.frequenciesHz.first!)…\(result.frequenciesHz.last!) · все \(result.points) точки, включая повторяющиеся частоты")
        }
    }
}
