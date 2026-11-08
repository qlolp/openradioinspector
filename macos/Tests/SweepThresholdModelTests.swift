import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepThresholdModelTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        private let lock = NSLock()
        private var stopped = false
        var isStopped: Bool { lock.withLock { stopped } }
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { lock.withLock { stopped = true } }
        func respond(_ object: Any, status: Int = 200) {
            guard !isStopped else { return }
            let payload: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": status == 409 ? "threshold_incompatible" : "sweep_not_found", "message": "Synthetic failure"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    final class Responses {
        private let lock = NSLock()
        private var requests: [Stub] = []
        private var hold = false
        private var status = 200
        private var wrongStream = false
        private var malformed = false
        private var stream = "tiny"
        private var sweepID = "w"
        var requestCount: Int { lock.withLock { requests.count } }
        var stoppedCount: Int { lock.withLock { requests.filter(\.isStopped).count } }
        var lastURL: URL? { lock.withLock { requests.last?.request.url } }

        func configure(hold: Bool = false, status: Int = 200, wrongStream: Bool = false,
                       malformed: Bool = false, stream: String = "tiny", sweepID: String = "w") {
            lock.withLock {
                self.hold = hold; self.status = status; self.wrongStream = wrongStream
                self.malformed = malformed; self.stream = stream; self.sweepID = sweepID
            }
        }
        func handle(_ stub: Stub) {
            let config = lock.withLock { (hold, status, wrongStream, malformed, stream, sweepID) }
            let path = stub.request.url!.path
            if path.hasSuffix("/sweep-threshold") {
                lock.withLock { requests.append(stub) }
                if !config.0 { stub.respond(config.3 ? [:] : result(stub, stream: config.2 ? "other" : config.4), status: config.1) }
            } else if path == "/api/v1/sessions" { stub.respond([Self.session("s", stream: config.4)]) }
            else if path == "/api/v1/sessions/s" || path == "/api/v1/sessions/s2" {
                stub.respond(Self.session(path.hasSuffix("s2") ? "s2" : "s", stream: config.4))
            } else if path.hasSuffix("/sweeps") { stub.respond([Self.sweep(config.5, stream: config.4)]) }
            else if path.hasSuffix("/workers") { stub.respond([]) }
            else { stub.respond([:], status: 404) }
        }
        func release() {
            for stub in lock.withLock({ requests }) where !stub.isStopped { stub.respond(result(stub, stream: "tiny")) }
        }
        private func result(_ stub: Stub, stream: String) -> [String: Any] {
            let query = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            func value(_ name: String) -> String { query.first(where: { $0.name == name })!.value! }
            return Self.result(sweepID: value("sweep_id"), stream: stream,
                window: Int(value("window_points"))!, margin: Double(value("margin_db"))!)
        }
        static func session(_ id: String, stream: String) -> [String: Any] {
            ["id": id, "name": "Synthetic", "state": "running", "streams": [["id": stream, "device": "tinySA Ultra",
             "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
        }
        static func sweep(_ id: String = "w", stream: String = "tiny") -> [String: Any] {
            ["id": id, "session_id": "s", "stream_id": stream,
             "interval": ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"],
             "frequencies_hz": [100, 200, 300, 400, 500], "levels": [-90, -90, -60, -90, -90],
             "level_unit": "dBm", "settings": ["rbw_hz": 1000]]
        }
        static func result(sweepID: String = "w", stream: String = "tiny", window: Int = 21, margin: Double = 6) -> [String: Any] {
            ["session_id": "s", "stream_id": stream, "sweep": sweep(sweepID, stream: stream),
             "frequencies_hz": [100, 200, 300, 400, 500], "parameters": ["method": "rolling_median",
             "window_points": window, "margin_db": margin, "edge_policy": "clip", "comparison": "strictly_greater"],
             "baseline_levels": Array(repeating: -90, count: 5), "threshold_levels": Array(repeating: -90 + margin, count: 5),
             "excess_levels": [-margin, -margin, 30 - margin, -margin, -margin], "level_unit": "dBm", "delta_unit": "dB",
             "points": 5, "exceedance_points": 1, "region_count": 1,
             "regions": [["start_index": 2, "end_index": 2, "start_frequency_hz": 300, "end_frequency_hz": 300,
                          "peak_index": 2, "peak_frequency_hz": 300, "peak_excess_db": 30 - margin, "points": 1]]]
        }
    }

    @MainActor final class DeferredResults {
        var pending: [CheckedContinuation<PlatformSweepThreshold, Error>] = []
        func fetch() async throws -> PlatformSweepThreshold {
            try await withCheckedThrowingContinuation { pending.append($0) }
        }
        func release(_ value: PlatformSweepThreshold, at index: Int = 0) { pending.remove(at: index).resume(returning: value) }
    }

    private func decode<T: Decodable>(_ object: Any, as: T.Type) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    private func client(_ base: String = "https://example.test") throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: base, session: URLSession(configuration: config))
    }
    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Timed out waiting for threshold state")
    }
    @MainActor private func model(_ responses: Responses, timeout: UInt64 = 6_000_000_000) throws -> SweepThresholdModel {
        Stub.handler = { responses.handle($0) }
        let model = SweepThresholdModel(timeoutNanoseconds: timeout)
        model.configure(client: try client(), scope: .init(server: "https://example.test", session: "s", stream: "tiny"),
            source: try decode(Responses.sweep(), as: PlatformSweep.self))
        return model
    }
    @MainActor private func integrated(_ responses: Responses) async throws -> RemoteDualModel {
        Stub.handler = { responses.handle($0) }
        let model = RemoteDualModel(makeClient: { try self.client($0) })
        model.connect(startPolling: false)
        try await waitFor { model.sweepThreshold.source != nil }
        return model
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }

    @MainActor func testDefaultsAndInvalidInputsAreExplicitOnly() async throws {
        let responses = Responses(); let model = try model(responses)
        defer { model.reset() }
        XCTAssertEqual(model.windowPoints, "21"); XCTAssertEqual(model.marginDb, "6")
        XCTAssertEqual(responses.requestCount, 0)
        for (window, margin) in [("2", "6"), ("4", "6"), ("103", "6"), ("21.0", "6"),
                                ("21", "NaN"), ("21", "inf"), ("21", "-1"), ("21", "121"), ("", "")] {
            model.windowPoints = window; model.marginDb = margin; model.analyze()
            XCTAssertNotNil(model.errorText); XCTAssertFalse(model.pending)
        }
        XCTAssertEqual(responses.requestCount, 0)
        model.windowPoints = "3"; model.marginDb = "7,5"; model.analyze()
        try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.parameters.marginDb, 7.5)
    }

    @MainActor func testPollingDoesNotAnalyzeOrMoveFrozenResultAndRawComparisonUntouched() async throws {
        let responses = Responses(); let remote = try await integrated(responses)
        defer { remote.disconnect() }
        let model = remote.sweepThreshold
        XCTAssertEqual(responses.requestCount, 0)
        let raw = remote.current; let revision = remote.waterfallRevision; let error = remote.errorText
        remote.comparisonSelectionID = "w"; remote.pinComparisonReference()
        model.analyze()
        try await waitFor { model.result != nil }
        XCTAssertEqual(remote.current, raw); XCTAssertEqual(remote.waterfallRevision, revision)
        XCTAssertEqual(remote.comparisonReference?.id, "w"); XCTAssertEqual(remote.errorText, error)
        model.windowPoints = "3"; model.marginDb = "8"
        responses.configure(sweepID: "new")
        await remote.refresh()
        XCTAssertEqual(model.source?.id, "new"); XCTAssertEqual(model.result?.sweep.id, "w")
        XCTAssertEqual(model.result?.parameters.windowPoints, 21); XCTAssertEqual(model.result?.parameters.marginDb, 6)
        XCTAssertEqual(responses.requestCount, 1)
        model.analyze(); try await waitFor { model.result?.sweep.id == "new" }
        XCTAssertEqual(model.result?.parameters.windowPoints, 3); XCTAssertEqual(model.result?.parameters.marginDb, 8)
    }

    @MainActor func testIsolated404409MalformedWrongStreamAndRetry() async throws {
        let responses = Responses(); let remote = try await integrated(responses)
        defer { remote.disconnect() }
        let model = remote.sweepThreshold; let raw = remote.current; let error = remote.errorText
        for kind in ["404", "409", "malformed", "stream"] {
            responses.configure(status: Int(kind) ?? 200, wrongStream: kind == "stream", malformed: kind == "malformed")
            model.analyze(); try await waitFor { !model.pending }
            XCTAssertNil(model.result); XCTAssertNotNil(model.errorText)
            XCTAssertEqual(remote.current, raw); XCTAssertEqual(remote.errorText, error)
        }
        responses.configure(); model.analyze(); try await waitFor { model.result != nil }
        XCTAssertNil(model.errorText)
    }

    @MainActor func testRepeatedActionsCancelPriorTransportAndCancelIsRecoverable() async throws {
        let responses = Responses(); let model = try model(responses)
        defer { model.reset() }
        responses.configure(hold: true)
        model.analyze(); try await waitFor { responses.requestCount == 1 }
        model.analyze(); try await waitFor { responses.requestCount == 2 && responses.stoppedCount == 1 }
        model.cancel(); try await waitFor { responses.stoppedCount == 2 }
        XCTAssertNil(model.result); XCTAssertFalse(model.pending); XCTAssertNil(model.errorText)
        responses.configure(); model.analyze(); try await waitFor { model.result != nil }
        XCTAssertEqual(responses.requestCount, 3)
    }

    @MainActor func testTimeoutCancelsTransportAndAllowsRetry() async throws {
        let responses = Responses(); let model = try model(responses, timeout: 30_000_000)
        defer { model.reset() }
        responses.configure(hold: true); model.analyze()
        try await waitFor { responses.requestCount == 1 }
        try await waitFor { !model.pending && responses.stoppedCount == 1 }
        XCTAssertTrue(model.errorText?.contains("истекло") == true)
        responses.configure(); model.analyze(); try await waitFor { model.result != nil }
        XCTAssertNil(model.errorText)
    }

    @MainActor func testSessionStreamServerDisconnectAndWindowCloseResetPending() async throws {
        for change in ["session", "stream", "server", "disconnect", "close"] {
            let responses = Responses(); let remote = try await integrated(responses)
            let model = remote.sweepThreshold
            responses.configure(hold: true); model.analyze(); try await waitFor { responses.requestCount == 1 }
            switch change {
            case "session": remote.selectSession("s2")
            case "stream": responses.configure(hold: true, stream: "other"); await remote.refresh()
            case "server": remote.serverURL = "https://other.example.test"
            default: remote.disconnect() // WindowCloseObserver calls exactly this operation.
            }
            XCTAssertNil(model.result, change); XCTAssertFalse(model.pending, change)
            try await waitFor { responses.stoppedCount == 1 }
            remote.disconnect()
        }
    }

    @MainActor func testNoncooperativeLateResultsCannotRepopulateAfterCancelResetOrNewRequest() async throws {
        let deferred = DeferredResults()
        let model = SweepThresholdModel(fetch: { _, _, _, _, _ in try await deferred.fetch() })
        let api = try client()
        let scope = SweepThresholdModel.Scope(server: "https://example.test", session: "s", stream: "tiny")
        let source = try decode(Responses.sweep(), as: PlatformSweep.self)
        let result = try decode(Responses.result(), as: PlatformSweepThreshold.self)
        model.configure(client: api, scope: scope, source: source)
        model.analyze(); try await waitFor { deferred.pending.count == 1 }
        model.cancel(); deferred.release(result)
        await Task.yield(); XCTAssertNil(model.result); XCTAssertFalse(model.pending)
        model.analyze(); try await waitFor { deferred.pending.count == 1 }
        model.reset(); deferred.release(result)
        await Task.yield(); XCTAssertNil(model.result); XCTAssertFalse(model.pending)
        model.configure(client: api, scope: scope, source: source)
        model.analyze(); try await waitFor { deferred.pending.count == 1 }
        model.marginDb = "7"; model.analyze(); try await waitFor { deferred.pending.count == 2 }
        let newer = try decode(Responses.result(margin: 7), as: PlatformSweepThreshold.self)
        deferred.release(newer, at: 1); try await waitFor { model.result != nil }
        deferred.release(result); await Task.yield()
        XCTAssertEqual(model.result?.parameters.marginDb, 7)
        model.reset()
    }

    @MainActor func testLateResponseAfterTimeoutIsIgnored() async throws {
        let deferred = DeferredResults()
        let model = SweepThresholdModel(timeoutNanoseconds: 20_000_000, fetch: { _, _, _, _, _ in try await deferred.fetch() })
        model.configure(client: try client(), scope: .init(server: "local", session: "s", stream: "tiny"),
            source: try decode(Responses.sweep(), as: PlatformSweep.self))
        model.analyze(); try await waitFor { !model.pending }
        XCTAssertTrue(model.errorText?.contains("истекло") == true)
        deferred.release(try decode(Responses.result(), as: PlatformSweepThreshold.self))
        await Task.yield(); XCTAssertNil(model.result)
        XCTAssertTrue(model.errorText?.contains("истекло") == true)
        model.reset()
    }

    @MainActor func testSnapshotUpdateDoesNotRestartPendingDeadlineOrReplaceFrozenSource() async throws {
        let responses = Responses()
        Stub.handler = { responses.handle($0) }
        let model = SweepThresholdModel(timeoutNanoseconds: 80_000_000)
        let api = try client()
        let scope = SweepThresholdModel.Scope(server: "local", session: "s", stream: "tiny")
        model.configure(client: api, scope: scope, source: try decode(Responses.sweep(), as: PlatformSweep.self))
        responses.configure(hold: true); model.analyze()
        try await waitFor { responses.requestCount == 1 }
        model.configure(client: api, scope: scope, source: try decode(Responses.sweep("new"), as: PlatformSweep.self))
        XCTAssertTrue(model.pending)
        XCTAssertEqual(responses.requestCount, 1)
        XCTAssertTrue(responses.lastURL?.absoluteString.contains("sweep_id=w&") == true)
        try await waitFor { !model.pending && responses.stoppedCount == 1 }
        XCTAssertTrue(model.errorText?.contains("истекло") == true)
        model.reset()
    }

    @MainActor func testNoncooperativeLateResponsesAfterEveryScopeChangeAreIgnored() async throws {
        for changed in ["server", "session", "stream", "client"] {
            let deferred = DeferredResults()
            let model = SweepThresholdModel(fetch: { _, _, _, _, _ in try await deferred.fetch() })
            let api = try client()
            let oldScope = SweepThresholdModel.Scope(server: "local", session: "s", stream: "tiny")
            let source = try decode(Responses.sweep(), as: PlatformSweep.self)
            model.configure(client: api, scope: oldScope, source: source)
            model.analyze(); try await waitFor { deferred.pending.count == 1 }
            let scope = SweepThresholdModel.Scope(server: changed == "server" ? "other" : "local",
                session: changed == "session" ? "other" : "s", stream: changed == "stream" ? "other" : "tiny")
            model.configure(client: changed == "client" ? try client() : api, scope: scope, source: source)
            deferred.release(try decode(Responses.result(), as: PlatformSweepThreshold.self))
            await Task.yield()
            XCTAssertNil(model.result, changed); XCTAssertFalse(model.pending, changed)
            model.reset()
        }
    }

    @MainActor func testWrongSourceScopeCannotBeAnalyzed() throws {
        let model = SweepThresholdModel()
        for stream in ["tiny", "other"] {
            model.configure(client: try client(), scope: .init(server: "local", session: "other", stream: stream),
                source: try decode(Responses.sweep(), as: PlatformSweep.self))
            XCTAssertFalse(model.canAnalyze); XCTAssertNil(model.source)
        }
    }
}
