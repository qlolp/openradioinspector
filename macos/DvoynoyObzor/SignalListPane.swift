import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class SignalListModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformPhonogram.sameID(a.server, b.server)
                && PlatformPhonogram.sameID(a.session, b.session)
                && PlatformPhonogram.sameID(a.stream, b.stream)
        }
    }
    @Published var draft = "" { didSet { if !filling && oldValue != draft { clearResult() } } }
    @Published var exportName = "Список сигналов"
    @Published private(set) var lastExport: PlatformSignalListExport?
    @Published private(set) var lastImport: PlatformSignalListImport?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var filling = false
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let export: (PlatformClient, String, String) async throws -> PlatformSignalListExport
    private let importList: (PlatformClient, String, String, String) async throws -> PlatformSignalListImport

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         export: @escaping (PlatformClient, String, String) async throws -> PlatformSignalListExport = {
             try await $0.signalListExport(sessionID: $1, name: $2)
         },
         importList: @escaping (PlatformClient, String, String, String) async throws -> PlatformSignalListImport = {
             try await $0.signalListImport(sessionID: $1, streamID: $2, listJSON: $3)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.export = export; self.importList = importList
    }
    var canExchange: Bool { scope != nil && client != nil }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client { clearResult() }
        self.client = client; self.scope = scope
    }
    func reset() { configure(client: nil, scope: nil) }
    func clearResult() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; errorText = nil
        // Editing the draft invalidates any frozen exchange result, as elsewhere.
        lastExport = nil; lastImport = nil
    }
    private func guardTimeout(current: Int) {
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.revision == current, self.pending else { return }
            self.clearResult(); self.errorText = "Время ожидания обмена истекло."
        }
    }
    func exportList() {
        guard !pending, let client, let scope else { return }
        clearResult()
        let current = revision
        let name = exportName
        pending = true
        request = Task { [weak self, export] in
            do {
                let result = try await export(client, scope.session, name)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                guard PlatformPhonogram.sameID(result.sessionId, scope.session) else {
                    throw PlatformError.invalidResponse("Сервер вернул другой экспорт")
                }
                self.lastExport = result
                // Filling the draft from the export must not invalidate the result.
                self.filling = true
                self.draft = String(data: (try? JSONEncoder().encode(result.list)) ?? Data(), encoding: .utf8) ?? self.draft
                self.filling = false
                self.infoText = "Экспортировано сигналов: \(result.exported)."
                self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.errorText = describeSession(error)
                self.finish()
            }
        }
        guardTimeout(current: current)
    }
    func importListNow() {
        guard !pending, let client, let scope else { return }
        clearResult()
        let current = revision
        let raw = draft
        pending = true
        request = Task { [weak self, importList] in
            do {
                let result = try await importList(client, scope.session, scope.stream, raw)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                guard PlatformPhonogram.sameID(result.sessionId, scope.session),
                      PlatformPhonogram.sameID(result.streamId, scope.stream) else {
                    throw PlatformError.invalidResponse("Сервер вернул другой импорт")
                }
                self.lastImport = result
                self.infoText = "Импортировано сигналов: \(result.imported) («\(result.name)»)."
                self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.errorText = describeSession(error)
                self.finish()
            }
        }
        guardTimeout(current: current)
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteSignalListPane: View {
    @ObservedObject var model: SignalListModel
    var body: some View {
        GroupBox("Список сигналов · выгрузка и загрузка") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Экспорт сериализует архивных кандидатов сеанса; импорт создаёт новых кандидатов через обычный жизненный цикл (состояние new). Единицы измерения не конвертируются: dBm и s_meter_count никогда не пересчитываются друг в друга.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    TextField("Имя списка", text: $model.exportName).textFieldStyle(.roundedBorder)
                    Button("Выгрузить список") { model.exportList() }
                        .disabled(!model.canExchange || model.pending)
                    Button("Загрузить список") { model.importListNow() }
                        .disabled(!model.canExchange || model.pending || model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                TextEditor(text: $model.draft)
                    .font(.system(.caption, design: .monospaced))
                    .frame(minHeight: 110, maxHeight: 160)
                    .border(Color.secondary.opacity(0.4))
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let exported = model.lastExport {
                    Text("Выгружено сигналов: \(exported.exported) («\(exported.list.name)»). Исходные записи не изменялись.")
                        .font(.caption)
                }
                if let imported = model.lastImport {
                    Text("Импортировано сигналов: \(imported.imported) в поток \(imported.streamId).")
                        .font(.caption)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clearResult() }
    }
}
