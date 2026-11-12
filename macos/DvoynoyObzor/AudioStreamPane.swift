import Foundation
import SwiftUI
import SpektrKomandy

enum AudioStreamOutcome: Equatable {
    case status(PlatformAudioStreamStatus)
    case sample(PlatformAudioStreamSample)
}

@MainActor
final class AudioStreamModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformPhonogram.sameID(a.server, b.server)
                && PlatformPhonogram.sameID(a.session, b.session)
                && PlatformPhonogram.sameID(a.stream, b.stream)
        }
    }
    @Published private(set) var status: PlatformAudioStreamStatus?
    @Published private(set) var sample: PlatformAudioStreamSample?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let call: (PlatformClient, String, String, String) async throws -> AudioStreamOutcome

    init(timeoutNanoseconds: UInt64 = 8_000_000_000,
         call: @escaping (PlatformClient, String, String, String) async throws -> AudioStreamOutcome = { client, action, session, stream in
             switch action {
             case "sample": return .sample(try await client.audioStreamSample(sessionID: session, streamID: stream))
             default: return .status(try await client.audioStreamStatus(sessionID: session, streamID: stream))
             }
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.call = call
    }
    var canCall: Bool { scope != nil && client != nil }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client { clear(); status = nil; sample = nil }
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
                switch value {
                case .status(let status):
                    guard PlatformPhonogram.sameID(status.sessionId, scope.session),
                          PlatformPhonogram.sameID(status.streamId, scope.stream) else {
                        throw PlatformError.invalidResponse("Сервер вернул другое состояние трансляции")
                    }
                    self.status = status
                    self.infoText = "Статус обновлён."
                case .sample(let sample):
                    self.sample = sample
                    self.infoText = "Выборка получена: \(sample.pcm.count) байт PCM16LE."
                }
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
            self.clear(); self.errorText = "Время ожидания трансляции истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteAudioStreamPane: View {
    @ObservedObject var model: AudioStreamModel
    var body: some View {
        GroupBox("Аудио по сети · трансляция демодулированного сигнала") {
            VStack(alignment: .leading, spacing: 8) {
                Text("Сетевой режим RemoteMD: блоки PCM16LE поступают клиенту по мере того, как рекордер принимает отсчёты. Здесь — ограниченная выборка из открытой трансляции: подключение удерживается ответом сервера без Content-Length, после выборки клиент его отпускает. Панель не генерирует аудио и не пишет архив: поток — доставка, а не запись. Остановленный клиент теряет только собственную очередь (потери считаются).")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Пробная выборка потока") { model.perform("sample") }
                        .disabled(!model.canCall || model.pending)
                    Button("Обновить статус") { model.perform("status") }
                        .disabled(!model.canCall || model.pending)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let status = model.status {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Подписчиков: \(status.subscribers) из \(status.maxSubscribers) · в очередях \(status.queuedBlocks) блоков · потеряно \(status.droppedBlocks) блоков")
                            .fontWeight(.semibold)
                        Text("Трансляция — runtime-доставка: запись и архив не изменяются.")
                            .foregroundStyle(.secondary)
                    }.font(.caption)
                } else {
                    Text("Статус запрашивается по кнопке.").foregroundStyle(.secondary)
                }
                if let sample = sampleLine {
                    Text(sample).font(.caption)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
    private var sampleLine: String? {
        guard let sample = model.sample else { return nil }
        return "Выборка: \(sample.pcm.count) байт · \(sample.sampleRateHz) Гц · \(sample.sampleFormat). Непрерывная трансляция и прослушивание — отдельная работа."
    }
}
