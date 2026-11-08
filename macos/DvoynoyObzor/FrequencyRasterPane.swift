import Foundation
import SwiftUI
import AppKit
import SpektrKomandy

/// File boundaries are independent of AppKit panels, so cancellation and failure use temporary-file tests.
enum FrequencyRasterFiles {
    static func readDraft(from url: URL) throws -> String {
        guard url.isFileURL, try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw PlatformError.invalidCommand("Выберите обычный локальный UTF-8 JSON файл")
        }
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        let data = try file.read(upToCount: PlatformFrequencyRaster.maximumBytes + 1) ?? Data()
        guard data.count <= PlatformFrequencyRaster.maximumBytes else {
            throw PlatformError.invalidCommand("Файл таблицы превышает 65 536 байт UTF-8")
        }
        guard let text = String(data: data, encoding: .utf8) else {
            throw PlatformError.invalidCommand("Файл таблицы должен быть в UTF-8")
        }
        return text
    }

    @discardableResult static func save(_ table: PlatformFrequencyRaster, to destination: URL?) throws -> Bool {
        guard let destination else { return false } // User canceled: no filesystem writes.
        guard destination.isFileURL else { throw PlatformError.invalidCommand("Нужен локальный файл назначения") }
        let data = try table.canonicalData()
        try data.write(to: destination, options: .atomic)
        return true
    }
}

