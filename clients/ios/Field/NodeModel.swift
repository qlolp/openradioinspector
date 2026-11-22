import Foundation
import Observation
import SpektrKomandy

@MainActor @Observable
final class NodeModel {
    private let keychain = ReaderKeychain()
    private(set) var profile: ReaderProfile?
    private(set) var reader: PlatformFieldReader?
    private(set) var sessions: [PlatformSession] = []
    private(set) var journal: PlatformFieldJournal?
    private(set) var revision = 0
    private(set) var busy = false
    var running = false
    var status = ""
    var failure: String?

    func load() {
        do { profile = try keychain.load() }
        catch { failure = error.localizedDescription }
    }

    func pair(address: String, certificate: ReaderCertificate, code: String) async {
        busy = true
        failure = nil
        defer { busy = false }
        do {
            let origin = try PlatformFieldReader.origin(address).absoluteString
            let endpoint = try PlatformFieldReader(node: origin, certificateDER: certificate.der)
            let credential = try await endpoint.pair(code: code)
            let candidate = ReaderProfile(origin: origin, certificateDER: certificate.der, bearer: credential.dataRepresentation)
            let paired = try candidate.reader()
            do {
                try await paired.verify()
                try keychain.save(candidate)
            } catch {
                try? await paired.revoke()
                throw error
            }
            profile = candidate
            running = true
        } catch { failure = error.localizedDescription }
    }

    func forget(revoke: Bool) async {
        guard let profile else { return }
        running = false
        busy = true
        defer { busy = false }
        do {
            if revoke { try await profile.reader().revoke() }
            try keychain.clear()
            self.profile = nil
            reader = nil
            sessions = []
            journal = nil
            failure = nil
            status = ""
        } catch { failure = error.localizedDescription }
    }

    /// The view cancels this task when it leaves the foreground. Keep the last committed
    /// cursor through disconnections, including a trip to Files or to the offline tab.
    func follow() async {
        guard let profile else { return }
        var backoff = 1
        while !Task.isCancelled {
            do {
                status = "Подключение…"
                let node = try profile.reader()
                try await node.verify()
                let watermark = try await node.watermark()
                let inventory = try await node.sessions()
                try Task.checkCancellation()
                sessions = inventory
                if journal == nil || journal!.cursor > watermark {
                    journal = PlatformFieldJournal(watermark: watermark)
                }
                reader = node
                revision += 1
                failure = nil
                status = "Подключено · только чтение"
                backoff = 1
                while !Task.isCancelled {
                    let page = try await node.events(after: journal!.cursor)
                    try Task.checkCancellation()
                    if !page.isEmpty {
                        try journal!.append(page)
                        if page.contains(where: { $0.eventType.hasPrefix("session.") || $0.eventType.hasPrefix("stream.") }) {
                            let refreshed = try await node.sessions()
                            try Task.checkCancellation()
                            sessions = refreshed
                        }
                        revision += 1
                    }
                    // Drain missed pages before resuming the foreground update interval.
                    if page.isEmpty { try await Task.sleep(for: .seconds(2)) }
                }
            } catch {
                if Task.isCancelled { return }
                if case PlatformEventError.sequenceGap = error { journal = nil }
                reader = nil
                failure = error.localizedDescription
                if case let PlatformError.http(status: code, code: _, message: _) = error, code == 401 || code == 403 {
                    running = false
                    status = "Доступ отозван. Выполните сопряжение заново."
                    return
                }
                status = "Нет связи. Повтор через \(backoff) с."
                do { try await Task.sleep(for: .seconds(backoff)) } catch { return }
                backoff = min(backoff * 2, 30)
            }
        }
    }
}
