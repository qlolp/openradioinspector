import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// A credential is deliberately neither Codable nor printable. Its explicit
/// representation is for a platform's secure credential store, never preferences.
public struct PlatformBearerCredential: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    private let token: String
    public init(token: String) throws {
        guard !token.isEmpty, token.utf8.count <= 4096,
              token.utf8.allSatisfy({ (33...126).contains($0) }) else {
            throw PlatformError.invalidCommand("Некорректные данные сопряжения")
        }
        self.token = token
    }
    public var dataRepresentation: Data { Data(token.utf8) }
    public var description: String { "<Bearer credential: redacted>" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: EmptyCollection<Mirror.Child>()) }
    func authorize(_ request: inout URLRequest) { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
}

public struct PlatformNodeSecurityStatus: Decodable, Equatable, Sendable {
    public enum Mode: String, Decodable, Sendable { case lanTLS = "lan_tls", legacyLoopback = "legacy_loopback" }
    public let mode: Mode
    public let pairingRequired: Bool
}

/// Only this validated cookie may cross into a separate WKHTTPCookieStore.
/// Keeping a value wrapper avoids moving Foundation cookie objects between actors.
public struct PlatformBrowserSessionCookie: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    private let header: String
    private let origin: URL
    public var description: String { "<Browser session cookie: redacted>" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: EmptyCollection<Mirror.Child>()) }
    init(header: String, origin: URL) throws {
        self.header = header; self.origin = origin
        let attributes = header.split(separator: ";").dropFirst().map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
        let names = attributes.map { String($0.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)[0]) }
        guard header.utf8.count <= 8192, origin.scheme == "https",
              header.utf8.allSatisfy({ (32...126).contains($0) }), !names.contains(""), Set(names).count == names.count,
              attributes.contains("secure"), attributes.contains("httponly"), attributes.contains("samesite=strict"),
              attributes.contains("path=/api"), !attributes.contains(where: { $0.hasPrefix("domain=") }) else {
            throw PlatformError.invalidResponse("Некорректная защищённая браузерная сессия")
        }
        _ = try makeCookie()
    }
    public func makeCookie() throws -> HTTPCookie {
        let cookies = HTTPCookie.cookies(withResponseHeaderFields: ["Set-Cookie": header], for: origin)
        guard cookies.count == 1, let cookie = cookies.first, cookie.name == "ori_session", !cookie.value.isEmpty,
              cookie.path == "/api", cookie.isSecure, cookie.isHTTPOnly,
              cookie.domain.lowercased() == origin.host?.lowercased() else {
            throw PlatformError.invalidResponse("Некорректная защищённая браузерная сессия")
        }
        return cookie
    }
}

extension PlatformClient {
    public func securityStatus() async throws -> PlatformNodeSecurityStatus {
        do {
            let result: PlatformNodeSecurityStatus = try await get("/security/status", expectedStatuses: [200])
            guard (result.mode != .lanTLS || baseURL.root.scheme == "https"),
                  result.mode != .legacyLoopback || !result.pairingRequired else {
                throw PlatformError.invalidResponse("Несогласованное состояние защиты узла")
            }
            return result
        } catch { throw securityError(error) }
    }

    /// The operator opens the limited pairing window on the core. This method
    /// submits exactly one code; it never opens a window or retries a code.
    public func pairNative(pairingCode: String, label: String) async throws -> PlatformBearerCredential {
        try requireTLS()
        struct Body: Encodable, Sendable { let pairingCode: String; let label: String; let client = "native" }
        struct Reply: Decodable, Sendable { let paired: Bool; let credential: String; let token: String }
        let body = Body(pairingCode: pairingCode, label: label)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard !pairingCode.isEmpty, !label.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              try encoder.encode(body).count <= 4096 else {
            throw PlatformError.invalidCommand("Введите код и имя клиента; запрос сопряжения ограничен 4 КиБ")
        }
        do {
            let reply: Reply = try await post("/security/pair", body: body, expectedStatuses: [200])
            guard reply.paired, reply.credential == "bearer" else {
                throw PlatformError.invalidResponse("Сервер не подтвердил сопряжение native-клиента")
            }
            return try PlatformBearerCredential(token: reply.token)
        } catch { throw securityError(error) }
    }

    public func revokeCurrentCredential() async throws {
        try requireTLSCredential()
        do {
            struct Reply: Decodable, Sendable { let revoked: Bool }
            let reply: Reply = try await post("/security/credentials/current/revoke", body: SecurityEmptyBody(), expectedStatuses: [200])
            guard reply.revoked else { throw PlatformError.invalidResponse("Узел не подтвердил отзыв доступа") }
        } catch { throw securityError(error) }
    }

    public func browserSession() async throws -> PlatformBrowserSessionCookie {
        try requireTLSCredential()
        var request = URLRequest(url: try baseURL.endpoint(path: "/security/browser-session"))
        request.httpMethod = "POST"; request.timeoutInterval = 5
        request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        do {
            let (data, response) = try await receivePrefix(request, maxBytes: 4097)
            try Task.checkCancellation()
            guard data.count <= 4096 else { throw PlatformError.responseTooLarge }
            guard let http = response as? HTTPURLResponse else { throw PlatformError.invalidResponse("Нет ответа на запрос браузерной сессии") }
            guard http.statusCode == 200 else {
                throw PlatformError.http(status: http.statusCode, code: "browser_session", message: "HTTP \(http.statusCode)")
            }
            struct Reply: Decodable { let paired: Bool; let credential: String }
            let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
            let reply = try decoder.decode(PlatformEnvelope<Reply>.self, from: data).data
            guard reply.paired, reply.credential == "browser_cookie", let header = http.value(forHTTPHeaderField: "Set-Cookie") else {
                throw PlatformError.invalidResponse("Сервер не подтвердил защищённую браузерную сессию")
            }
            return try PlatformBrowserSessionCookie(header: header, origin: baseURL.root)
        } catch { throw securityError(error) }
    }

    private func requireTLS() throws {
        guard baseURL.root.scheme == "https" else { throw PlatformError.invalidURL("Для сопряжения требуется HTTPS с доверенным сертификатом") }
    }
    private func requireTLSCredential() throws {
        try requireTLS()
        guard hasBearerCredential else { throw PlatformError.invalidCommand("Сначала выполните сопряжение этого Mac") }
    }
}

private struct SecurityEmptyBody: Encodable, Sendable {}

/// Security responses must not echo a peer's untrusted error text into UI/logs.
private func securityError(_ error: Error) -> Error {
    if Task.isCancelled || error is CancellationError { return CancellationError() }
    if let error = error as? PlatformError {
        switch error {
        case .http(let status, _, _):
            return PlatformError.http(status: status, code: "security_request", message: "Запрос защиты узла отклонён: HTTP \(status)")
        case .invalidResponse, .invalidCommand:
            return PlatformError.invalidResponse("Некорректный ответ защиты узла")
        default: return error
        }
    }
    return PlatformError.invalidResponse("Не удалось проверить защищённое подключение")
}