@MainActor
final class FrequencyRasterModel: ObservableObject {
    struct Scope: Equatable { let server: String; let session: String }
    @Published var draft = "" { didSet { if oldValue != draft { clear() } } }
    @Published var chosen: Set<String> = [] { didSet { if oldValue != chosen { clear() } } }
    @Published private(set) var candidates: [PlatformCandidate] = []
    @Published private(set) var result: PlatformFrequencyRounding?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, [String], String) async throws -> PlatformFrequencyRounding

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, [String], String) async throws -> PlatformFrequencyRounding = {
             try await $0.frequencyRounding(sessionID: $1, candidateIDs: $2, tableJSON: $3)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch
    }
    var canAnalyze: Bool { scope != nil && client != nil && !chosen.isEmpty && !draft.isEmpty }
    var canMakeTemplate: Bool {
        guard !chosenCandidates.isEmpty else { return false }
        let values = chosenCandidates.map(\.centerFrequencyHz)
        return values.min()! < values.max()! || values.count == 1
    }
    var chosenCandidates: [PlatformCandidate] { candidates.filter { chosen.contains($0.id) } }

    func configure(client: PlatformClient?, scope: Scope?, candidates: [PlatformCandidate]) {
        if self.scope != scope || self.client !== client { clear(); chosen = [] }
        self.client = client; self.scope = scope
        var seen = Set<Data>()
        self.candidates = candidates.filter {
            PlatformFrequencyRounding.sameID($0.sessionId, scope?.session) && seen.insert(Data($0.id.utf8)).inserted
        }
        chosen = chosen.intersection(Set(self.candidates.map(\.id)))
    }
    func toggle(_ id: String) {
        if chosen.contains(id) { chosen.remove(id) } else { chosen.insert(id) }
    }
    func reset() { configure(client: nil, scope: nil, candidates: []) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil
    }
    func makeTemplate() {
        let values = chosenCandidates.map(\.centerFrequencyHz)
        guard let minimum = values.min(), let maximum = values.max() else { return }
        let start = max(Int64(0), minimum - 1_000_000), stop = min(PlatformFrequencyRaster.maximumFrequencyHz, maximum + 1_000_000)
        guard stop - start >= 100_000 else {
            errorText = "Диапазон примера уже шага сетки 100 кГц."; return
        }
        let table = PlatformFrequencyRaster(name: "НЕНОРМАТИВНЫЙ пример 100 кГц", entries: [
            .init(name: "пример", startHz: start, stopHz: stop, gridStepHz: 100_000),
        ])
        guard let data = try? table.canonicalData(), let text = String(data: data, encoding: .utf8) else { return }
        draft = text
    }
    func analyze() {
        guard !pending, let client, let scope, !chosen.isEmpty else { return }
        cancel()
        guard !draft.isEmpty, draft.utf8.count <= PlatformFrequencyRaster.maximumBytes else {
            errorText = "Введите таблицу JSON до 65 536 байт UTF-8. Проверку содержимого выполняет ядро."
            return
        }
        let currentRevision = revision, submitted = draft
        let ids = candidates.map(\.id).filter { chosen.contains($0) }
        guard !ids.isEmpty else { errorText = "Выберите хотя бы одного кандидата."; return }
        pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, ids, submitted)
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                try value.validate(table: submitted, candidateIDs: ids)
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Сеанс или кандидат не найден; старый сервер может не поддерживать округление (404)."
                case PlatformError.http(status: 413, code: _, message: _):
                    self.errorText = "Запрос превышает предел HTTP-тела сервера (413)."
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, currentRevision), self.pending else { return }
            self.clear(); self.errorText = "Время ожидания округления истекло. Можно повторить запрос."
        }
    }
    func cancel() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil
    }
    func importDraft(from url: URL?) {
        guard let url else { return } // User canceled: the previous draft stays.
        do { draft = try FrequencyRasterFiles.readDraft(from: url) }
        catch { errorText = "Ошибка файла таблицы: \(describeSession(error))" }
    }
    func exportValidated(to url: URL?, expected: PlatformFrequencyRounding) {
        do {
            if try FrequencyRasterFiles.save(expected.table, to: url) {
                errorText = nil
            }
        } catch { errorText = "Ошибка файла таблицы: \(describeSession(error))" }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ revision: Int) -> Bool {
        self.scope == scope && self.client === client && self.revision == revision
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteFrequencyRasterPane: View {
    @ObservedObject var model: FrequencyRasterModel
    var body: some View {
        GroupBox("Округление частот обнаруженных сигналов · таблица сетки") {
            VStack(alignment: .leading, spacing: 8) {
                Text(model.candidates.isEmpty ? "Нет кандидатов выбранного сеанса" : "Кандидатов сеанса: \(model.candidates.count). Исходные частоты берутся из архива.")
                    .foregroundStyle(model.candidates.isEmpty ? .secondary : .primary)
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(model.candidates) { candidate in
                            Button {
                                model.toggle(candidate.id)
                            } label: {
                                HStack {
                                    Image(systemName: model.chosen.contains(candidate.id) ? "checkmark.square" : "square")
                                    Text("\(candidate.id) · \(String(candidate.centerFrequencyHz)) Гц")
                                }
                            }.buttonStyle(.plain)
                        }
                    }
                }.frame(maxHeight: 110)
                Text(model.result == nil ? "Черновик НЕ ПРОВЕРЕН ядром" : "Таблица проверена для показанного результата")
                    .font(.caption.bold())
                TextEditor(text: $model.draft).font(.system(.caption, design: .monospaced)).frame(minHeight: 130, maxHeight: 200)
                    .border(Color.secondary.opacity(0.4))
                HStack {
                    Button("Импорт JSON…") { importFile() }
                    Button("НЕ НОРМАТИВ: пример 100 кГц") { model.makeTemplate() }.disabled(!model.canMakeTemplate)
                    Text("\(model.draft.utf8.count) / 65 536 байт UTF-8").font(.caption)
                }
                HStack {
                    Button("Округлить выбранных кандидатов") { model.analyze() }.disabled(!model.canAnalyze || model.pending)
                    Button(model.pending ? "Отмена" : "Очистить результат") { model.cancel() }
                    Button("Экспорт проверенной таблицы…") { exportFile() }.disabled(model.result == nil || model.pending)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                Text("Импорт и редактирование локальны. Только кнопка округления передаёт текст таблицы подключённому ядру. POST выполняет только чтение и расчёт: кандидаты в архиве не изменяются. Сетка записи — точки start_hz + k·grid_step_hz; при равном расстоянии берётся более высокая точка. Частоты вне всех записей не округляются. Это не калибровка и не сверка с нормативной сеткой каналов.")
                    .font(.caption).foregroundStyle(.secondary)
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
    private func importFile() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]; panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        if panel.runModal() == .OK { model.importDraft(from: panel.url) }
    }
    private func exportFile() {
        guard let expected = model.result else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "frequency-raster.json"
        if panel.runModal() == .OK { model.exportValidated(to: panel.url, expected: expected) }
    }
    private func resultView(_ result: PlatformFrequencyRounding) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Зафиксировано: таблица «\(result.table.name)» · сеанс \(result.sessionId) · \(result.count) кандидатов")
            Text("Совпало с диапазонами: \(result.matchedCount) · вне таблицы: \(result.unmatchedCount). Исходные частоты архива не изменялись.")
                .fontWeight(.semibold)
            ForEach(result.items.indices, id: \.self) { index in
                let item = result.items[index]
                if item.matched, let rounded = item.roundedFrequencyHz, let delta = item.deltaHz,
                   let step = item.gridStepHz, let entryName = item.entryName {
                    Text("\(item.candidateId): \(String(item.frequencyHz)) Гц → \(String(rounded)) Гц (Δ \(delta >= 0 ? "+" : "")\(String(delta)) Гц, \(entryName), шаг \(String(step)) Гц)")
                } else {
                    Text("\(item.candidateId): \(String(item.frequencyHz)) Гц — вне диапазонов таблицы, не округлялась")
                }
            }
            DisclosureGroup("Записи проверенной таблицы") {
                ForEach(result.table.entries.indices, id: \.self) { index in
                    let entry = result.table.entries[index]
                    Text("\(entry.name): \(String(entry.startHz))…\(String(entry.stopHz)) Гц, шаг \(String(entry.gridStepHz)) Гц")
                }
            }
        }.font(.caption)
    }
}
