import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@MainActor @Suite(.serialized) struct NativeArchiveBrowserTests {
    final class Stub: URLProtocol {
        final class Pending: @unchecked Sendable {
            let lock = NSLock()
            private var value: Stub?
            func set(_ value: Stub?) { lock.withLock { self.value = value } }
            func get() -> Stub? { lock.withLock { value } }
        }
        static let pending = Pending()
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.pending.set(self) }
        override func stopLoading() {}
        func finish(empty: Bool = false, status: Int = 200) throws {
            let row: [String: Any] = ["id": "old", "session_id": "s", "stream_id": "t",
                "interval": ["started_at": "2026-11-14T12:00:00Z", "ended_at": "2026-11-14T12:00:05Z"],
                "point_count": 2, "level_unit": "dBm", "min_frequency_hz": 88, "max_frequency_hz": 108,
                "peak_frequency_hz": 88, "peak_level": -90]
            let data = try JSONSerialization.data(withJSONObject: ["contract": "openradioinspector-platform", "contract_version": 1,
                "data": ["items": empty ? [] : [row], "next_cursor": empty ? NSNull() : "old-cursor"] as [String: Any]])
            let url = try #require(request.url)
            let response = try #require(HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil))
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
    }
    @Test func resettingScopeDiscardsInFlightArchivePageAndCursor() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); Stub.pending.set(nil) }
        let client = try PlatformClient(baseURL: "https://example.test", session: session)
        let browser = NativeArchiveBrowser()
        let task = Task { await browser.search(client: client, session: "s", stream: "t") }
        for _ in 0..<200 {
            if Stub.pending.get() != nil { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        let pending = try #require(Stub.pending.get())
        #expect(browser.busy)
        browser.reset()
        try pending.finish()
        await task.value
        #expect(browser.rows.isEmpty)
        #expect(browser.nextCursor == nil)
        #expect(browser.message.isEmpty)
        #expect(!browser.busy)
    }

    private func respond(to task: Task<Void, Never>, empty: Bool = false, status: Int = 200) async throws {
        for _ in 0..<200 {
            if Stub.pending.get() != nil { break }
            try await Task.sleep(for: .milliseconds(5))
        }
        let request = try #require(Stub.pending.get())
        try request.finish(empty: empty, status: status)
        await task.value
        Stub.pending.set(nil)
    }

    @Test func emptySearchReplacesPreviousPageAndCursor() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); Stub.pending.set(nil) }
        let client = try PlatformClient(baseURL: "https://example.test", session: session)
        let browser = NativeArchiveBrowser()
        try await respond(to: Task { await browser.search(client: client, session: "s", stream: "t") })
        #expect(browser.rows.count == 1)
        #expect(browser.nextCursor != nil)
        try await respond(to: Task { await browser.search(client: client, session: "s", stream: "t") }, empty: true)
        #expect(browser.didLoad)
        #expect(browser.rows.isEmpty)
        #expect(browser.nextCursor == nil)
        #expect(!browser.busy)
    }

    @Test func failedSearchCanBeRetriedUsingTheSameClient() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); Stub.pending.set(nil) }
        let client = try PlatformClient(baseURL: "https://example.test", session: session)
        let browser = NativeArchiveBrowser()
        try await respond(to: Task { await browser.search(client: client, session: "s", stream: "t") }, status: 503)
        #expect(!browser.didLoad)
        #expect(!browser.busy)
        #expect(browser.message.contains("Архив недоступен"))
        try await respond(to: Task { await browser.search(client: client, session: "s", stream: "t") })
        #expect(browser.didLoad)
        #expect(browser.rows.first?.id == "old")
        #expect(!browser.message.contains("недоступен"))
    }
}
