import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class PhonogramModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformPhonogram.sameID(a.server, b.server)
                && PlatformPhonogram.sameID(a.session, b.session)
                && PlatformPhonogram.sameID(a.stream, b.stream)
        }
    }
    @Published private(set) var status: PlatformPhonogramStatus?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let call: (PlatformClient, String, String, String) async throws -> PlatformPhonogramStatus

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         call: @escaping (PlatformClient, String, String, String) async throws -> PlatformPhonogramStatus = { client, action, session, stream in
             switch action {
             case "start": return try await client.phonogramStart(sessionID: session, streamID: stream)
             case "stop": return try await client.phonogramStop(sessionID: session, streamID: stream)
             default: return try await client.phonogramStatus(sessionID: session, streamID: stream)
             }
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.call = call
    }
    var canCall: Bool { scope != nil && client != nil }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client { clear(); status = nil }
        self.client = client; self.scope = scope
    }
    func reset() { configure(client: nil, scope: nil) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; errorText = nil; infoText = nil
    }
    func perform(_ action: String) {
        guard !pending, let client, let scope else { return }
        clear()
        let current = revision
        pending = true
        request = Task { [weak self, call] in
            do {
                let value = try await call(client, action, scope.session, scope.stream)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                guard PlatformPhonogram.sameID(value.sessionId, scope.session),
                      PlatformPhonogram.sameID(value.streamId, scope.stream) else {
                    throw PlatformError.invalidResponse("Сервер вернул другое состояние фонограммы")
                }
                self.status = value
                self.infoText = "Состояние обновлено."
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
            self.clear(); self.errorText = "Время ожидания фонограммы истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemotePhonogramPane: View {
    @ObservedObject var model: PhonogramModel
    var body: some View {
        GroupBox("Фонограмма · запись с пятисекундной предысторией") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Кнопка «Начать запись» фиксирует окно последних 5 секунд буфера как предысторию — файл уже содержит звучавшее до нажатия. Отсчёты поступают из аудиопотока или через API; панель их не генерирует. Запись — runtime-состояние: в архив ничего не пишется.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Начать запись (предыстория 5 с)") { model.perform("start") }
                        .disabled(!model.canCall || model.pending)
                    Button("Остановить запись") { model.perform("stop") }
                        .disabled(!model.canCall || model.pending)
                    Button("Обновить состояние") { model.perform("status") }
                        .disabled(!model.canCall || model.pending)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let status = model.status {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Предыстория: \(String(status.preHistorySecondsField)) с · буфер: \(String(status.bufferedSeconds)) с · запись: \(status.recording ? "идёт" : "остановлена") · записано: \(String(status.recordedSeconds)) с")
                            .fontWeight(.semibold)
                        if let last = status.last {
                            Text("Последняя фонограмма: \(String(last.durationSeconds)) с = \(String(last.preHistorySeconds)) с предыстории + \(String(last.recordedSeconds)) с записи · \(String(last.sampleRateHz)) Гц")
                            Text("WAV: GET …/phonogram/wav?stream_id=\(status.streamId)")
                                .foregroundStyle(.secondary)
                        }
                    }.font(.caption)
                } else {
                    Text("Состояние запрашивается по кнопке.").foregroundStyle(.secondary)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
}
