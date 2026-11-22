import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Security)
import Security
#endif

/// A user-selected anchor applies only to this node and only to the reader's JSON requests.
struct PlatformReaderTrust: Sendable {
    let host: String
    let certificateDER: Data

    func evaluate(_ challenge: URLAuthenticationChallenge) -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        #if canImport(Security)
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = space.serverTrust, space.host.caseInsensitiveCompare(host) == .orderedSame,
              let anchor = SecCertificateCreateWithData(nil, certificateDER as CFData),
              SecTrustSetPolicies(trust, SecPolicyCreateSSL(true, host as CFString)) == errSecSuccess,
              SecTrustSetAnchorCertificates(trust, [anchor] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
              SecTrustSetNetworkFetchAllowed(trust, false) == errSecSuccess,
              SecTrustEvaluateWithError(trust, nil) else { return (.cancelAuthenticationChallenge, nil) }
        return (.useCredential, URLCredential(trust: trust))
        #else
        return (.cancelAuthenticationChallenge, nil)
        #endif
    }
}

/// Field exposes only pairing, credential revocation and committed archive reads.
/// A saved credential must pass verify() before the app opens a node view.
public struct PlatformFieldReader: Sendable {
    private let client: PlatformClient

    init(client: PlatformClient) { self.client = client }

    public static func origin(_ input: String) throws -> URL {
        let url = try PlatformBaseURL(input).root
        guard url.scheme == "https", let host = url.host, !host.isEmpty, url.path.isEmpty || url.path == "/",
              url.port.map({ (1...65535).contains($0) }) ?? true else {
            throw PlatformError.invalidURL("Введите HTTPS-адрес узла с именем из сертификата.")
        }
        return url
    }

    #if canImport(Security)
    public init(node: String, certificateDER: Data, credential: PlatformBearerCredential? = nil) throws {
        let origin = try Self.origin(node)
        guard certificateDER.count <= 65536, SecCertificateCreateWithData(nil, certificateDER as CFData) != nil else {
            throw PlatformError.invalidCommand("Выберите сертификат локального центра сертификации.")
        }
        let trust = PlatformReaderTrust(host: origin.host(percentEncoded: false)!.trimmingCharacters(in: CharacterSet(charactersIn: "[]")),
                                        certificateDER: certificateDER)
        client = try PlatformClient(baseURL: origin.absoluteString, credential: credential, readerTrust: trust)
    }
    #endif

    public func pair(code: String) async throws -> PlatformBearerCredential {
        let status = try await client.securityStatus()
        guard !client.hasBearerCredential, status.mode == .lanTLS, status.readerPairing == true else {
            throw PlatformError.invalidResponse("Узел должен поддерживать сопряжение для чтения.")
        }
        return try await client.pairNative(pairingCode: code, label: "OpenRadioInspector Field iPhone", requiredScope: "reader")
    }

    public func verify() async throws {
        let status = try await client.securityStatus()
        guard client.hasBearerCredential, status.mode == .lanTLS, !status.pairingRequired,
              status.readerPairing == true, status.accessScope == "reader" else {
            throw PlatformError.invalidResponse("Узел не подтвердил доступ для чтения. Проверьте сопряжение.")
        }
    }

    public func revoke() async throws { try await client.revokeCurrentCredential() }
    public func sessions() async throws -> [PlatformSession] { try await client.listSessions() }
    public func watermark() async throws -> Int64 { try await client.eventWatermark() }

    public func history(session: String, stream: String, after: String? = nil) async throws -> PlatformArchiveSweepPage {
        try await client.archiveSweepPage(sessionID: session, streamID: stream, after: after)
    }

    public func sweep(session: String, stream: String, id: String) async throws -> PlatformSweep {
        try await client.archiveOriginalSweep(sessionID: session, streamID: stream, sweepID: id)
    }

    public func events(after: Int64, limit: Int = 16) async throws -> [PlatformCommittedEvent] {
        guard after >= 0, (1...32).contains(limit) else { throw PlatformEventError.malformedEvent }
        do {
            return try await client.get("/events/history", query: [.init(name: "after", value: String(after)),
                                                                     .init(name: "limit", value: String(limit))])
        } catch PlatformError.responseTooLarge where limit > 1 {
            return try await events(after: after, limit: max(1, limit / 2))
        }
    }
}

/// Advance only after a complete page validates. A retry can replay the same page safely.
public struct PlatformFieldJournal: Sendable {
    public private(set) var cursor: Int64
    public private(set) var recent: [PlatformFieldEvent] = []
    public init(watermark: Int64) { cursor = max(0, watermark - min(max(0, watermark), 32)) }

    public mutating func append(_ page: [PlatformCommittedEvent]) throws {
        var next = cursor
        var additions: [PlatformFieldEvent] = []
        for event in page where event.sequence > cursor {
            guard next < Int64.max, event.sequence == next + 1 else {
                throw PlatformEventError.sequenceGap(expected: next == Int64.max ? next : next + 1, received: event.sequence)
            }
            next = event.sequence
            additions.append(PlatformFieldEvent(event: event))
        }
        recent = Array((recent + additions).suffix(64))
        cursor = next
    }
}

/// The event list keeps lightweight identities. Large source arrays remain in the archive.
public struct PlatformFieldEvent: Sendable, Identifiable {
    public let id: Int64
    public let type, occurredAt: String
    public let sessionID, streamID, sweepID: String?

    init(event: PlatformCommittedEvent) {
        id = event.sequence
        type = event.eventType
        occurredAt = event.occurredAt
        sessionID = event.sessionId
        streamID = event.payload.sweep?.streamId ?? event.payload.observation?.streamId
        sweepID = event.payload.sweep?.id
    }
}
