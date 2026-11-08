import Foundation
import CoreFoundation
import SwiftUI
import SpektrKomandy

@MainActor
final class MeasurementResultsModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformMeasurementResult.sameID(a.server, b.server)
                && PlatformMeasurementResult.sameID(a.session, b.session)
                && PlatformMeasurementResult.sameID(a.stream, b.stream)
        }
    }
    struct Draft: Equatable {
        var methodology = ""
        var resultCode = "norm"
        var frequencyText = "101800000"
        var startedText = "2026-11-08T12:00:00Z"
        var endedText = "2026-11-08T12:00:01Z"
        var values = "{}"
    }
    @Published var draft = Draft() { didSet { if oldValue != draft { clear() } } }
    @Published private(set) var catalog: [PlatformMethodologyEntry] = []
    @Published private(set) var items: [PlatformMeasurementResult] = []
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let save: (PlatformClient, String, PlatformMeasurementResultDraft) async throws -> PlatformMeasurementResult
    private let list: (PlatformClient, String) async throws -> [PlatformMeasurementResult]
    private let loadCatalog: (PlatformClient) async throws -> [PlatformMethodologyEntry]

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         save: @escaping (PlatformClient, String, PlatformMeasurementResultDraft) async throws -> PlatformMeasurementResult = {
             try await $0.saveMeasurementResult(sessionID: $1, $2)
         },
         list: @escaping (PlatformClient, String) async throws -> [PlatformMeasurementResult] = {
             try await $0.listMeasurementResults(sessionID: $1)
         },
         loadCatalog: @escaping (PlatformClient) async throws -> [PlatformMethodologyEntry] = {
             try await $0.measurementCatalog()
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.save = save; self.list = list; self.loadCatalog = loadCatalog
    }
    var entry: PlatformMethodologyEntry? {
        catalog.first(where: { $0.methodology == draft.methodology })
    }
    var canSave: Bool { scope != nil && client != nil && entry != nil }
    var chosenValues: [String: Double]? {
        guard let entry else { return nil }
        if draft.resultCode == "not_measured" {
            // The code alone decides "no values": a non-empty draft is refused.
            let source = draft.values.trimmingCharacters(in: .whitespacesAndNewlines)
            guard source.isEmpty || source == "{}" else { return nil }
            return [:]
        }
        guard let data = draft.values.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let names = Set(entry.fields)
        guard Set(object.keys) == names else { return nil }
        var result: [String: Double] = [:]
        for name in entry.fields {
            guard let value = object[name] as? NSNumber,
                  CFGetTypeID(value) != CFBooleanGetTypeID() else { return nil }
            let number: Double? = value.doubleValue
            guard let number, number.isFinite else { return nil }
            result[name] = number
        }
        return result
    }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client {
            clear(); items = []; catalog = []
        }
        self.client = client; self.scope = scope
        if client != nil, scope != nil, catalog.isEmpty {
            let current = revision
            request = Task { [weak self, loadCatalog] in
                do {
                    let entries = try await loadCatalog(client!)
                    guard let self, !Task.isCancelled, self.revision == current else { return }
                    self.catalog = entries
                    if self.draft.methodology.isEmpty, let first = entries.first {
                        self.draft.methodology = first.methodology
                    }
                    self.refresh()
                } catch {
                    guard let self, !Task.isCancelled, self.revision == current else { return }
                    self.errorText = describeSession(error)
                }
            }
        }
    }
    func reset() { configure(client: nil, scope: nil) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; errorText = nil
    }
    func refresh() {
        guard let client, let scope else { return }
        let current = revision
        request = Task { [weak self, list] in
            do {
                let records = try await list(client, scope.session)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.items = records
            } catch {
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.errorText = describeSession(error)
            }
        }
    }
    func analyze() {
        guard !pending, let client, let scope, let entry, let values = chosenValues else {
            if chosenValues == nil {
                errorText = "Значения должны быть JSON-объектом с точными полями методики; для not_measured — {}."
            }
            return
        }
        clear()
        guard let frequency = Int64(draft.frequencyText.trimmingCharacters(in: .whitespacesAndNewlines)),
              frequency >= 0, frequency <= PlatformMeasurementResult.maximumFrequencyHz else {
            errorText = "Частота: целое неотрицательное число Гц."; return
        }
        let started = draft.startedText.trimmingCharacters(in: .whitespacesAndNewlines)
        let ended = draft.endedText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard validUTC(started), validUTC(ended) else {
            errorText = "Интервал: корректные метки UTC."; return
        }
        let body = PlatformMeasurementResultDraft(
            methodology: entry.methodology, resultCode: draft.resultCode,
            frequencyHz: frequency,
            interval: PlatformInterval(startedAt: started, endedAt: ended),
            values: values, streamId: scope.stream)
        let current = revision
        pending = true
        request = Task { [weak self, save] in
            do {
                let record = try await save(client, scope.session, body)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                // The archived sources stay unchanged; only the echoed record is accepted.
                guard PlatformMeasurementResult.sameID(record.sessionId, scope.session),
                      PlatformMeasurementResult.sameID(record.streamId, scope.stream),
                      record.methodology == body.methodology,
                      record.resultCode == body.resultCode,
                      record.frequencyHz == body.frequencyHz,
                      record.interval == body.interval else {
                    throw PlatformError.invalidResponse("Сервер вернул другой результат измерения")
                }
                self.items = self.items.filter { $0.id != record.id } + [record]
                self.infoText = "Результат \(record.id) записан: \(record.methodology) / \(record.resultCode)."
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
            self.clear(); self.errorText = "Время ожидания записи результата истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

private func validUTC(_ value: String) -> Bool {
    !value.isEmpty && value.hasSuffix("Z") && ISO8601DateFormatter().date(from: value) != nil
}

struct RemoteMeasurementResultsPane: View {
    @ObservedObject var model: MeasurementResultsModel
    var body: some View {
        GroupBox("Результаты измерений с явной методикой") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Методика — поле результата; код результата (norm / not_norm / not_measured) хранится отдельно от числовых значений, как *_RESULT и *_RESULTCODE оригинала. Запись привязана к сеансу, потоку и интервалу UTC. Исходные измерения архива не изменяются.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Методика", selection: $model.draft.methodology) {
                    ForEach(model.catalog, id: \.methodology) { entry in
                        Text("\(entry.methodology) · \(entry.title)").tag(entry.methodology)
                    }
                }
                Picker("Код результата", selection: $model.draft.resultCode) {
                    ForEach(PlatformMeasurementResult.resultCodes, id: \.self) { code in
                        Text(code).tag(code)
                    }
                }
                TextField("Частота, Гц", text: $model.draft.frequencyText)
                    .textFieldStyle(.roundedBorder)
                HStack {
                    TextField("Начало UTC", text: $model.draft.startedText)
                        .textFieldStyle(.roundedBorder)
                    TextField("Окончание UTC", text: $model.draft.endedText)
                        .textFieldStyle(.roundedBorder)
                }
                TextEditor(text: $model.draft.values)
                    .font(.system(.caption, design: .monospaced))
                    .frame(minHeight: 60, maxHeight: 90)
                    .border(Color.secondary.opacity(0.4))
                Text("Значения: JSON с точными полями методики (\(model.entry?.fields.joined(separator: ", ") ?? "—")); для not_measured — {}.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Записать результат") { model.analyze() }
                        .disabled(!model.canSave || model.pending)
                    Button("Обновить список") { model.refresh() }.disabled(model.pending)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if model.items.isEmpty {
                    Text("Результатов измерений в сеансе нет.").foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 4) {
                            ForEach(model.items, id: \.id) { record in
                                Text(describe(record))
                            }
                        }
                    }.frame(maxHeight: 180)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
    private func describe(_ record: PlatformMeasurementResult) -> String {
        let values = record.values.keys.sorted()
            .map { "\($0)=\(String(describing: record.values[$0]!))" }
            .joined(separator: ", ")
        return "[\(record.methodology)] \(record.resultCode) · \(String(record.frequencyHz)) Гц · "
            + (values.isEmpty ? "без значений" : values)
    }
}
