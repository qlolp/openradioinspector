import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class SweepContextModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformSweepContext.sameID(a.server, b.server) && PlatformSweepContext.sameID(a.session, b.session)
                && PlatformSweepContext.sameID(a.stream, b.stream)
        }
    }
    @Published var beforeText = "2" { didSet { if beforeText != oldValue { clear() } } }
    @Published var afterText = "2" { didSet { if afterText != oldValue { clear() } } }
    @Published private(set) var choices: [PlatformSweep] = []
    @Published private(set) var anchor: PlatformSweep?
    @Published private(set) var frozenAnchor: PlatformSweep?
    @Published private(set) var result: PlatformSweepContext?
    @Published private(set) var selectedOffset = 0
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, Int, Int) async throws -> PlatformSweepContext

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, Int, Int) async throws -> PlatformSweepContext = {
             try await $0.sweepContext(sessionID: $1, sweepID: $2, before: $3, after: $4)
         }) { self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch }

    private func count(_ text: String) -> Int? {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.utf8.allSatisfy({ (48...57).contains($0) }),
              let value = Int(text), PlatformContextCounts.valid(value) else { return nil }
        return value
    }
    var before: Int? { count(beforeText) }
    var after: Int? { count(afterText) }
    var canLoad: Bool { client != nil && scope != nil && anchor != nil }
    var selectedItem: PlatformContextItem? { result?.items.first(where: { $0.offset == selectedOffset }) }

    func configure(client: PlatformClient?, scope: Scope?, sweeps: [PlatformSweep]) {
        if self.scope != scope || self.client !== client { clear(); anchor = nil; choices = [] }
        self.client = client; self.scope = scope
        var seen = Set<Data>()
        choices = sweeps.filter {
            PlatformSweepContext.sameID($0.sessionId, scope?.session) && PlatformSweepContext.sameID($0.streamId, scope?.stream)
                && PlatformSweepContext.validSource($0) && seen.insert(Data($0.id.utf8)).inserted
        }.sorted(by: PlatformSweepContext.sourcePrecedes)
        // Polling changes only the menu. It cannot replace a full-value selection or frozen result.
        if anchor == nil { anchor = choices.last }
    }
    func selectAnchor(id: String) {
        guard let value = choices.first(where: { PlatformSweepContext.sameID($0.id, id) }) else { return }
        clear(); anchor = value
    }
    func selectRow(offset: Int) {
        guard result?.items.contains(where: { $0.offset == offset }) == true else { return }
        selectedOffset = offset
    }
    func reset() { configure(client: nil, scope: nil, sweeps: []) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; frozenAnchor = nil; selectedOffset = 0; errorText = nil
    }
    func load() {
        guard !pending, let client, let scope, let anchor else { return }
        clear()
        guard let before, let after else { errorText = "Введите целые N1 и N2 от 0 до 16."; return }
        guard PlatformSweepContext.validSource(anchor) else { errorText = "Нужен архивный dBm свип с известными настройками и корректным UTC."; return }
        let token = revision; pending = true; frozenAnchor = anchor
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, anchor.id, before, after)
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                try value.validate(anchor: anchor, before: before, after: after)
                self.result = value; self.selectedOffset = 0; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Сеанс или свип не найден; старый сервер может не поддерживать архивное окно (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Окно несовместимо по сетке, единицам, настройкам или числу точек (409): \(message)"
                case PlatformError.http(status: 413, code: _, message: _):
                    self.errorText = "Запрос превышает предел HTTP-тела сервера (413)."
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, token), self.pending else { return }
            self.clear(); self.errorText = "Время ожидания архивного окна истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ token: Int) -> Bool {
        self.scope == scope && self.client === client && revision == token
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteSweepContextPane: View {
    @ObservedObject var model: SweepContextModel
    var body: some View {
        GroupBox("Архивное окно N1/N2 · исходные dBm") {
            VStack(alignment: .leading, spacing: 8) {
                Menu("Выбрать опорный свип из полученных (\(model.choices.count))") {
                    ForEach(model.choices.indices, id: \.self) { index in
                        let sweep = model.choices[index]
                        Button("\(sweep.id) · \(sweep.interval.startedAt)") { model.selectAnchor(id: sweep.id) }
                    }
                }.disabled(model.choices.isEmpty)
                Text("Опорный свип: \(model.anchor?.id ?? "нет подходящего свипа")").fixedSize(horizontal: false, vertical: true)
                HStack {
                    input("N1 · до", text: $model.beforeText)
                    input("N2 · после", text: $model.afterText)
                }
                Text("N1 = 2 и N2 = 2 — начальные примеры. Допустимо 0…16 с каждой стороны. Для соседей после опорного выберите более ранний свип.").foregroundStyle(.secondary)
                HStack {
                    Button("Загрузить окно") { model.load() }.disabled(!model.canLoad || model.pending)
                    Button(model.pending ? "Отмена и очистка" : "Очистить окно") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
                Text("Только чтение архива по кнопке. Ближайшие свипы того же сеанса и потока упорядочены по UTC начала, затем точному ID. Опрос обновляет только варианты выбора. Выбранный свип и результат не меняются. Это основа N1/N2, не автоматический захват, не настройка ControlTask и не связь с кандидатом.")
                    .foregroundStyle(.secondary)
            }.font(.caption).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
    private func input(_ title: String, text: Binding<String>) -> some View {
        HStack { Text(title); TextField("0…16", text: text).textFieldStyle(.roundedBorder).frame(width: 65).accessibilityLabel(title) }
    }
    private func resultView(_ result: PlatformSweepContext) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Зафиксировано: \(result.anchorId) · сеанс \(result.sessionId) · поток \(result.streamId)")
            Text("\(result.complete ? "Полное окно" : "Неполное окно"): запрошено до/после \(result.requested.before)/\(result.requested.after), получено \(result.available.before)/\(result.available.after), недостаёт \(result.missing.before)/\(result.missing.after)")
                .fontWeight(.semibold)
            Text("\(result.items.count) свипов · \(result.points) точек в каждом · порядок started_at_then_id")
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(result.items, id: \.offset) { item in
                        VStack(alignment: .leading, spacing: 3) {
                            Button("\(item.offset == 0 ? "Опорный" : "Смещение") \(item.offset > 0 ? "+" : "")\(item.offset) · \(item.id)") { model.selectRow(offset: item.offset) }
                                .fontWeight(model.selectedOffset == item.offset ? .bold : .regular)
                            Text("ID: \(item.id)").fixedSize(horizontal: false, vertical: true)
                            Text("UTC: \(item.interval.startedAt) — \(item.interval.endedAt)").fixedSize(horizontal: false, vertical: true)
                            DisclosureGroup("Полные настройки, включая sequence") { Text(contextSettingsText(item.settings)) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }.frame(maxHeight: 340)
            if let item = model.selectedItem {
                Text("Выбранный исходный спектр: \(item.id) · смещение \(item.offset)")
                SweepContextChart(frequenciesHz: result.frequenciesHz, levels: item.levels)
                DisclosureGroup("Все \(result.points) исходные значения dBm") {
                    ScrollView {
                        LazyVStack(alignment: .leading) {
                            ForEach(item.levels.indices, id: \.self) { index in
                                Text("[\(index)] \(result.frequenciesHz[index]) Гц: \(String(item.levels[index])) dBm")
                            }
                        }
                    }.frame(height: 150)
                }
            }
        }
    }
}

private func contextSettingsText(_ settings: [String: PlatformComparisonSetting]) -> String {
    func text(_ value: PlatformComparisonSetting) -> String {
        switch value {
        case .string(let value): return "\"\(value)\""
        case .number(let value): return String(value)
        case .boolean(let value): return value ? "true" : "false"
        case .null: return "null"
        case .array(let values): return "[" + values.map(text).joined(separator: ", ") + "]"
        case .object(let values): return "{" + contextSettingsText(values) + "}"
        }
    }
    return settings.keys.sorted { $0.utf8.lexicographicallyPrecedes($1.utf8) }.map { "\($0): \(text(settings[$0]!))" }.joined(separator: ", ")
}

struct SweepContextChart: View {
    let frequenciesHz: [Int64]
    let levels: [Double]
    static func points(frequenciesHz: [Int64], levels: [Double]) -> [PlotPoint] {
        zip(frequenciesHz, levels).map { PlotPoint(x: Double($0), y: $1) }
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Исходные dBm: \(String(levels.min()!))…\(String(levels.max()!))")
            Canvas { context, size in
                let scale = TraceChartVerticalScale(minimum: levels.min()!, maximum: levels.max()!)
                func point(_ index: Int) -> CGPoint {
                    CGPoint(x: ThreeSweepThresholdsChart.x(frequenciesHz[index], first: frequenciesHz.first!, last: frequenciesHz.last!, width: size.width),
                            y: 12 + (1 - scale.fraction(levels[index])) * max(size.height - 24, 1))
                }
                var path = Path(); path.move(to: point(0))
                for index in levels.indices.dropFirst() { path.addLine(to: point(index)) }
                context.stroke(path, with: .color(.blue), lineWidth: 1.3)
                // Render every raw bin, including a single-bin or repeated-frequency grid.
                for index in levels.indices {
                    let center = point(index)
                    context.fill(Path(ellipseIn: CGRect(x: center.x - 2, y: center.y - 2, width: 4, height: 4)), with: .color(.blue))
                }
            }.frame(height: 220).background(Color.primary.opacity(0.04))
            Text("Частота, Гц: \(frequenciesHz.first!)…\(frequenciesHz.last!) · все \(levels.count) точек, включая повторяющиеся частоты")
        }
    }
}
