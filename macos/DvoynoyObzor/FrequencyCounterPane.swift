import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class FieldStrengthModel: ObservableObject {
    typealias Scope = FrequencyCounterModel.Scope
    @Published var sweepID = "" { didSet { if oldValue != sweepID { clear() } } }
    @Published var frequencyText = "" { didSet { if oldValue != frequencyText { clear() } } }
    @Published var calibrationText = "" { didSet { if oldValue != calibrationText { clear() } } }
    @Published private(set) var sweeps: [PlatformSweep] = []
    @Published private(set) var result: PlatformFieldStrengthResult?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let measure: (PlatformClient, String, String, PlatformFieldStrengthRequest) async throws -> PlatformFieldStrengthResult
    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         measure: @escaping (PlatformClient, String, String, PlatformFieldStrengthRequest) async throws -> PlatformFieldStrengthResult = {
        try await $0.fieldStrengthRMSV2(sessionID: $1, streamID: $2, request: $3)
    }) { self.measure = measure; self.timeoutNanoseconds = timeoutNanoseconds }
    func configure(client: PlatformClient?, scope: Scope?, sweeps: [PlatformSweep]) {
        if self.scope != scope || self.client !== client { clear(); sweepID = "" }
        self.scope = scope; self.client = client
        self.sweeps = sweeps.filter {
            PlatformFrequencyCounterResult.sameID($0.sessionId, scope?.session)
                && PlatformFrequencyCounterResult.sameID($0.streamId, scope?.stream)
        }
        if !self.sweeps.contains(where: { PlatformFrequencyCounterResult.sameID($0.id, sweepID) }) {
            sweepID = self.sweeps.last?.id ?? ""
        }
    }
    func reset() { configure(client: nil, scope: nil, sweeps: []) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        result = nil; pending = false; errorText = nil
    }
    func analyze() {
        guard let client, let scope, !pending else { return }
        clear()
        do {
            guard !sweepID.isEmpty, let frequency = Int64(frequencyText), frequency >= 0,
                  frequency <= 9_007_199_254_740_991 else {
                throw PlatformError.invalidCommand("Выберите исходный свип и частоту в целых Гц")
            }
            let sent = PlatformFieldStrengthRequest(sweepId: sweepID, frequencyHz: frequency,
                calibration: try PlatformFieldStrengthRequest.calibration(from: calibrationText))
            let current = revision; pending = true
            request = Task { [weak self, measure] in
                do {
                    let value = try await measure(client, scope.session, scope.stream, sent)
                    try value.validate(sessionID: scope.session, streamID: scope.stream, request: sent)
                    guard let self, !Task.isCancelled, self.revision == current else { return }
                    self.result = value; self.pending = false; self.request = nil
                    self.timeout?.cancel(); self.timeout = nil
                } catch {
                    guard let self, !Task.isCancelled, self.revision == current else { return }
                    self.errorText = error.localizedDescription; self.pending = false; self.request = nil
                    self.timeout?.cancel(); self.timeout = nil
                }
            }
            timeout = Task { [weak self, timeoutNanoseconds] in
                do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
                guard let self, self.pending, self.revision == current else { return }
                self.clear(); self.errorText = "Время ожидания оценки истекло"
            }
        } catch { errorText = error.localizedDescription }
    }
}

struct RemoteFieldStrengthPane: View {
    @ObservedObject var model: FieldStrengthModel
    var body: some View {
        GroupBox("Узкополосная RMS-эквивалентная оценка поля") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Нужен профиль калибровки тракта и совместимые исходные RMS/CW/RBW настройки. Без них расчёт недоступен. Старый field_strength — некалиброванная оценка уровня. Исходники и охрана не меняются.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Архивный свип", selection: $model.sweepID) {
                    Text("Выберите свип").tag("")
                    ForEach(model.sweeps) { Text($0.id).tag($0.id) }
                }
                TextField("Частота исходного бина, целые Гц", text: $model.frequencyText)
                Text("Профиль калибровки JSON · до 64 КиБ").font(.caption)
                TextEditor(text: $model.calibrationText).font(.system(.body, design: .monospaced)).frame(minHeight: 120)
                HStack {
                    Button("Оценить поле") { model.analyze() }.disabled(model.pending || model.sweeps.isEmpty)
                    Button("Очистить результат") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let result = model.result {
                    Text(result.summary).textSelection(.enabled)
                    Text("Свип \(result.source.id) · калибровка \(result.calibration.sha256)")
                        .font(.caption).textSelection(.enabled)
                }
                if let error = model.errorText { Text("Оценка недоступна: \(error)").foregroundStyle(.red) }
            }
        }
    }
}

