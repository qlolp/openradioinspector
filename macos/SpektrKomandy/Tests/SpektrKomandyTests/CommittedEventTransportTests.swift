import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class CommittedEventTransportTests: XCTestCase {
    private final class TimingBox: @unchecked Sendable {
        private let lock = NSLock()
        private var rows: [PlatformMeasurementTiming] = []
        func append(_ value: PlatformMeasurementTiming) { lock.lock(); defer { lock.unlock() }; rows.append(value) }
        func snapshot() -> [PlatformMeasurementTiming] { lock.lock(); defer { lock.unlock() }; return rows }
    }

    func testSSEReportsTransportDecodeAndBoundedQueueBeforeConsumerApplication() async throws {
        let raw = #"{"contract":"openradioinspector-platform","contract_version":1,"data":{"sequence":43,"event_type":"sweep.received","occurred_at":"2026-11-16T00:00:00.000002Z","session_id":"s","payload":{"sweep":{"id":"timed-sweep","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-16T00:00:00.000001Z","ended_at":"2026-11-16T00:00:00.000002Z"},"frequencies_hz":[100,200],"levels":[-90,-80],"level_unit":"dBm","settings":{}}}}}"#
        Stub.respond = { stub in
            stub.headers()
            stub.send("id: 43\nevent: sweep.received\ndata: " + raw + "\n\n")
            stub.finish()
        }
        let box = TimingBox(), api = try client()
        try await api.consumeMeasurements(after: 42, using: .serverSentEvents, observe: { box.append($0) }) { event in
            XCTAssertEqual(event.sequence, 43)
            XCTAssertEqual(event.source.originalData, Data(raw.utf8))
            let decoded = try XCTUnwrap(box.snapshot().first(where: { $0.stage == .decoded }))
            XCTAssertGreaterThan(decoded.atNanoseconds, 0)
            XCTAssertEqual(decoded.sourceID, "timed-sweep")
            XCTAssertEqual(decoded.eventType, "sweep.received")
        }
        let rows = box.snapshot()
        XCTAssertEqual(rows.map(\.stage), [.received, .decoded, .queued])
        XCTAssertTrue(rows.allSatisfy { $0.sequence == 43 && $0.transport == .serverSentEvents })
        XCTAssertLessThanOrEqual(rows[0].atNanoseconds, rows[1].atNanoseconds)
        XCTAssertLessThanOrEqual(rows[1].atNanoseconds, rows[2].atNanoseconds)
        XCTAssertEqual(rows[0].payloadBytes, raw.utf8.count)
        let queued = try XCTUnwrap(rows[2].queuedEvents)
        XCTAssertTrue((0...64).contains(queued))
    }

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
            stub.send("id: 43\nevent: session.changed\ndata: {\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":{\"sequence\":43,\"event_type\":\"session.changed\",\"occurred_at\":\"2026-11-11T00:00:00Z\",\"payload\":{\"unknown_future_field\":true}}}\n\n")
            stub.finish(); request.fulfill()
        }
        let api = try client(); var events: [PlatformCommittedEvent] = []
        for try await event in try api.committedEvents(after: 42) { events.append(event) }
        await fulfillment(of: [request], timeout: 1)
        XCTAssertEqual(events.map(\.sequence), [43])
    }

    func testQueueOverrunDrainsItsValidatedPrefixBeforeReportingRecovery() async throws {
        let cancelled = expectation(description: "bounded stream cancelled after queue overflow")
        Stub.stopped = { cancelled.fulfill() }
        Stub.respond = { stub in
            stub.headers()
            stub.send((1...70).map { sequence in
                "id: \(sequence)\nevent: future.event\ndata: {\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":{\"sequence\":\(sequence),\"event_type\":\"future.event\",\"occurred_at\":\"2026-11-11T00:00:00Z\",\"payload\":{}}}\n\n"
            }.joined())
        }
        let api = try client(), stream = try api.committedEvents(after: 0)
        // No iterator runs until the producer has filled its 64-event queue.
        await fulfillment(of: [cancelled], timeout: 1)
        var received: [Int64] = []
        do {
            for try await event in stream { received.append(event.sequence) }
            XCTFail("full queue did not request cursor recovery")
        } catch { XCTAssertEqual(error as? PlatformEventError, .consumerOverrun) }
        XCTAssertEqual(received, Array(1...64).map(Int64.init))
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

    func testConsecutiveWatermarksRevalidateHealthAndObserveExactArchiveRollback() async throws {
        let lock = NSLock()
        var reads = 0
        Stub.respond = { stub in
            XCTAssertEqual(stub.request.httpMethod, "GET")
            XCTAssertEqual(stub.request.url?.path, "/root/api/v1/health")
            XCTAssertEqual(stub.request.cachePolicy, .reloadIgnoringLocalCacheData,
                           "An archive reset check must not reuse cached health")
            XCTAssertEqual(stub.request.value(forHTTPHeaderField: "Cache-Control"), "no-cache")
            let cursor: Int64 = lock.withLock { reads += 1; return reads == 1 ? 9_007_199_254_740_993 : 7 }
            stub.headers(type: "application/json")
            stub.send("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":{\"last_event_id\":\(cursor)}}")
            stub.finish()
        }
        let api = try client()
        let before = try await api.eventWatermark()
        let restored = try await api.eventWatermark()
        XCTAssertEqual(before, 9_007_199_254_740_993)
        XCTAssertEqual(restored, 7)
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
