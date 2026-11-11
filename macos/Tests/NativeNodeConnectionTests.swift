import Foundation
import XCTest
import WebKit
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeNodeConnectionTests: XCTestCase {
    actor MemoryStore: NativeCredentialStoring {
        var values: [PlatformExactID: PlatformBearerCredential] = [:]
        var failSave = false
        func load(for server: PlatformBaseURL) -> PlatformBearerCredential? { values[PlatformExactID(server.root.absoluteString)] }
        func save(_ credential: PlatformBearerCredential, for server: PlatformBaseURL) throws {
            if failSave { throw CocoaError(.fileWriteNoPermission) }
            values[PlatformExactID(server.root.absoluteString)] = credential
        }
        func remove(for server: PlatformBaseURL, matching credential: PlatformBearerCredential?) {
            let key = PlatformExactID(server.root.absoluteString)
            if let credential, values[key]?.dataRepresentation != credential.dataRepresentation { return }
            values.removeValue(forKey: key)
        }
        func rejectSaving() { failSave = true }
    }
    final class Stub: URLProtocol {
        static var handle: ((Stub) throws -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do { try Self.handle?(self) } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
        override func stopLoading() {}
        func reply(_ value: Any, status: Int = 200, headers: [String: String] = [:]) throws {
            let body: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": value]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try JSONSerialization.data(withJSONObject: body))
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    final class Responses: @unchecked Sendable {
        let lock = NSLock()
        var requests: [URLRequest] = []
        var held: Stub?
        var holdStatus = false
        var statusCode = 200
        var certificateError = false
        var requestCount: Int { lock.withLock { requests.count } }
        var revokeCount: Int { lock.withLock { requests.filter { $0.url!.path.hasSuffix("/revoke") }.count } }
        func handle(_ stub: Stub) throws {
            try lock.withLock {
                requests.append(stub.request)
                if certificateError { throw URLError(.serverCertificateUntrusted) }
                switch stub.request.url!.path {
                case "/api/v1/security/status":
                    if holdStatus { held = stub; return }
                    try stub.reply(["mode": "lan_tls", "pairing_required": stub.request.value(forHTTPHeaderField: "Authorization") != "Bearer synthetic-native-secret"], status: statusCode)
                case "/api/v1/security/pair": try stub.reply(["paired": true, "credential": "bearer", "token": "synthetic-native-secret"])
                case "/api/v1/security/credentials/current/revoke": try stub.reply(["revoked": true])
                default: XCTFail("Connection UI must not control a core worker: \(stub.request.url!.path)")
                }
            }
        }
        func release() throws {
            let stub = lock.withLock { let result = held; held = nil; return result }
            try stub?.reply(["mode": "lan_tls", "pairing_required": true])
        }
    }
    private let server = "https://node.example"
    override func tearDown() { Stub.handle = nil; super.tearDown() }
    @MainActor private func model(_ store: MemoryStore, _ responses: Responses) -> NativeNodeConnectionModel {
        Stub.handle = responses.handle
        return NativeNodeConnectionModel(store: store, makeClient: { server, credential in
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: server, session: URLSession(configuration: configuration), credential: credential)
        })
    }

    @MainActor func testPairingRequiresExplicitCodeThenPersistsBeforeReturningAuthenticatedClient() async throws {
        let store = MemoryStore(), responses = Responses(), model = model(store, responses)
        let initial = await model.preflight(server: server)
        XCTAssertNil(initial); XCTAssertEqual(model.phase, .pairingRequired)
        XCTAssertEqual(responses.requestCount, 1, "Preflight must never open or submit a pairing window")
        model.pairingCode = "123456"
        let paired = await model.pair(server: server)
        let node = try XCTUnwrap(paired)
        XCTAssertTrue(node.client.hasBearerCredential); XCTAssertEqual(node.access, .pairedTLS)
        XCTAssertTrue(model.accepts(node)); XCTAssertEqual(model.pairingCode, "")
        let stored = await store.load(for: try PlatformBaseURL(server))
        XCTAssertEqual(stored?.dataRepresentation, Data("synthetic-native-secret".utf8))
        XCTAssertEqual(responses.requestCount, 3)
    }

    @MainActor func testOnly404OffersExplicitLegacyCompatibilityAndItDoesNotReuseBearer() async throws {
        let store = MemoryStore(), responses = Responses()
        try await store.save(.init(token: "synthetic-native-secret"), for: PlatformBaseURL(server))
        responses.statusCode = 404
        let model = model(store, responses)
        let initial = await model.preflight(server: server)
        XCTAssertNil(initial); XCTAssertEqual(model.phase, .legacyConfirmation)
        let legacy = try XCTUnwrap(model.confirmLegacy(server: server))
        XCTAssertEqual(legacy.access, .confirmedLegacy); XCTAssertFalse(legacy.client.hasBearerCredential)
        XCTAssertEqual(responses.requestCount, 1)
        model.cancel(); XCTAssertFalse(model.accepts(legacy)); XCTAssertNil(model.confirmLegacy(server: server))
        responses.statusCode = 500
        let unavailable = await model.preflight(server: server)
        XCTAssertNil(unavailable); XCTAssertEqual(model.phase, .failed)
        XCTAssertNil(model.confirmLegacy(server: server))
    }

    @MainActor func testSystemCertificateFailureNeverOffersLegacyOrPlaintextRetry() async throws {
        let store = MemoryStore(), responses = Responses(); responses.certificateError = true
        let model = model(store, responses)
        let result = await model.preflight(server: server)
        XCTAssertNil(result); XCTAssertEqual(model.phase, .failed)
        XCTAssertNil(model.confirmLegacy(server: server)); XCTAssertEqual(responses.requestCount, 1)
        XCTAssertFalse(model.message.contains("synthetic-native-secret"))
    }

    @MainActor func testLatePreflightAfterClosingClientCannotConnectOrPersist() async throws {
        let store = MemoryStore(), responses = Responses(); responses.holdStatus = true
        let model = model(store, responses)
        let pending = Task { await model.preflight(server: server) }
        for _ in 0..<200 {
            if responses.requestCount > 0 { break }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertEqual(responses.requestCount, 1)
        model.cancel(); try responses.release()
        let result = await pending.value
        XCTAssertNil(result); XCTAssertEqual(model.phase, .idle); XCTAssertNil(model.authorizedNode)
        let stored = await store.load(for: try PlatformBaseURL(server)); XCTAssertNil(stored)
    }

    @MainActor func testKeychainFailureDoesNotFallBackToMemoryOrPreferencesAndRevokesNewCredential() async throws {
        let store = MemoryStore(), responses = Responses(), model = model(store, responses)
        await store.rejectSaving()
        _ = await model.preflight(server: server); model.pairingCode = "123456"
        let result = await model.pair(server: server)
        XCTAssertNil(result); XCTAssertEqual(model.phase, .pairingRequired)
        XCTAssertNil(model.authorizedNode); XCTAssertEqual(responses.revokeCount, 1)
        let stored = await store.load(for: try PlatformBaseURL(server)); XCTAssertNil(stored)
        XCTAssertFalse(model.message.contains("synthetic-native-secret"))
    }

    @MainActor func testRevokeInvalidatesOnlyThisCredentialAndNeverDisarmsTheCore() async throws {
        let store = MemoryStore(), responses = Responses()
        try await store.save(.init(token: "synthetic-native-secret"), for: PlatformBaseURL(server))
        let model = model(store, responses)
        let result = await model.preflight(server: server)
        let node = try XCTUnwrap(result)
        XCTAssertTrue(model.accepts(node))
        let revoked = await model.revokeCurrent(server: server)
        XCTAssertTrue(revoked)
        XCTAssertEqual(responses.revokeCount, 1); XCTAssertEqual(responses.requestCount, 2)
        let stored = await store.load(for: try PlatformBaseURL(server)); XCTAssertNil(stored)
        XCTAssertNil(model.authorizedNode)
    }

    @MainActor func testForgetIsLocalAndDoesNotDeleteAnotherNodesCredential() async throws {
        let store = MemoryStore(), responses = Responses()
        let other = try PlatformBaseURL("https://other.example")
        try await store.save(.init(token: "synthetic-native-secret"), for: PlatformBaseURL(server))
        try await store.save(.init(token: "other-synthetic-secret"), for: other)
        let model = model(store, responses)
        let forgottenLocally = await model.forget(server: server)
        XCTAssertTrue(forgottenLocally); XCTAssertEqual(responses.requestCount, 0)
        let forgotten = await store.load(for: try PlatformBaseURL(server)), retained = await store.load(for: other)
        XCTAssertNil(forgotten); XCTAssertNotNil(retained)
    }

    @MainActor func testRevokingAnOldWindowDoesNotEraseAnotherWindowsNewCredential() async throws {
        let store = MemoryStore(), responses = Responses()
        let base = try PlatformBaseURL(server)
        try await store.save(.init(token: "synthetic-native-secret"), for: base)
        let model = model(store, responses)
        _ = await model.preflight(server: server)
        try await store.save(.init(token: "new-window-secret"), for: base)
        let revoked = await model.revokeCurrent(server: server)
        XCTAssertTrue(revoked)
        let stored = await store.load(for: base)
        XCTAssertEqual(stored?.dataRepresentation, Data("new-window-secret".utf8))
    }

    @MainActor func testRevokedBearerDisconnectsOnlyUIAndRequestsPairingAgain() async throws {
        var requests: [URLRequest] = []
        let lock = NSLock()
        Stub.handle = { stub in
            lock.withLock { requests.append(stub.request) }
            try stub.reply([:], status: 401)
        }
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: server, session: URLSession(configuration: configuration), credential: .init(token: "synthetic-native-secret"))
        let remote = RemoteDualModel(); remote.serverURL = server
        remote.connect(startPolling: false, using: client)
        defer { remote.disconnect() }
        for _ in 0..<200 {
            if remote.authorizationRequired { break }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTAssertTrue(remote.authorizationRequired); XCTAssertFalse(remote.connected)
        XCTAssertTrue(lock.withLock { requests.allSatisfy { $0.httpMethod == "GET" } })
        XCTAssertTrue(remote.current.isEmpty)
    }

    @MainActor func testMonitoringReusesAuthenticatedClientAndReplacesItAtSameEndpoint() async throws {
        let lock = NSLock(); var headers: [String] = []
        Stub.handle = { stub in
            lock.withLock { headers.append(stub.request.value(forHTTPHeaderField: "Authorization") ?? "missing") }
            try stub.reply([])
        }
        func client(_ token: String) throws -> PlatformClient {
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: server, session: URLSession(configuration: configuration), credential: .init(token: token))
        }
        let workspace = MonitoringWorkspaceModel()
        workspace.configure(server, client: try client("first-synthetic-secret")); await workspace.load()
        let first = lock.withLock { headers }
        XCTAssertFalse(first.isEmpty); XCTAssertTrue(first.allSatisfy { $0 == "Bearer first-synthetic-secret" })
        lock.withLock { headers = [] }
        workspace.configure(server, client: try client("second-synthetic-secret")); await workspace.load()
        let second = lock.withLock { headers }
        XCTAssertFalse(second.isEmpty); XCTAssertTrue(second.allSatisfy { $0 == "Bearer second-synthetic-secret" })
    }

    @MainActor func testBrowserCookieIsInstalledInAnIsolatedNonpersistentWebKitStore() async throws {
        let store = WKWebsiteDataStore.nonPersistent()
        XCTAssertFalse(store.isPersistent)
        Stub.handle = { stub in
            try stub.reply(["paired": true, "credential": "browser_cookie"], headers: ["Set-Cookie": "ori_session=synthetic-browser-secret; Path=/api; Secure; HttpOnly; SameSite=Strict"])
        }
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: server, session: URLSession(configuration: configuration), credential: .init(token: "synthetic-native-secret"))
        let session = try await client.browserSession()
        let cookie = try session.makeCookie()
        await store.httpCookieStore.setCookie(cookie)
        let cookies = await store.httpCookieStore.allCookies()
        let installed = try XCTUnwrap(cookies.first { $0.name == "ori_session" })
        XCTAssertEqual(installed.path, "/api"); XCTAssertTrue(installed.isSecure); XCTAssertTrue(installed.isHTTPOnly)
        await store.httpCookieStore.delete(installed)
    }
}