@MainActor
final class FrequencyCounterModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformFrequencyCounterResult.sameID(a.server, b.server)
                && PlatformFrequencyCounterResult.sameID(a.session, b.session)
                && PlatformFrequencyCounterResult.sameID(a.stream, b.stream)
        }
    }
    struct Draft: Equatable {
        var sweepID = ""
        var resolutionText = "10"
        var startText = "100"
        var stopText = "400"
    }
    @Published var draft = Draft() { didSet { if oldValue != draft { clear() } } }
    @Published private(set) var sweeps: [PlatformSweep] = []
    @Published private(set) var result: PlatformFrequencyCounterResult?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let measure: (PlatformClient, String, PlatformFrequencyCounterRequest) async throws -> PlatformFrequencyCounterResult

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         measure: @escaping (PlatformClient, String, PlatformFrequencyCounterRequest) async throws -> PlatformFrequencyCounterResult = {
             try await $0.frequencyCounter(sessionID: $1, $2)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.measure = measure
    }
    var canMeasure: Bool { scope != nil && client != nil && !sweeps.isEmpty }
    func configure(client: PlatformClient?, scope: Scope?, sweeps: [PlatformSweep]) {
        if self.scope != scope || self.client !== client {
            clear(); self.sweeps = []; draft.sweepID = ""
        }
        self.client = client; self.scope = scope
        var seen = Set<Data>()
        self.sweeps = sweeps.filter {
            PlatformFrequencyCounterResult.sameID($0.sessionId, scope?.session)
                && PlatformFrequencyCounterResult.sameID($0.streamId, scope?.stream)
                && $0.levelUnit == "dBm" && seen.insert(Data($0.id.utf8)).inserted
        }
        if !self.sweeps.contains(where: { PlatformFrequencyCounterResult.sameID($0.id, draft.sweepID) }) {
            draft.sweepID = self.sweeps.last?.id ?? ""
        }
    }
    func reset() { configure(client: nil, scope: nil, sweeps: []) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil; infoText = nil
    }
    func analyze() {
        guard !pending, let client, let scope else { return }
        clear()
        guard !draft.sweepID.isEmpty else { errorText = "Выберите архивный dBm свип."; return }
        guard let resolution = Int64(draft.resolutionText.trimmingCharacters(in: .whitespacesAndNewlines)),
              resolution >= 1, resolution <= PlatformFrequencyCounterResult.maximumFrequencyHz else {
            errorText = "Разрешение: положительное целое число Гц."; return
        }
        guard let start = Int64(draft.startText.trimmingCharacters(in: .whitespacesAndNewlines)),
              let stop = Int64(draft.stopText.trimmingCharacters(in: .whitespacesAndNewlines)),
              (0...PlatformFrequencyCounterResult.maximumFrequencyHz).contains(start),
              (0...PlatformFrequencyCounterResult.maximumFrequencyHz).contains(stop),
              start < stop else {
            errorText = "Окно: целые Гц, start < stop."; return
        }
        let body = PlatformFrequencyCounterRequest(
            sweepId: draft.sweepID, resolutionHz: resolution, startHz: start, stopHz: stop)
        let current = revision
        pending = true
        request = Task { [weak self, measure] in
            do {
                let value = try await measure(client, scope.session, body)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.result = value
                self.infoText = "Измерено: \(value.measuredFrequencyHz) Гц ± \(value.uncertaintyHz) Гц."
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
            self.clear(); self.errorText = "Время ожидания измерения истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteFrequencyCounterPane: View {
    @ObservedObject var model: FrequencyCounterModel
    var body: some View {
        GroupBox("Встроенный частотомер · измерение частоты с погрешностью") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Измерение частоты пика архивного dBm свипа в окне. Параболическая интерполяция только при равномерной сетке и пике внутри окна; иначе — сам бин пика. Погрешность: граница max(разрешение, сетка)/2. Это производная оценка по записанным данным, не показание аппаратного счётчика.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Архивный dBm свип", selection: $model.draft.sweepID) {
                    ForEach(model.sweeps, id: \.id) { sweep in
                        Text(sweep.id).tag(sweep.id)
                    }
                }
                HStack {
                    TextField("Разрешение, Гц", text: $model.draft.resolutionText)
                        .textFieldStyle(.roundedBorder).frame(width: 110)
                    TextField("Начало окна, Гц", text: $model.draft.startText)
                        .textFieldStyle(.roundedBorder).frame(width: 110)
                    TextField("Конец окна, Гц", text: $model.draft.stopText)
                        .textFieldStyle(.roundedBorder).frame(width: 110)
                }
                HStack {
                    Button("Измерить частоту") { model.analyze() }
                        .disabled(!model.canMeasure || model.pending)
                    Button("Очистить результат") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let result = model.result {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(String(result.measuredFrequencyHz)) Гц ± \(String(result.uncertaintyHz)) Гц · метод \(result.method)")
                            .fontWeight(.semibold)
                        Text("Пик: \(String(result.peakFrequencyHz)) Гц (\(String(result.peakLevelDbm)) dBm), индекс \(result.peakIndex)")
                        Text("Сетка: \(String(result.rasterHz)) Гц · разрешение: \(String(result.resolutionHz)) Гц")
                        Text("Свип: \(result.sweepId) · окно: \(String(result.windowStartHz))…\(String(result.windowStopHz)) Гц")
                        Text("Значения готовы для записи в результаты измерений (методика frequency_counter).")
                            .foregroundStyle(.secondary)
                    }.font(.caption)
                } else {
                    Text("Измерение запускается только по кнопке.").foregroundStyle(.secondary)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
}
