import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepComparisonModelTests: XCTestCase {
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
            let payload: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": status == 409 ? "comparison_incompatible" : "sweep_not_found", "message": "Test failure"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    final class Responses {
        private let lock = NSLock()
        private var ids = ["r", "t"]
        private var stream = "tiny"
        private var hold = false
        private var status = 200
        private var malformed = false
        private var wrongStream = false
        private var watermark: Int64 = 100
        private var held: [Stub] = []
        private var comparisons: [(String, String)] = []
        var requestCount: Int { lock.withLock { comparisons.count } }
        var pendingCount: Int { lock.withLock { held.count } }
        var stoppedCount: Int { lock.withLock { held.filter(\.isStopped).count } }
        var lastPair: (String, String)? { lock.withLock { comparisons.last } }
        func restoreArchive() { lock.withLock { watermark = 7; ids = ["restored"] } }

        func configure(ids: [String]? = nil, stream: String? = nil, hold: Bool = false,
                       status: Int = 200, malformed: Bool = false, wrongStream: Bool = false) {
            lock.withLock {
                if let ids { self.ids = ids }
                if let stream { self.stream = stream }
                self.hold = hold; self.status = status; self.malformed = malformed; self.wrongStream = wrongStream
            }
        }

        private func interval() -> [String: Any] {
            ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"]
        }
        private func session(_ id: String, stream: String) -> [String: Any] {
            ["id": id, "name": "Synthetic", "state": "running", "streams": [["id": stream, "device": "tinySA Ultra",
                "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
        }
        private func sweep(_ id: String, stream: String) -> [String: Any] {
            ["id": id, "session_id": "s", "stream_id": stream, "interval": interval(), "frequencies_hz": [100, 200],
             "levels": id == "r" ? [-90, -60] : [-80, -80], "level_unit": "dBm", "settings": ["rbw_hz": 1000]]
        }
        private func comparison(_ stub: Stub, stream: String) -> [String: Any] {
            let query = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            let reference = query.first(where: { $0.name == "reference_id" })!.value!
            let target = query.first(where: { $0.name == "target_id" })!.value!
            let delta = reference == target ? [0, 0] : [10, -20]
            return ["session_id": "s", "stream_id": stream, "reference": sweep(reference, stream: stream),
                    "target": sweep(target, stream: stream), "frequencies_hz": [100, 200], "delta_levels": delta,
                    "delta_unit": "dB", "points": 2, "max_increase": ["frequency_hz": 100, "delta_db": delta[0]],
                    "min_change": ["frequency_hz": reference == target ? 100 : 200, "delta_db": delta[1]]]
        }

        func release() {
            let pending = lock.withLock { let previous = held; held = []; return previous }
            for stub in pending { stub.respond(comparison(stub, stream: "tiny")) }
        }

        func handle(_ stub: Stub) {
            let path = stub.request.url!.path
            let config = lock.withLock { (ids, stream, hold, status, malformed, wrongStream) }
            if path.hasSuffix("/sweep-comparison") {
                let query = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
                lock.withLock {
                    comparisons.append((query[0].value!, query[1].value!))
                    if config.2 { held.append(stub) }
                }
                if !config.2 {
                    stub.respond(config.4 ? [:] : comparison(stub, stream: config.5 ? "foreign" : config.1), status: config.3)
                }
            } else if path == "/api/v1/health" { stub.respond(["last_event_id": lock.withLock { watermark }]) }
            else if path == "/api/v1/sessions" { stub.respond([session("s", stream: config.1)]) }
            else if path == "/api/v1/sessions/s" || path == "/api/v1/sessions/s2" {
                stub.respond(session(path.hasSuffix("s2") ? "s2" : "s", stream: config.1))
            } else if path.hasSuffix("/sweeps") { stub.respond(config.0.map { sweep($0, stream: config.1) }) }
            else if path.hasSuffix("/workers") { stub.respond([]) }
            else if path == "/api/v1/archive/capacity" {
                stub.respond(["storage_mode": "sqlite", "database_bytes": 1000, "max_database_bytes": NSNull(),
                    "remaining_bytes": NSNull(), "state": "unlimited", "warning_threshold": 0.9,
                    "measurement_writes_blocked": false, "automatic_deletion": false])
            } else { stub.respond([:], status: 404) }
        }
    }

    @MainActor private func model(_ responses: Responses, timeout: UInt64 = 6_000_000_000) async throws -> RemoteDualModel {
        Stub.handler = { responses.handle($0) }
        let model = RemoteDualModel(makeClient: { baseURL in
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: baseURL, session: URLSession(configuration: configuration))
        }, comparisonTimeoutNanoseconds: timeout)
        model.connect(startPolling: false)
        try await waitFor { model.comparisonSweeps.count == 2 }
        return model
    }

    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<300 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Timed out waiting for comparison state")
    }

    @MainActor private func pin(_ model: RemoteDualModel, _ id: String = "r") {
        model.comparisonSelectionID = id
        model.pinComparisonReference()
        XCTAssertEqual(model.comparisonReference?.id, id)
    }

    @MainActor func testPinPersistsOutsideRecentSnapshotAndComparisonDoesNotPollOrAlterRaw() async throws {
        let responses = Responses()
        let model = try await model(responses)
        defer { model.disconnect() }
        XCTAssertEqual(responses.requestCount, 0)
        pin(model)
        XCTAssertEqual(responses.requestCount, 0)
        responses.configure(ids: ["new"])
        await model.refresh()
        XCTAssertEqual(model.comparisonReference?.id, "r")
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["new"])
        let raw = model.current
        let revision = model.waterfallRevision
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil }
        XCTAssertEqual(responses.lastPair?.0, "r")
        XCTAssertEqual(responses.lastPair?.1, "new")
        XCTAssertEqual(model.sweepComparison?.deltaLevels, [10, -20])
        XCTAssertEqual(model.current, raw)
        XCTAssertEqual(model.waterfallRevision, revision)
        responses.configure(ids: ["newer"])
        await model.refresh()
        XCTAssertEqual(responses.requestCount, 1)
        XCTAssertEqual(model.sweepComparison?.target.id, "new")
        XCTAssertEqual(model.comparisonReference?.id, "r")
    }

    @MainActor func testFailuresAreIsolatedAndRecoverableIncludingWrongResponseStream() async throws {
        let responses = Responses()
        let model = try await model(responses)
        defer { model.disconnect() }
        pin(model)
        let raw = model.current
        let revision = model.waterfallRevision
        let previousError = model.errorText
        for kind in ["404", "409", "malformed", "wrongStream"] {
            responses.configure(status: Int(kind) ?? 200, malformed: kind == "malformed", wrongStream: kind == "wrongStream")
            model.compareLatestSweep()
            try await waitFor { !model.sweepComparisonPending }
            XCTAssertNil(model.sweepComparison)
            XCTAssertNotNil(model.sweepComparisonError)
            XCTAssertEqual(model.current, raw)
            XCTAssertEqual(model.waterfallRevision, revision)
            XCTAssertEqual(model.errorText, previousError)
            XCTAssertEqual(model.comparisonReference?.id, "r")
        }
        responses.configure()
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil }
        XCTAssertNil(model.sweepComparisonError)
    }

    @MainActor func testRepeatedComparisonCancelsOlderRequestAndClearRejectsLateResults() async throws {
        let responses = Responses()
        let model = try await model(responses)
        defer { model.disconnect() }
        pin(model)
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 2 && responses.stoppedCount == 1 }
        responses.configure()
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil && responses.stoppedCount == 2 }
        XCTAssertEqual(responses.requestCount, 3)
        responses.release()
        XCTAssertEqual(model.sweepComparison?.target.id, "t")
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        model.clearSweepComparison()
        XCTAssertNil(model.comparisonReference)
        XCTAssertNil(model.sweepComparison)
        XCTAssertFalse(model.sweepComparisonPending)
        try await waitFor { responses.stoppedCount == 1 }
        responses.release()
        XCTAssertNil(model.sweepComparison)
    }

    @MainActor func testCancelKeepsReferenceAndSameIDComparisonIsAllowed() async throws {
        let responses = Responses()
        let model = try await model(responses)
        defer { model.disconnect() }
        pin(model, "t")
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        model.cancelSweepComparison()
        XCTAssertEqual(model.comparisonReference?.id, "t")
        XCTAssertFalse(model.sweepComparisonPending)
        XCTAssertNil(model.sweepComparisonError)
        responses.configure()
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil }
        XCTAssertEqual(model.sweepComparison?.deltaLevels, [0, 0])
        responses.release()
    }

    @MainActor func testTimeoutFinishesPendingDespiteSnapshotRefreshAndAllowsRetry() async throws {
        let responses = Responses()
        let model = try await model(responses, timeout: 500_000_000)
        defer { model.disconnect() }
        pin(model)
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        await model.refresh()
        try await waitFor { !model.sweepComparisonPending && responses.stoppedCount == 1 }
        XCTAssertTrue(model.sweepComparisonError?.contains("истекло") == true)
        XCTAssertEqual(model.comparisonReference?.id, "r")
        responses.configure()
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil }
        responses.release()
        XCTAssertNil(model.sweepComparisonError)
    }

    func testSignedChartScaleKeepsExtremeFiniteCoordinatesAndZeroBaseline() {
        let scale = TraceChartVerticalScale(minimum: -1e308, maximum: 1e308)
        XCTAssertTrue(scale.fraction(-1e308).isFinite)
        XCTAssertTrue(scale.fraction(1e308).isFinite)
        XCTAssertLessThan(scale.fraction(-1e308), scale.fraction(0))
        XCTAssertGreaterThan(scale.fraction(1e308), scale.fraction(0))
        XCTAssertEqual(scale.fraction(0), 0.5, accuracy: 0.000001)
        for value in [-1e308, 0, 1e308] {
            let flat = TraceChartVerticalScale(minimum: value, maximum: value)
            XCTAssertEqual(flat.fraction(value), 0.5, accuracy: 0.000001)
        }
    }

    @MainActor func testRepinningCancelsPreviousComparisonAndRejectsUnknownSweep() async throws {
        let responses = Responses()
        let model = try await model(responses)
        defer { model.disconnect() }
        model.comparisonSelectionID = "not-in-snapshot"
        XCTAssertFalse(model.canPinComparisonReference)
        model.pinComparisonReference()
        XCTAssertNil(model.comparisonReference)
        pin(model)
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        pin(model, "t")
        XCTAssertFalse(model.sweepComparisonPending)
        try await waitFor { responses.stoppedCount == 1 }
        responses.release()
        XCTAssertNil(model.sweepComparison)
        XCTAssertEqual(model.comparisonReference?.id, "t")
    }

    @MainActor func testSameIDArchiveRollbackCancelsComparisonAndRejectsItsLateAnswer() async throws {
        let responses = Responses()
        let model = try await self.model(responses)
        defer { responses.release(); model.disconnect() }
        pin(model)
        responses.configure(hold: true)
        model.compareLatestSweep()
        try await waitFor { responses.pendingCount == 1 }
        responses.restoreArchive()
        await model.refresh()
        XCTAssertNil(model.comparisonReference)
        XCTAssertNil(model.sweepComparison)
        XCTAssertFalse(model.sweepComparisonPending)
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["restored"])
        try await waitFor { responses.stoppedCount == 1 }
        responses.release()
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertNil(model.sweepComparison)
        XCTAssertNil(model.sweepComparisonError)
    }

    @MainActor func testScopeChangesAndWindowDisconnectCancelAndClearReference() async throws {
        for change in ["session", "stream", "server", "disconnect"] {
            let responses = Responses()
            let model = try await model(responses)
            pin(model)
            responses.configure(hold: true)
            model.compareLatestSweep()
            try await waitFor { responses.pendingCount == 1 }
            switch change {
            case "session": model.selectSession("s2")
            case "stream": responses.configure(stream: "tiny2", hold: true); await model.refresh()
            case "server": model.serverURL = "https://other.example.test"
            default: model.disconnect() // WindowCloseObserver calls this same path.
            }
            XCTAssertNil(model.comparisonReference, change)
            XCTAssertNil(model.sweepComparison, change)
            XCTAssertFalse(model.sweepComparisonPending, change)
            try await waitFor { responses.stoppedCount == 1 }
            responses.release()
            XCTAssertNil(model.sweepComparison, change)
            model.disconnect()
        }
    }
}
