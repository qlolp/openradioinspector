import Foundation
import SwiftUI
import AppKit
import UniformTypeIdentifiers
import SpektrKomandy

enum FrequencyRoundingFiles {
    static func readDraft(from url: URL) throws -> String {
        guard url.isFileURL, try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw PlatformError.invalidCommand("Выберите обычный локальный UTF-8 JSON файл")
        }
        let file = try FileHandle(forReadingFrom: url); defer { try? file.close() }
        let data = try file.read(upToCount: PlatformFrequencyRoundingProfile.maximumBytes + 1) ?? Data()
        guard data.count <= PlatformFrequencyRoundingProfile.maximumBytes, let text = String(data: data, encoding: .utf8) else {
            throw PlatformError.invalidCommand("Профиль должен быть UTF-8 JSON не более 65 536 байт")
        }
        return text
    }
    @discardableResult static func save(_ profile: PlatformFrequencyRoundingProfile, to destination: URL?) throws -> Bool {
        guard let destination else { return false }
        guard destination.isFileURL else { throw PlatformError.invalidCommand("Нужен локальный файл назначения") }
        try profile.canonicalData().write(to: destination, options: .atomic)
        return true
    }
}

@MainActor
final class FrequencyRoundingModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformThreeSweepThresholds.sameID(a.server, b.server) && PlatformThreeSweepThresholds.sameID(a.session, b.session)
        }
    }
    struct FileToken: Equatable { let revision: Int; let operation: Int }
    static let example = """
    {
      "format": "openradioinspector-frequency-rounding",
      "format_version": 1,
      "name": "НЕ НОРМАТИВ: пример округления",
      "frequency_unit": "Hz",
      "method": "nearest_grid",
      "tie_break": "lower",
      "rules": [
        {"start_frequency_hz":100000000,"end_frequency_hz":110000000,"step_hz":100000,"origin_frequency_hz":0,"max_offset_hz":30000},
        {"start_frequency_hz":140000000,"end_frequency_hz":150000000,"step_hz":12500,"origin_frequency_hz":0,"max_offset_hz":6250}
      ]
    }
    """
    @Published var draft = FrequencyRoundingModel.example {
        didSet { if !PlatformThreeSweepThresholds.sameID(draft, oldValue) { cancel() } }
    }
    @Published private(set) var choices: [PlatformCandidate] = []
    @Published private(set) var source: PlatformCandidate?
    @Published private(set) var frozenSource: PlatformCandidate?
    @Published private(set) var frozenScope: Scope?
    @Published private(set) var result: PlatformCandidateFrequencyRounding?
    @Published private(set) var pending = false
    @Published private(set) var ready = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private var fileOperation = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, String) async throws -> PlatformCandidateFrequencyRounding

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, String) async throws -> PlatformCandidateFrequencyRounding = {
             try await $0.candidateFrequencyRounding(sessionID: $1, candidateID: $2, profileJSON: $3)
         }) { self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch }
    var canAnalyze: Bool { ready && scope != nil && client != nil && source != nil && !draft.isEmpty }
    var menuChoices: [PlatformCandidate] {
        guard let source, !choices.contains(where: { PlatformThreeSweepThresholds.sameID($0.id, source.id) }) else { return choices }
        return [source] + choices
    }
    func configure(client: PlatformClient?, scope: Scope?, candidates: [PlatformCandidate], ready: Bool = true) {
        if self.scope != scope || self.client !== client { cancel(); source = nil; choices = [] }
        self.client = client; self.scope = scope
        self.ready = ready && client != nil && scope != nil
        guard self.ready else { cancel(); choices = []; return }
        var seen = Set<Data>()
        choices = candidates.filter {
            PlatformThreeSweepThresholds.sameID($0.sessionId, scope?.session)
                && PlatformCandidateFrequencyRounding.validSource($0) && seen.insert(Data($0.id.utf8)).inserted
        }
        // Keep the entire chosen source, not an ID lookup into a moving polling page.
        if source == nil { source = choices.first }
    }
    func selectSource(id: String) {
        guard let chosen = menuChoices.first(where: { PlatformThreeSweepThresholds.sameID($0.id, id) }) else { return }
        cancel(); source = chosen
    }
    func reset() { configure(client: nil, scope: nil, candidates: [], ready: false) }
    func unavailable() { cancel(); ready = false }
    func cancel() {
        revision += 1; fileOperation += 1; request?.cancel(); timeout?.cancel()
        request = nil; timeout = nil; pending = false; result = nil; errorText = nil; frozenSource = nil; frozenScope = nil
    }
    func makeTemplate() { cancel(); draft = Self.example }
    func beginFileOperation() -> FileToken { fileOperation += 1; return FileToken(revision: revision, operation: fileOperation) }
    private func current(_ token: FileToken) -> Bool { token.revision == revision && token.operation == fileOperation }
    func importDraft(from url: URL?, token: FileToken) {
        guard let url, current(token) else { return }
        do {
            let text = try FrequencyRoundingFiles.readDraft(from: url)
            guard current(token) else { return }
            cancel(); draft = text
        } catch { if current(token) { errorText = describeSession(error) } }
    }
    @discardableResult func exportValidated(to url: URL?, expected: PlatformCandidateFrequencyRounding, token: FileToken) -> Bool {
        guard let url, current(token), !pending, ready, result == expected, let source = frozenSource,
              frozenScope == scope else { return false }
        do {
            try expected.validate(source: source, profileJSON: draft)
            return try FrequencyRoundingFiles.save(expected.profile, to: url)
        } catch { if current(token) { errorText = describeSession(error) }; return false }
    }
    func analyze() {
        guard !pending, canAnalyze, let client, let scope, let source else { return }
        cancel()
        guard draft.utf8.count <= PlatformFrequencyRoundingProfile.maximumBytes else {
            errorText = "Введите профиль JSON до 65 536 байт UTF-8. Содержимое проверяет ядро."; return
        }
        let token = revision, submitted = draft
        pending = true; frozenSource = source; frozenScope = scope
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, source.id, submitted)
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                try value.validate(source: source, profileJSON: submitted)
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, token) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Сеанс/кандидат не найден или сервер не поддерживает округление (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Источник несовместим с округлением (409): \(message)"
                case PlatformError.http(status: 413, code: _, message: _):
                    self.errorText = "Запрос превышает предел HTTP-тела ядра (413)."
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, token), self.pending else { return }
            self.cancel(); self.errorText = "Время ожидания округления истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ token: Int) -> Bool {
        self.scope == scope && self.client === client && revision == token && ready
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

