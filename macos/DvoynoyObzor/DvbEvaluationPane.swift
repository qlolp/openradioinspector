import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class DvbEvaluationModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformDvbEvaluation.sameID(a.server, b.server)
                && PlatformDvbEvaluation.sameID(a.session, b.session)
        }
    }
    struct Draft: Equatable {
        var merText = "20"
        var berViterbiText = "0.000001"
        var berRsText = "0"
        var minMerText = "15"
        var maxViterbiText = "0.001"
        var maxRsText = "1e-9"
        var guardInterval = "1/4"
        var codeRate = "2/3"
    }
    @Published var draft = Draft() { didSet { if oldValue != draft { clear() } } }
    @Published private(set) var result: PlatformDvbEvaluation?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let evaluate: (PlatformClient, String, PlatformDvbEvaluationRequest) async throws -> PlatformDvbEvaluation

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         evaluate: @escaping (PlatformClient, String, PlatformDvbEvaluationRequest) async throws -> PlatformDvbEvaluation = {
             try await $0.dvbEvaluation(sessionID: $1, $2)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.evaluate = evaluate
    }
    var canEvaluate: Bool { scope != nil && client != nil }
    private func number(_ text: String) -> Double? {
        let raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty, let value = Double(raw), value.isFinite else { return nil }
        return value
    }
    var draftRequest: PlatformDvbEvaluationRequest? {
        guard let mer = number(draft.merText), let berViterbi = number(draft.berViterbiText),
              let berRs = number(draft.berRsText), let minMer = number(draft.minMerText),
              let maxViterbi = number(draft.maxViterbiText), let maxRs = number(draft.maxRsText),
              PlatformDvbEvaluation.guardIntervals.contains(draft.guardInterval),
              PlatformDvbEvaluation.codeRates.contains(draft.codeRate) else { return nil }
        return PlatformDvbEvaluationRequest(
            parameters: ["mer_db": mer, "ber_after_viterbi": berViterbi, "ber_after_rs": berRs],
            thresholds: ["min_mer_db": minMer, "max_ber_after_viterbi": maxViterbi, "max_ber_after_rs": maxRs],
            guardInterval: draft.guardInterval, codeRate: draft.codeRate)
    }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client { clear() }
        self.client = client; self.scope = scope
    }
    func reset() { configure(client: nil, scope: nil) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil; infoText = nil
    }
    func analyze() {
        guard !pending, let client, let scope else { return }
        clear()
        guard let body = draftRequest else {
            errorText = "Все поля — конечные числа; интервал и скорость — из списков."; return
        }
        let current = revision
        pending = true
        request = Task { [weak self, evaluate] in
            do {
                let value = try await evaluate(client, scope.session, body)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.result = value
                self.infoText = "Код результата: \(value.resultCode)."
                self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.errorText = describeSession(error)
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.revision == current, self.pending else { return }
            self.clear(); self.errorText = "Время ожидания оценки истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteDvbEvaluationPane: View {
    @ObservedObject var model: DvbEvaluationModel
    var body: some View {
        GroupBox("Параметры DVB-T · сверка с пороговыми значениями") {
            VStack(alignment: .leading, spacing: 8) {
                Text("MER, BER после Витерби и после Рида — Соломона сверяются с пороговыми значениями; равенство порогу — норма. Это оценка введённых чисел, а не измерение: демодуляция IQ и измерение MER/BER прибором не выполняются.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    TextField("MER, дБ", text: $model.draft.merText).textFieldStyle(.roundedBorder)
                    TextField("BER после Витерби", text: $model.draft.berViterbiText).textFieldStyle(.roundedBorder)
                    TextField("BER после РС", text: $model.draft.berRsText).textFieldStyle(.roundedBorder)
                }
                HStack {
                    TextField("Порог MER", text: $model.draft.minMerText).textFieldStyle(.roundedBorder)
                    TextField("Порог BER Витерби", text: $model.draft.maxViterbiText).textFieldStyle(.roundedBorder)
                    TextField("Порог BER РС", text: $model.draft.maxRsText).textFieldStyle(.roundedBorder)
                }
                HStack {
                    Picker("Защитный интервал", selection: $model.draft.guardInterval) {
                        ForEach(PlatformDvbEvaluation.guardIntervals, id: \.self) { value in
                            Text(value).tag(value)
                        }
                    }
                    Picker("Скорость кодирования", selection: $model.draft.codeRate) {
                        ForEach(PlatformDvbEvaluation.codeRates, id: \.self) { value in
                            Text(value).tag(value)
                        }
                    }
                }
                HStack {
                    Button("Сравнить с порогами") { model.analyze() }
                        .disabled(!model.canEvaluate || model.pending || model.draftRequest == nil)
                    Button("Очистить результат") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let result = model.result {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Код результата: \(result.resultCode)")
                            .fontWeight(.semibold)
                        Text("Защитный интервал \(result.guardInterval) · скорость кодирования \(result.codeRate)")
                        ForEach(PlatformDvbEvaluation.parameterFields, id: \.self) { name in
                            Text("\(name): \(String(describing: result.parameters[name]!)) — "
                                 + ((result.verdicts[name] ?? false) ? "в норме" : "вне нормы"))
                        }
                        Text("Значения готовы для записи в результаты измерений (методика dvb_t_parameters).")
                            .foregroundStyle(.secondary)
                    }.font(.caption)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
}
