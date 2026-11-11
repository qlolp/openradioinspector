import Foundation
import Combine
import SpektrKomandy

enum NativeNodeAccess: Hashable, Sendable { case pairedTLS, legacyLoopback, confirmedLegacy }
struct NativeAuthorizedNode: Sendable {
    let client: PlatformClient
    let access: NativeNodeAccess
    let server: PlatformExactID
    let request: UUID
}

@MainActor
final class NativeNodeConnectionModel: ObservableObject {
    enum Phase: Equatable { case idle, checking, pairingRequired, pairing, legacyConfirmation, ready, revoking, forgetting, failed }
    @Published private(set) var phase = Phase.idle
    @Published private(set) var authorizedNode: NativeAuthorizedNode?
    @Published private(set) var message = ""
    @Published var pairingCode = ""
    @Published var label = "Этот Mac"
    private let store: any NativeCredentialStoring
    private let makeClient: (String, PlatformBearerCredential?) throws -> PlatformClient
    private var server: PlatformExactID?
    private var request = UUID()
    private var legacyClient: PlatformClient?
    private var activeCredential: PlatformBearerCredential?
    var isBusy: Bool { [.checking, .pairing, .revoking, .forgetting].contains(phase) }

    init(store: any NativeCredentialStoring = NativeKeychainCredentialStore(),
         makeClient: @escaping (String, PlatformBearerCredential?) throws -> PlatformClient = {
             try PlatformClient(baseURL: $0, credential: $1)
         }) {
        self.store = store; self.makeClient = makeClient
    }

    func cancel() {
        request = UUID(); server = nil; legacyClient = nil; authorizedNode = nil; activeCredential = nil
        pairingCode = ""; phase = .idle; message = ""
    }
    func accepts(_ node: NativeAuthorizedNode) -> Bool { current(node.request, node.server) && phase == .ready }

    func preflight(server text: String) async -> NativeAuthorizedNode? {
        guard !isBusy else { return nil }
        let (operation, scope) = begin(text, phase: .checking)
        do {
            let base = try PlatformBaseURL(text)
            let credential = base.root.scheme == "https" ? try await store.load(for: base) : nil
            guard current(operation, scope), !Task.isCancelled else { return nil }
            let client = try makeClient(text, credential)
            do {
                let status = try await client.securityStatus()
                guard current(operation, scope), !Task.isCancelled else { return nil }
                switch status.mode {
                case .lanTLS:
                    guard !status.pairingRequired else {
                        phase = .pairingRequired
                        message = credential == nil ? "Требуется сопряжение этого Mac с узлом." : "Сохранённый доступ не принят. Выполните сопряжение заново."
                        return nil
                    }
                    guard credential != nil else { throw PlatformError.invalidResponse("Узел не подтвердил учётные данные") }
                    return ready(client, access: .pairedTLS, credential: credential, operation: operation, scope: scope)
                case .legacyLoopback:
                    guard base.isLoopback else { throw PlatformError.invalidResponse("Legacy loopback доступ разрешён только локальному узлу") }
                    return ready(try makeClient(text, nil), access: .legacyLoopback, operation: operation, scope: scope)
                }
            } catch PlatformError.http(status: 404, code: _, message: _) {
                guard current(operation, scope), !Task.isCancelled else { return nil }
                legacyClient = try makeClient(text, nil)
                phase = .legacyConfirmation
                message = "Сервер не поддерживает защищённое сопряжение (HTTP 404). Подтвердите подключение к старому API без сопряжения."
            }
        } catch {
            if current(operation, scope), !Task.isCancelled { phase = .failed; message = describe(error) }
        }
        return nil
    }

    func confirmLegacy(server text: String) -> NativeAuthorizedNode? {
        guard phase == .legacyConfirmation, server == PlatformExactID(text), let legacyClient else { return nil }
        return ready(legacyClient, access: .confirmedLegacy, operation: request, scope: PlatformExactID(text))
    }

