import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class CommittedEventTransportTests: XCTestCase {
    final class Stub: URLProtocol {
        static var respond: ((Stub) -> Void)?
        static var stopped: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.respond?(self) }
        override func stopLoading() { Self.stopped?() }
        func headers(_ status: Int = 200, type: String = "text/event-stream") {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": type])!, cacheStoragePolicy: .notAllowed)
        }
        func send(_ value: String) { client?.urlProtocol(self, didLoad: Data(value.utf8)) }
        func finish() { client?.urlProtocolDidFinishLoading(self) }
    }
    override func tearDown() { Stub.respond = nil; Stub.stopped = nil; super.tearDown() }
    private func client() throws -> PlatformClient {
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root", session: URLSession(configuration: configuration))
    }

    func testCommittedEventTransportUsesCursorAndDoesNotRefetchSnapshots() async throws {
        let request = expectation(description: "one streaming request")
        Stub.respond = { stub in
            XCTAssertEqual(stub.request.httpMethod, "GET")
            XCTAssertEqual(stub.request.url?.path, "/root/api/v1/events")
            XCTAssertEqual(stub.request.url?.query, "after=42")
            XCTAssertEqual(stub.request.value(forHTTPHeaderField: "Last-Event-ID"), "42")
            stub.headers()
            stub.send("id: 43\nevent: session.changed\ndata: {\"sequence\":43,\"event_type\":\"session.changed\",\"occurred_at\":\"2026-11-11T00:00:00Z\",\"payload\":{\"unknown_future_field\":true}}\n\n")
            stub.finish(); request.fulfill()
        }
        let api = try client(); var events: [PlatformCommittedEvent] = []
        for try await event in try api.committedEvents(after: 42) { events.append(event) }
        await fulfillment(of: [request], timeout: 1)
        XCTAssertEqual(events.map(\.sequence), [43])
    }

    func testWatermarkReadsHealthEnvelopeAndRejectsNegativeCursor() async throws {
        Stub.respond = { stub in
            XCTAssertEqual(stub.request.url?.path, "/root/api/v1/health")
            stub.headers(type: "application/json")
            stub.send("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":{\"last_event_id\":9007199254740993}}")
            stub.finish()
        }
        let api = try client()
        let cursor = try await api.eventWatermark(); XCTAssertEqual(cursor, 9_007_199_254_740_993)
        XCTAssertThrowsError(try api.committedEvents(after: -1))
    }

    func testNonSSEHTTPResponseIsVisibleAsFailure() async throws {
        Stub.respond = { stub in stub.headers(404, type: "application/json"); stub.finish() }
        let api = try client()
        do {
            for try await _ in try api.committedEvents(after: 0) {}
            XCTFail("unsupported SSE must request recovery")
        } catch {
            guard case PlatformError.http(status: 404, code: _, message: _) = error else { return XCTFail("unexpected \(error)") }
        }
    }

    func testCancellingConsumerClosesOnlyItsSubscription() async throws {
        let started = expectation(description: "stream started"), stopped = expectation(description: "UI subscription cancelled")
        Stub.respond = { stub in stub.headers(); started.fulfill() }
        Stub.stopped = { stopped.fulfill() }
        let api = try client()
        let task = Task { for try await _ in try api.committedEvents(after: 0) {} }
        await fulfillment(of: [started], timeout: 1)
        task.cancel(); _ = await task.result
        await fulfillment(of: [stopped], timeout: 1)
    }
}