private struct RoundingMenuChoice: Identifiable {
    let candidate: PlatformCandidate
    var id: Data { Data(candidate.id.utf8) }
}

struct RemoteFrequencyRoundingPane: View {
    @ObservedObject var model: FrequencyRoundingModel
    var body: some View {
        GroupBox("Округление частоты · переносимый JSON") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Отдельный источник", selection: Binding(
                    get: { model.source.map { Data($0.id.utf8) } ?? Data() },
                    set: { model.selectSource(id: String(decoding: $0, as: UTF8.self)) }
                )) {
                    Text("Не выбран").tag(Data())
                    ForEach(model.menuChoices.map { RoundingMenuChoice(candidate: $0) }) { choice in
                        let candidate = choice.candidate
                        Text("\(candidate.id) · \(candidate.centerFrequencyHz) Гц · \(candidate.sourceStreamId)").tag(Data(candidate.id.utf8))
                    }
                }.disabled(!model.ready)
                if let source = model.source {
                    Text("Исходная: \(source.centerFrequencyHz) Гц · единицы уровня: \(source.levelUnit)")
                    Text("UTC \(source.interval.startedAt) — \(source.interval.endedAt)")
                }
                Text(model.result == nil ? "Черновик НЕ ПРОВЕРЕН ядром" : "Профиль проверен для зафиксированного источника").bold()
                TextEditor(text: $model.draft).font(.system(.caption, design: .monospaced))
                    .frame(minHeight: 150, maxHeight: 220).border(Color.secondary.opacity(0.4))
                HStack {
                    Button("Импорт JSON…") { importFile() }
                    Button("Пример") { model.makeTemplate() }
                    Text("\(model.draft.utf8.count) / 65 536 байт")
                }
                Text("НЕ НОРМАТИВ: пример сетки не является разрешением на частоты или калибровкой.").foregroundStyle(.secondary)
                HStack {
                    Button("Рассчитать") { model.analyze() }.disabled(!model.canAnalyze || model.pending)
                    Button("Очистить") { model.cancel() }
                    if model.pending { ProgressView().controlSize(.small) }
                    Button("Экспорт профиля…") { exportFile() }.disabled(model.result == nil || model.pending || !model.ready)
                }
                ErrorLine(text: model.errorText)
                if let value = model.result { resultView(value) }
                Text("Только расчёт ядра по кнопке. Все кандидаты сеанса доступны независимо от спектрального потока. Исходный кандидат, уровни, handoff и приборы не изменяются.")
                    .foregroundStyle(.secondary)
            }.font(.caption).textSelection(.enabled).padding(4)
        }
    }
    private func importFile() {
        let token = model.beginFileOperation()
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]; panel.allowsMultipleSelection = false; panel.canChooseDirectories = false
        if panel.runModal() == .OK { model.importDraft(from: panel.url, token: token) }
    }
    private func exportFile() {
        guard let expected = model.result else { return }
        let token = model.beginFileOperation()
        let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "frequency-rounding-profile.json"
        if panel.runModal() == .OK { model.exportValidated(to: panel.url, expected: expected, token: token) }
    }
    private func resultView(_ value: PlatformCandidateFrequencyRounding) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text("Статус: \(value.result.status)").bold()
            Text("Исходная: \(value.candidate.centerFrequencyHz) Гц")
            Text("Ближайшая точка: \(value.result.nearestFrequencyHz.map(String.init) ?? "—") Гц")
            Text("Отклонение: \(value.result.offsetHz.map { ($0 >= 0 ? "+" : "") + String($0) } ?? "—") Гц")
            Text("Допуск: \(value.result.ruleIndex.map { String(value.profile.rules[$0].maxOffsetHz) } ?? "—") Гц")
            Text("Производная принятая частота: \(value.result.roundedFrequencyHz.map(String.init) ?? "—") Гц")
            Text("Источник: \(value.candidate.id) · \(value.candidate.sourceStreamId) · \(value.candidate.levelUnit)")
            Text("Сеанс: \(value.sessionId) · сервер: \(model.frozenScope?.server ?? "—")")
            Text("UTC \(value.candidate.interval.startedAt) — \(value.candidate.interval.endedAt)")
            Text("Профиль: \(value.profile.name) · nearest_grid · tie lower · Hz")
        }
    }
}