    func pair(server text: String) async -> NativeAuthorizedNode? {
        guard phase == .pairingRequired, server == PlatformExactID(text), !pairingCode.isEmpty else { return nil }
        let code = pairingCode, clientLabel = label; pairingCode = ""
        let (operation, scope) = begin(text, phase: .pairing)
        do {
            let base = try PlatformBaseURL(text)
            let credential = try await makeClient(text, nil).pairNative(pairingCode: code, label: clientLabel)
            guard current(operation, scope), !Task.isCancelled else { return nil }
            let client = try makeClient(text, credential)
            let status = try await client.securityStatus()
            guard current(operation, scope), !Task.isCancelled else { return nil }
            guard status.mode == .lanTLS, !status.pairingRequired else { throw PlatformError.invalidResponse("Узел не подтвердил сопряжение") }
            do { try await store.save(credential, for: base) }
            catch {
                // Never retain an issued credential only in memory when Keychain fails.
                // Retire this newly issued access, without touching any worker/project.
                try? await client.revokeCurrentCredential()
                throw error
            }
            guard current(operation, scope), !Task.isCancelled else { return nil }
            return ready(client, access: .pairedTLS, credential: credential, operation: operation, scope: scope)
        } catch {
            if current(operation, scope), !Task.isCancelled { phase = .pairingRequired; message = describe(error) }
            return nil
        }
    }

    /// A deliberate server revocation is separate from closing/disconnecting UI.
    /// True means remote access was revoked even if local Keychain removal failed.
    func revokeCurrent(server text: String) async -> Bool {
        guard !isBusy, let node = authorizedNode, let credential = activeCredential,
              node.access == .pairedTLS, accepts(node), node.server == PlatformExactID(text) else { return false }
        let (operation, scope) = begin(text, phase: .revoking)
        do {
            try await node.client.revokeCurrentCredential()
            do { try await store.remove(for: PlatformBaseURL(text), matching: credential) }
            catch {
                if current(operation, scope) { phase = .failed; message = "Доступ на узле отозван. " + describe(error) }
                return true
            }
            if current(operation, scope) { phase = .idle; message = "Текущее сопряжение отозвано; охрана ядра не изменена." }
            return true
        } catch {
            if current(operation, scope), !Task.isCancelled {
                _ = ready(node.client, access: node.access, credential: credential, operation: operation, scope: scope)
                message = describe(error)
            }
            return false
        }
    }

    func forget(server text: String) async -> Bool {
        guard !isBusy else { return false }
        let (operation, scope) = begin(text, phase: .forgetting)
        do {
            try await store.remove(for: PlatformBaseURL(text))
            guard current(operation, scope), !Task.isCancelled else { return false }
            phase = .idle; message = "Сопряжение забыто на этом Mac. Для отзыва на узле используйте действие отзыва доступа."
            return true
        } catch {
            if current(operation, scope), !Task.isCancelled { phase = .failed; message = describe(error) }
            return false
        }
    }

    func authenticationExpired(server text: String) {
        _ = begin(text, phase: .pairingRequired)
        message = "Доступ этого Mac больше не принят. Охрана остаётся в ядре; выполните сопряжение заново."
    }

    private func begin(_ text: String, phase: Phase) -> (UUID, PlatformExactID) {
        request = UUID(); server = PlatformExactID(text); legacyClient = nil; authorizedNode = nil; activeCredential = nil
        self.phase = phase; message = ""
        return (request, PlatformExactID(text))
    }
    private func current(_ operation: UUID, _ scope: PlatformExactID) -> Bool { request == operation && server == scope }
    private func ready(_ client: PlatformClient, access: NativeNodeAccess, credential: PlatformBearerCredential? = nil, operation: UUID, scope: PlatformExactID) -> NativeAuthorizedNode {
        let node = NativeAuthorizedNode(client: client, access: access, server: scope, request: operation)
        authorizedNode = node; activeCredential = credential; phase = .ready
        switch access {
        case .pairedTLS: message = "HTTPS · сопряжение сохранено в связке ключей этого Mac."
        case .legacyLoopback: message = "Локальный loopback API · без сетевого сопряжения."
        case .confirmedLegacy: message = "Старый API · подключение без сопряжения подтверждено для этой сессии UI."
        }
        return node
    }
    private func describe(_ error: Error) -> String {
        if let error = error as? NativeCredentialStoreError { return error.localizedDescription }
        if case PlatformError.http(let status, _, _) = error { return "Узел отклонил запрос: HTTP \(status). Проверьте код и открытое оператором окно сопряжения." }
        if case PlatformError.invalidURL(let message) = error { return message }
        if case PlatformError.invalidCommand(let message) = error { return message }
        if case PlatformError.connection = error { return "Не удалось установить доверенное соединение. Проверьте адрес и установите CA узла из доверенного источника в системную связку ключей." }
        return "Подключение или сохранение сопряжения не завершено. Проверьте состояние узла и доступ к связке ключей."
    }
}
