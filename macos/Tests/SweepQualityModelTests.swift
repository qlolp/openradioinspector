import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepQualityModelTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "not_found", "message": "Synthetic unavailable endpoint"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    final class Responses {
        private let lock = NSLock()
        private let report: [String: Any]
        private var sweepID = "w"
        private var snapshotFails = false
        private var holdQuality = false
        private var pending: [(Stub, [String: Any])] = []
        private var qualityRequests = 0
        init(report: [String: Any]) { self.report = report }
        var pendingCount: Int { lock.withLock { pending.count } }
        var qualityRequestCount: Int { lock.withLock { qualityRequests } }
        func configure(sweepID: String = "w", snapshotFails: Bool = false, holdQuality: Bool = false) {
            lock.withLock {
                self.sweepID = sweepID; self.snapshotFails = snapshotFails; self.holdQuality = holdQuality
            }
        }
        func release() {
            let held = lock.withLock { let held = pending; pending = []; return held }
            for (stub, response) in held { stub.respond(response) }
        }
        func handle(_ stub: Stub) {
            let state = lock.withLock { (sweepID, snapshotFails) }
            let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [
                ["id": "tiny", "device": "tinySA Ultra", "role": "spectrum_analyzer", "level_unit": "dBm",
                 "state": "connected", "capabilities": ["sweep"]]]]
            switch stub.request.url!.path {
            case "/api/v1/sessions": stub.respond([session])
            case "/api/v1/sessions/s":
                if state.1 { stub.client?.urlProtocol(stub, didFailWithError: URLError(.notConnectedToInternet)) }
                else { stub.respond(session) }
            case "/api/v1/sessions/s/sweeps":
                var sweep = report["sweep"] as! [String: Any]
                sweep["id"] = state.0; sweep["session_id"] = "s"; sweep["stream_id"] = "tiny"
                sweep["frequencies_hz"] = [100, 200, 300]; sweep["levels"] = [0, -20, -30]
                stub.respond([sweep])
            case "/api/v1/sessions/s/workers": stub.respond([])
            case "/api/v1/sessions/s/sweep-quality":
                let response: [String: Any]? = lock.withLock {
                    qualityRequests += 1
                    var value = report, sweep = report["sweep"] as! [String: Any]
                    sweep["id"] = sweepID; value["sweep"] = sweep
                    if holdQuality { pending.append((stub, value)); return nil }
                    return value
                }
                if let response { stub.respond(response) }
            default: stub.respond([:], status: 404)
            }
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }

    func fixture() -> [String: Any] {
        ["session_id": "s", "stream_id": "tiny", "sweep": ["id": "w",
            "interval": ["started_at": "2026-11-09T01:00:00Z", "ended_at": "2026-11-09T01:00:01Z"],
            "level_unit": "dBm", "settings": ["unknown": ["nullable": NSNull(), "enabled": true]]],
         "parameters": ["edge_margin_db": 20, "high_level_dbm": 0], "points": 3,
         "peak_index": 0, "peak_frequency_hz": 100, "peak_level_dbm": 0, "high_level_count": 1,
         "edge_assessments": [
            ["edge": "first", "index": 0, "frequency_hz": 100, "level_dbm": 0, "neighbor_index": 1, "neighbor_level_dbm": -20, "delta_db": 20, "flagged": true],
            ["edge": "last", "index": 2, "frequency_hz": 300, "level_dbm": -30, "neighbor_index": 1, "neighbor_level_dbm": -20, "delta_db": -10, "flagged": false]],
         "flags": ["first_edge_jump", "level_threshold"], "assessment": "review", "method": "archived_edge_level_review_v1", "hardware_diagnosis": false, "raw_modified": false]
    }
    func decode<T: Decodable>(_ object: Any, as: T.Type) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func source() throws -> PlatformSweep {
        var sweep = fixture()["sweep"] as! [String: Any]
        sweep["session_id"] = "s"; sweep["stream_id"] = "tiny"
        sweep["levels"] = [0, -20, -30]; sweep["frequencies_hz"] = [100, 200, 300]
        return try decode(sweep, as: PlatformSweep.self)
    }

    @MainActor final class Deferred {
        var pending: [CheckedContinuation<PlatformSweepQuality, Error>] = []
        func fetch() async throws -> PlatformSweepQuality {
            try await withCheckedThrowingContinuation { pending.append($0) }
        }
        func release(_ value: PlatformSweepQuality) { pending.removeFirst().resume(returning: value) }
    }
    @MainActor func waitFor(_ predicate: @escaping () -> Bool) async throws {
        for _ in 0..<400 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out")
    }
    @MainActor func testEditingCancelsAndSuppressesLateResponse() async throws {
        let deferred = Deferred()
        let model = SweepQualityModel(fetch: { _, _, _, _, _ in try await deferred.fetch() })
        let client = try PlatformClient(baseURL: "https://example.test")
        model.configure(client: client, scope: .init(server: "test", session: "s", stream: "tiny"), sweeps: [try source()])
        model.analyze(); try await waitFor { deferred.pending.count == 1 }
        model.edgeText = "30"
        XCTAssertFalse(model.pending); XCTAssertNil(model.result)
        deferred.release(try decode(fixture(), as: PlatformSweepQuality.self))
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertNil(model.result); XCTAssertNil(model.errorText)
    }
    @MainActor func testResultThenSelectionClears() async throws {
        let value = try decode(fixture(), as: PlatformSweepQuality.self)
        let model = SweepQualityModel(fetch: { _, _, _, _, _ in value })
        model.configure(client: try PlatformClient(baseURL: "https://example.test"),
            scope: .init(server: "test", session: "s", stream: "tiny"), sweeps: [try source()])
        model.analyze(); try await waitFor { !model.pending }
        XCTAssertNotNil(model.result); XCTAssertNil(model.errorText)
        model.select(id: "w"); XCTAssertNil(model.result)
        model.highText = "nan"; model.analyze()
        XCTAssertNotNil(model.errorText); XCTAssertFalse(model.pending)
    }
    @MainActor func testScopeChangeAndTimeoutSuppressLateResults() async throws {
        for timeout in [false, true] {
            let deferred = Deferred()
            let model = SweepQualityModel(timeoutNanoseconds: timeout ? 10_000_000 : 6_000_000_000,
                fetch: { _, _, _, _, _ in try await deferred.fetch() })
            let client = try PlatformClient(baseURL: "https://example.test")
            model.configure(client: client, scope: .init(server: "a", session: "s", stream: "tiny"), sweeps: [try source()])
            model.analyze(); try await waitFor { deferred.pending.count == 1 }
            if timeout { try await waitFor { !model.pending }; XCTAssertNotNil(model.errorText) }
            else { model.reset() }
            deferred.release(try decode(fixture(), as: PlatformSweepQuality.self))
            try await Task.sleep(nanoseconds: 20_000_000); XCTAssertNil(model.result)
        }
    }
    @MainActor func testRemoteSnapshotFailureInvalidatesRetainedAndPendingQualityAndRecoversFreshSource() async throws {
        let responses = Responses(report: fixture())
        Stub.handler = { responses.handle($0) }
        let remote = RemoteDualModel(makeClient: { baseURL in
            let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: baseURL, session: URLSession(configuration: config))
        })
        remote.connect(startPolling: false); defer { remote.disconnect() }
        let model = remote.sweepQuality
        try await waitFor { model.source != nil }
        XCTAssertEqual(model.source?.id, "w"); XCTAssertTrue(model.canAnalyze)
        model.analyze(); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.sweep.id, "w")

        responses.configure(snapshotFails: true)
        await remote.refresh()
        XCTAssertNotNil(remote.errorText)
        XCTAssertNil(model.result); XCTAssertNil(model.source); XCTAssertTrue(model.choices.isEmpty)
        XCTAssertFalse(model.canAnalyze); XCTAssertFalse(model.pending)

        responses.configure(sweepID: "fresh")
        await remote.refresh()
        XCTAssertEqual(model.source?.id, "fresh"); XCTAssertEqual(model.choices.map(\.id), ["fresh"])
        XCTAssertTrue(model.canAnalyze); XCTAssertNil(model.result)
        responses.configure(sweepID: "fresh", holdQuality: true)
        model.analyze(); try await waitFor { responses.pendingCount == 1 }
        XCTAssertTrue(model.pending)

        responses.configure(sweepID: "fresh", snapshotFails: true)
        await remote.refresh()
        XCTAssertNil(model.result); XCTAssertNil(model.source); XCTAssertTrue(model.choices.isEmpty)
        XCTAssertFalse(model.canAnalyze); XCTAssertFalse(model.pending)
        let requests = responses.qualityRequestCount
        model.analyze(); await Task.yield()
        XCTAssertEqual(responses.qualityRequestCount, requests)

        responses.configure(sweepID: "newest")
        await remote.refresh()
        XCTAssertEqual(model.source?.id, "newest"); XCTAssertEqual(model.choices.map(\.id), ["newest"])
        XCTAssertTrue(model.canAnalyze); XCTAssertNil(model.result)
        // Deliver the old response after recovery, even if its transport was cancelled.
        responses.release()
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertNil(model.result); XCTAssertNil(model.errorText); XCTAssertFalse(model.pending)
        XCTAssertEqual(model.source?.id, "newest"); XCTAssertTrue(model.canAnalyze)
    }
}
