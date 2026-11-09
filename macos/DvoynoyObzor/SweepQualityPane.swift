import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class SweepQualityModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformSweepQuality.sameID(a.server, b.server) && PlatformSweepQuality.sameID(a.session, b.session)
                && PlatformSweepQuality.sameID(a.stream, b.stream)
        }
    }
    @Published var edgeText = "20" { didSet { if oldValue != edgeText { clear() } } }
    @Published var highText = "0" { didSet { if oldValue != highText { clear() } } }
    @Published private(set) var choices: [PlatformSweep] = []
    @Published private(set) var source: PlatformSweep?
    @Published private(set) var result: PlatformSweepQuality?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var client: PlatformClient?
    private var scope: Scope?
    private var revision = 0
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, Double, Double) async throws -> PlatformSweepQuality
    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, Double, Double) async throws -> PlatformSweepQuality = {
             try await $0.sweepQuality(sessionID: $1, sweepID: $2, edgeMarginDb: $3, highLevelDbm: $4)
         }) { self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch }
    func configure(client: PlatformClient?, scope: Scope?, sweeps: [PlatformSweep]) {
        if self.client !== client || self.scope != scope { clear(); source = nil }
        self.client = client; self.scope = scope
        var seen = Set<Data>()
        choices = sweeps.filter {
            PlatformSweepQuality.sameID($0.sessionId, scope?.session) && PlatformSweepQuality.sameID($0.streamId, scope?.stream)
                && PlatformSweepQuality.validSource($0) && seen.insert(Data($0.id.utf8)).inserted
        }
        // Polling updates the menu only, never the frozen selection/result.
        if source == nil { source = choices.last }
    }
    func select(id: String) {
        guard let next = choices.first(where: { PlatformSweepQuality.sameID($0.id, id) }) else { return }
        clear(); source = next
    }
    func reset() { configure(client: nil, scope: nil, sweeps: []) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil
    }
    var canAnalyze: Bool { client != nil && scope != nil && source != nil && !pending }
    func analyze() {
        guard canAnalyze, let client, let scope, let source else { return }
        clear()
        guard let edge = Double(edgeText.trimmingCharacters(in: .whitespacesAndNewlines)),
              let high = Double(highText.trimmingCharacters(in: .whitespacesAndNewlines)),
              PlatformSweepQuality.Parameters.valid(edge: edge, high: high) else {
            errorText = "Введите конечные пороги: край >0…200 dB, уровень −200…100 dBm."; return
        }
        let token = revision; pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, source.id, edge, high)
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                try value.validate(source: source)
                guard value.parameters.edgeMarginDb == edge, value.parameters.highLevelDbm == high else {
                    throw PlatformError.invalidResponse("Сервер вернул другие пороги")
                }
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                self.errorText = describeSession(error); self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, token), self.pending else { return }
            self.clear(); self.errorText = "Время ожидания отчёта истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ token: Int) -> Bool {
        self.scope == scope && self.client === client && revision == token
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteSweepQualityPane: View {
    @ObservedObject var model: SweepQualityModel
    var body: some View {
        GroupBox("Качество архивного свипа · эвристический отчёт") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Архивный свип", selection: Binding(get: { model.source?.id ?? "" }, set: { model.select(id: $0) })) {
                    Text("Выберите свип").tag("")
                    ForEach(model.choices) { Text($0.id).tag($0.id) }
                }
                if let source = model.source {
                    Text("Источник: \(source.id) · \(source.streamId) · UTC \(source.interval.startedAt) — \(source.interval.endedAt)").font(.caption)
                }
                HStack {
                    TextField("Скачок края, dB", text: $model.edgeText)
                    TextField("Высокий уровень, dBm", text: $model.highText)
                    Button("Проверить") { model.analyze() }.disabled(!model.canAnalyze)
                    Button(model.pending ? "Отмена" : "Очистить") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                Text("Эвристика НЕ диагностирует перегрузку или помехи и НЕ отбрасывает отсчёты. Исходные данные не изменяются. Только чтение архива, без команд приборам. Флаги используют ≥ порога.")
                    .font(.caption).foregroundStyle(.secondary)
                if let value = model.result {
                    Text(value.assessment == "review" ? "Нужен просмотр" : "Флагов нет (не гарантия качества)")
                    Text("\(value.points) точек · пик \(value.peakFrequencyHz) Гц: \(value.peakLevelDbm) dBm · индекс \(value.peakIndex)")
                    Text("Пороги: край \(value.parameters.edgeMarginDb) dB · уровень \(value.parameters.highLevelDbm) dBm · отсчётов ≥ уровня: \(value.highLevelCount)")
                    ForEach(value.edgeAssessments.indices, id: \.self) { index in
                        let edge = value.edgeAssessments[index]
                        Text("\(edge.edge): индекс \(edge.index) · \(edge.frequencyHz) Гц · \(edge.levelDbm) dBm; сосед \(edge.neighborIndex): \(edge.neighborLevelDbm) dBm; Δ \(edge.deltaDb) dB · \(edge.flagged ? "флаг" : "нет флага")")
                    }
                    Text("Флаги: \(value.flags.joined(separator: ", ")) · \(value.method)").font(.caption)
                }
                ErrorLine(text: model.errorText)
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
}
