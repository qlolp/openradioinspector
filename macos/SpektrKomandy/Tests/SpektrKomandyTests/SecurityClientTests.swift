import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class SecurityClientTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handle: ((Stub, URLRequest) throws -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do { try Self.handle?(self, bufferedTestRequest(request)) }
            catch { client?.urlProtocol(self, didFailWithError: error) }
        }
        override func stopLoading() {}
        func reply(_ value: Any, status: Int = 200, headers: [String: String] = [:]) throws {
            let body: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": value]
            raw(try JSONSerialization.data(withJSONObject: body), status: status, headers: headers)
        }
        func raw(_ data: Data, status: Int = 200, headers: [String: String] = [:]) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handle = nil; super.tearDown() }
    private func api(_ server: String = "https://node.example", credential: PlatformBearerCredential? = nil) throws -> PlatformClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: server, session: URLSession(configuration: configuration), credential: credential)
    }
    private func credential() throws -> PlatformBearerCredential { try .init(token: "synthetic-native-secret") }
    private let cookieHeader = "ori_session=synthetic-browser-secret; Path=/api; Secure; HttpOnly; SameSite=Strict"

    func testCredentialIsRedactedAndCannotInjectHeadersOrTravelOverHTTP() throws {
        let value = try credential()
        XCTAssertEqual(value.dataRepresentation, Data("synthetic-native-secret".utf8))
        XCTAssertFalse(String(describing: value).contains("synthetic-native-secret"))
        XCTAssertFalse(String(reflecting: value).contains("synthetic-native-secret"))
        XCTAssertTrue(Mirror(reflecting: value).children.isEmpty)
        for token in ["", "a\r\nCookie: injected", "a b", "a\t", "токен", String(repeating: "a", count: 4097)] {
            XCTAssertThrowsError(try PlatformBearerCredential(token: token))
        }
        XCTAssertThrowsError(try api("http://127.0.0.1:8770", credential: value))
        XCTAssertEqual(try api("HTTPS://node.example", credential: value).baseURL.root.scheme, "https")
    }

    func testStatusRetainsAdditiveCompatibilityAndRejectsUnconfirmedModes() async throws {
        Stub.handle = { stub, request in
            XCTAssertEqual(request.url?.path, "/api/v1/security/status")
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            try stub.reply(["mode": "lan_tls", "pairing_required": true, "future_field": ["preserved_by_server": true]])
        }
        let value = try await api().securityStatus()
        XCTAssertEqual(value.mode, .lanTLS); XCTAssertTrue(value.pairingRequired)
        for mode in ["unknown", "legacy_loopback"] {
            Stub.handle = { stub, _ in try stub.reply(["mode": mode, "pairing_required": true]) }
            do { _ = try await api().securityStatus(); XCTFail("Inconsistent status was accepted") }
            catch { XCTAssertTrue(error is PlatformError) }
        }
        Stub.handle = { stub, _ in try stub.reply(["mode": "legacy_loopback", "pairing_required": false]) }
        let legacy = try await api("http://127.0.0.1:8770").securityStatus()
        XCTAssertEqual(legacy.mode, .legacyLoopback)
    }

    func testNativePairingUsesExactBoundedTLSBodyAndReturnsOnlyRedactedCredential() async throws {
        Stub.handle = { stub, request in
            XCTAssertEqual(request.url?.path, "/api/v1/security/pair")
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String])
            XCTAssertEqual(body, ["pairing_code": "123456", "label": "Этот Mac", "client": "native"])
            try stub.reply(["paired": true, "credential": "bearer", "token": "synthetic-native-secret", "future": true])
        }
        let result = try await api().pairNative(pairingCode: "123456", label: "Этот Mac")
        XCTAssertEqual(result.dataRepresentation, try credential().dataRepresentation)
        Stub.handle = { _, _ in XCTFail("Invalid pairing must fail before transport") }
        for (server, label) in [("http://127.0.0.1:8770", "Mac"), ("https://node.example", String(repeating: "é", count: 3000))] {
            do { _ = try await api(server).pairNative(pairingCode: "123456", label: label); XCTFail("Unsafe pairing was accepted") }
            catch { XCTAssertTrue(error is PlatformError) }
        }
    }

    func testMalformedPairingResponseNeverBecomesACredentialOrLeaksServerToken() async throws {
        for body: [String: Any] in [["paired": false, "credential": "bearer", "token": "synthetic-server-secret"],
                                    ["paired": true, "credential": "browser_cookie", "token": "synthetic-server-secret"],
                                    ["paired": true, "credential": "bearer", "token": "synthetic-server-secret\r\n"]] {
            Stub.handle = { stub, _ in try stub.reply(body) }
            do { _ = try await api().pairNative(pairingCode: "123456", label: "Mac"); XCTFail("Malformed pairing was accepted") }
            catch { XCTAssertFalse(error.localizedDescription.contains("synthetic-server-secret")) }
        }
    }

    func testBearerIsReusedForJSONSSEAndRawDeliveryWithoutNativeCookies() async throws {
        let api = try api(credential: credential())
        let lock = NSLock(); var paths: [String] = []
        Stub.handle = { stub, request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-native-secret")
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            XCTAssertFalse(request.httpShouldHandleCookies)
            let path = request.url!.path; lock.withLock { paths.append(path) }
            if path.hasSuffix("/events") {
                stub.raw(Data("id: 1\nevent: session.changed\ndata: {\"sequence\":1,\"event_type\":\"session.changed\",\"occurred_at\":\"2026-11-11T00:00:00Z\",\"payload\":{}}\n\n".utf8), headers: ["Content-Type": "text/event-stream"])
            } else if path.hasSuffix("/sample") { stub.raw(Data([1, 2, 3])) }
            else { try stub.reply([], headers: ["Set-Cookie": self.cookieHeader]) }
        }
        _ = try await api.listSessions()
        _ = try await api.listSessions()
        var sequences: [Int64] = []
        for try await event in try api.committedEvents(after: 0) { sequences.append(event.sequence) }
        let (bytes, _) = try await api.receivePrefix(URLRequest(url: api.baseURL.endpoint(path: "/sample")), maxBytes: 3)
        XCTAssertEqual(sequences, [1]); XCTAssertEqual(bytes, Data([1, 2, 3]))
        XCTAssertEqual(lock.withLock { paths }, ["/api/v1/sessions", "/api/v1/sessions", "/api/v1/events", "/api/v1/sample"])
        XCTAssertFalse(api.eventSessionConfiguration.httpShouldSetCookies)
        XCTAssertNil(api.eventSessionConfiguration.httpCookieStorage)
    }

    func testBrowserExchangeReturnsOnlyAValidatedHttpOnlyCookieAndRevocationUsesSameBearer() async throws {
        let api = try api(credential: credential())
        Stub.handle = { stub, request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer synthetic-native-secret")
            XCTAssertNil(request.value(forHTTPHeaderField: "Cookie"))
            XCTAssertEqual(try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String], [:])
            if request.url?.path == "/api/v1/security/browser-session" {
                try stub.reply(["paired": true, "credential": "browser_cookie"], headers: ["Set-Cookie": self.cookieHeader])
            } else {
                XCTAssertEqual(request.url?.path, "/api/v1/security/credentials/current/revoke")
                try stub.reply(["revoked": true])
            }
        }
        let session = try await api.browserSession()
        let cookie = try session.makeCookie()
        XCTAssertEqual(cookie.name, "ori_session"); XCTAssertEqual(cookie.domain, "node.example")
        XCTAssertEqual(cookie.path, "/api"); XCTAssertTrue(cookie.isSecure); XCTAssertTrue(cookie.isHTTPOnly)
        XCTAssertFalse(String(reflecting: session).contains("synthetic-browser-secret"))
        XCTAssertTrue(Mirror(reflecting: session).children.isEmpty)
        try await api.revokeCurrentCredential()
        Stub.handle = { stub, _ in try stub.reply(["revoked": false]) }
        do { try await api.revokeCurrentCredential(); XCTFail("Unconfirmed revocation was accepted") }
        catch { XCTAssertTrue(error is PlatformError) }
    }

    func testCookieExchangeRejectsMissingFlagsBroaderDomainsWrongPathsAndOversize() async throws {
        let api = try api(credential: credential())
        for header in [cookieHeader.replacingOccurrences(of: "; Secure", with: ""),
                       cookieHeader.replacingOccurrences(of: "; HttpOnly", with: ""),
                       cookieHeader.replacingOccurrences(of: "Strict", with: "None"),
                       cookieHeader.replacingOccurrences(of: "/api", with: "/"),
                       cookieHeader + "; Domain=other.example", cookieHeader + "; Domain=.node.example",
                       cookieHeader + "; SameSite=None", cookieHeader + "; Path=/api", cookieHeader + "; ="] {
            Stub.handle = { stub, _ in try stub.reply(["paired": true, "credential": "browser_cookie"], headers: ["Set-Cookie": header]) }
            do { _ = try await api.browserSession(); XCTFail("Unsafe browser cookie was accepted") }
            catch { XCTAssertFalse(error.localizedDescription.contains("synthetic-browser-secret")) }
        }
        Stub.handle = { stub, _ in stub.raw(Data(repeating: 32, count: 4097), headers: ["Set-Cookie": self.cookieHeader]) }
        do { _ = try await api.browserSession(); XCTFail("Oversized security response was accepted") }
        catch { XCTAssertTrue(error is PlatformError) }
    }

    func testCertificateFailureIsNeverRetriedOverPlaintextOrTreatedAsLegacy() async throws {
        var requests: [URL] = []
        Stub.handle = { _, request in requests.append(request.url!); throw URLError(.serverCertificateUntrusted) }
        do { _ = try await api().securityStatus(); XCTFail("Untrusted TLS was accepted") }
        catch { XCTAssertTrue(error is PlatformError) }
        XCTAssertEqual(requests.map(\.scheme), ["https"])
    }
}
