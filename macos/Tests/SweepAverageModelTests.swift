import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepAverageModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [([String], String)] = []
        var continuations: [CheckedContinuation<PlatformSweepAverageChangeDetection, Error>] = []
        func fetch(_ refs: [String], _ target: String, _ threshold: Double, _ bins: Int) async throws -> PlatformSweepAverageChangeDetection {
            requests.append((refs, target))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ result: PlatformSweepAverageChangeDetection) { continuations.removeFirst().resume(returning: result) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200 ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "average_reference_incompatible", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    private func raw(_ n: Int) -> [String: Any] {
        ["id": "w\(n)", "session_id": "s", "stream_id": "tiny", "interval": ["started_at": "2026-11-08T01:00:0\(n)Z", "ended_at": "2026-11-08T01:00:0\(n).000001Z"],
         "settings": ["rbw_hz": 1000, "sequence": n], "frequencies_hz": [100, 100, 200],
         "levels": n <= 3 ? [-90, -90, -90] : [-80, -100, -80], "level_unit": "dBm"]
    }
    private func sweeps(_ indices: ClosedRange<Int> = 1...4) throws -> [PlatformSweep] { try indices.map { try decode(raw($0)) } }
    private func body(target: Int = 4) -> [String: Any] {
        ["session_id": "s", "stream_id": "tiny", "frequencies_hz": [100, 100, 200],
         "reference": ["method": "mean_power", "count": 3, "source_ids": ["w1", "w2", "w3"],
                       "sources": (1...3).map { raw($0).filter { ["id", "interval", "settings"].contains($0.key) } },
                       "interval": ["started_at": "2026-11-08T01:00:01Z", "ended_at": "2026-11-08T01:00:03.000001Z"],
                       "levels": [-90, -90, -90], "level_unit": "dBm"],
         "target": raw(target), "delta_levels": [10, -10, 10], "delta_unit": "dB", "points": 3,
         "max_increase": ["frequency_hz": 100, "delta_db": 10], "min_change": ["frequency_hz": 100, "delta_db": -10]]
    }
    private func detectionBody(_ comparison: [String: Any]? = nil) -> [String: Any] {
        ["comparison": comparison ?? body(),
         "detection": ["minimum_increase_db": 6.0, "minimum_bins": 2, "regions": [], "count": 0]]
    }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for average state")
    }
    @MainActor private var scope: SweepAverageModel.Scope { .init(server: "local", session: "s", stream: "tiny") }

    @MainActor func testDefaultPinRequiresNPlusOneAndNeverShrinksOrCallsNetwork() throws {
        Stub.handler = { _ in XCTFail("Pin must be local") }
        let model = SweepAverageModel(), client = try api()
        XCTAssertEqual(model.countText, "3")
        model.configure(client: client, scope: scope, sweeps: try sweeps(1...3)); model.pin()
        XCTAssertTrue(model.sources.isEmpty); XCTAssertNotNil(model.errorText)
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin()
        XCTAssertEqual(model.sources.map(\.id), ["w1", "w2", "w3"]); XCTAssertNil(model.errorText)
        XCTAssertEqual(model.sources[2].settings?["sequence"], .number(3)); XCTAssertTrue(model.pinnedUTC.contains("03.000001Z"))
        for text in ["1", "17", "2.5", "nan", ""] {
            model.countText = text; model.pin(); XCTAssertTrue(model.sources.isEmpty); XCTAssertNotNil(model.errorText)
        }
        model.countText = "2"; model.pin(); XCTAssertEqual(model.sources.map(\.id), ["w2", "w3"])
        model.clear(); XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.result)
    }
    @MainActor func testSelectedScopeDbmWindowAndUnknownSettings() throws {
        let model = SweepAverageModel(), client = try api()
        var wrong = raw(5); wrong["stream_id"] = "other"
        var unit = raw(6); unit["level_unit"] = "S"
        model.configure(client: client, scope: scope, sweeps: try sweeps() + [decode(wrong), decode(unit)])
        model.pin(); XCTAssertEqual(model.receivedCount, 4); XCTAssertEqual(model.target?.id, "w4")
        var legacy = raw(2); legacy.removeValue(forKey: "settings")
        model.configure(client: client, scope: scope, sweeps: [try decode(raw(1)), try decode(legacy), try decode(raw(3)), try decode(raw(4))])
        model.pin(); XCTAssertTrue(model.sources.isEmpty); XCTAssertNotNil(model.errorText)
    }
    @MainActor func testPollingNeverChangesPinPendingOrFrozenResultAndRepeatedCompareIsSingleFlight() async throws {
        let deferred = Deferred(), client = try api()
        let model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
            try await deferred.fetch(refs, target, threshold, bins)
        })
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin()
        model.compare(); model.compare(); try await waitFor { deferred.requests.count == 1 }
        model.configure(client: client, scope: scope, sweeps: try sweeps(2...5))
        XCTAssertTrue(model.pending); XCTAssertEqual(model.sources.map(\.id), ["w1", "w2", "w3"])
        XCTAssertEqual(deferred.requests[0].1, "w4")
        deferred.release(try decode(detectionBody())); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.comparison.target.id, "w4"); XCTAssertEqual(model.target?.id, "w5")
        model.configure(client: client, scope: scope, sweeps: try sweeps(3...6))
        XCTAssertEqual(model.result?.comparison.target.id, "w4"); XCTAssertEqual(deferred.requests.count, 1)
        model.compare(); try await waitFor { deferred.requests.count == 2 }
        XCTAssertEqual(deferred.requests[1].0, ["w1", "w2", "w3"]); XCTAssertEqual(deferred.requests[1].1, "w6")
        deferred.release(try decode(detectionBody(body(target: 6)))); try await waitFor { model.result?.comparison.target.id == "w6" }
    }
    @MainActor func testEveryResetInvalidatesNoncooperativeLateResponse() async throws {
        for reason in ["count", "clear", "pin", "server", "session", "stream", "client", "disconnect"] {
            let deferred = Deferred(), client = try api()
            let model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
                try await deferred.fetch(refs, target, threshold, bins)
            })
            model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
            try await waitFor { deferred.requests.count == 1 }
            switch reason {
            case "count": model.countText = "2"
            case "clear": model.clear()
            case "pin": model.pin()
            case "disconnect": model.reset()
            default:
                model.configure(client: reason == "client" ? try api() : client,
                    scope: .init(server: reason == "server" ? "other" : "local", session: reason == "session" ? "other" : "s", stream: reason == "stream" ? "other" : "tiny"), sweeps: try sweeps())
            }
            deferred.release(try decode(detectionBody())); await Task.yield()
            XCTAssertNil(model.result, reason); XCTAssertFalse(model.pending, reason)
            if reason != "pin" { XCTAssertTrue(model.sources.isEmpty, reason) }
        }
    }
    @MainActor func testLateResponseCannotOverwriteNewRequest() async throws {
        let deferred = Deferred(), client = try api()
        let model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
            try await deferred.fetch(refs, target, threshold, bins)
        })
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        try await waitFor { deferred.requests.count == 1 }; model.clear()
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin()
        model.configure(client: client, scope: scope, sweeps: try sweeps(2...5)); model.compare()
        try await waitFor { deferred.requests.count == 2 }
        deferred.release(try decode(detectionBody())); await Task.yield()
        XCTAssertTrue(model.pending); XCTAssertNil(model.result)
        deferred.release(try decode(detectionBody(body(target: 5)))); try await waitFor { !model.pending }
        XCTAssertEqual(model.result?.comparison.target.id, "w5")
    }
    @MainActor func testTimeoutRemainsBoundedAcrossPollingAndIgnoresLateResponse() async throws {
        let deferred = Deferred(), client = try api()
        let model = SweepAverageModel(timeoutNanoseconds: 40_000_000, fetch: { _, _, refs, target, threshold, bins in
            try await deferred.fetch(refs, target, threshold, bins)
        })
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        try await waitFor { deferred.requests.count == 1 }
        model.configure(client: client, scope: scope, sweeps: try sweeps(2...5))
        try await waitFor { !model.pending }; XCTAssertTrue(model.errorText?.contains("истекло") == true)
        deferred.release(try decode(detectionBody())); await Task.yield(); XCTAssertNil(model.result)
        XCTAssertEqual(model.sources.count, 3)
        model.compare(); try await waitFor { deferred.requests.count == 2 }
        deferred.release(try decode(detectionBody(body(target: 5)))); try await waitFor { model.result != nil }
        XCTAssertNil(model.errorText)
    }
    @MainActor func testFullFrozenSourceSequenceAndTargetContentValidation() async throws {
        for reason in ["sequence", "sourceTime", "targetTime", "stream", "targetLevels", "sourceBounds"] {
            let deferred = Deferred(), client = try api()
            let model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
                try await deferred.fetch(refs, target, threshold, bins)
            })
            var window = try sweeps()
            if reason == "sourceBounds" {
                window = try (1...4).map { n in var item = raw(n); if n < 4 { item["levels"] = [-100, -100, -100] }; return try decode(item) }
            }
            model.configure(client: client, scope: scope, sweeps: window); model.pin(); model.compare()
            try await waitFor { deferred.requests.count == 1 }
            var response = body()
            if reason == "sequence" || reason == "sourceTime" {
                var reference = response["reference"] as! [String: Any], sources = reference["sources"] as! [[String: Any]]
                if reason == "sequence" { sources[1]["settings"] = ["rbw_hz": 1000, "sequence": 99] }
                else { sources[1]["interval"] = ["started_at": "2026-11-08T01:00:02.000001Z", "ended_at": "2026-11-08T01:00:02.000002Z"] }
                reference["sources"] = sources; response["reference"] = reference
            }
            if reason == "targetTime" { var target = raw(4); target["interval"] = ["started_at": "2026-11-08T01:00:04Z", "ended_at": "2026-11-08T01:00:04.000002Z"]; response["target"] = target }
            if reason == "stream" { response["stream_id"] = "other" }
            if reason == "targetLevels" {
                var target = raw(4); target["levels"] = [-81, -100, -80]; response["target"] = target
                response["delta_levels"] = [9, -10, 10]; response["max_increase"] = ["frequency_hz": 200, "delta_db": 10]
            }
            deferred.release(try decode(detectionBody(response))); try await waitFor { !model.pending }
            XCTAssertNil(model.result, reason); XCTAssertNotNil(model.errorText, reason)
        }
    }
    @MainActor func testTargetInReferencesRejectedWithoutNetwork() throws {
        Stub.handler = { _ in XCTFail("Target in reference must not request") }
        let model = SweepAverageModel(), client = try api()
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin()
        model.configure(client: client, scope: scope, sweeps: try sweeps(1...3)); model.compare()
        XCTAssertNotNil(model.errorText); XCTAssertFalse(model.pending); XCTAssertEqual(model.sources.count, 3)
    }
    @MainActor func testActualTransportCancellationAndErrorsRetry() async throws {
        let model = SweepAverageModel(), client = try api()
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin()
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        model.compare(); await fulfillment(of: [started], timeout: 1); model.clear()
        await fulfillment(of: [stopped], timeout: 1); Stub.onStop = nil
        model.pin()
        for status in [404, 409, 413, 200] {
            Stub.handler = { $0.respond([:], status: status) }
            model.compare(); try await waitFor { !model.pending }
            XCTAssertNotNil(model.errorText); XCTAssertNil(model.result); XCTAssertEqual(model.sources.count, 3)
        }
        Stub.handler = { $0.respond(self.detectionBody()) }; model.compare(); try await waitFor { model.result != nil }
    }
    @MainActor func testNativeRemoteIntegrationAndDisconnectIsolation() async throws {
        var generation = 4
        var failed = false
        Stub.handler = { stub in
            let path = stub.request.url!.path
            if failed && path == "/api/v1/sessions/s" {
                stub.client?.urlProtocol(stub, didFailWithError: URLError(.notConnectedToInternet)); return
            }
            let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": "tiny", "device": "tinySA Ultra", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
            if path == "/api/v1/sessions" { stub.respond([session]) }
            else if path == "/api/v1/sessions/s" { stub.respond(session) }
            else if path.hasSuffix("/sweeps") { stub.respond((1...generation).map(self.raw)) }
            else if path.hasSuffix("/workers") { stub.respond([]) }
            else if path.hasSuffix("/sweep-average-change-detection") { stub.respond(self.detectionBody()) }
            else { stub.respond([:], status: 404) }
        }
        let remote = RemoteDualModel(makeClient: { _ in try self.api() })
        remote.connect(startPolling: false); defer { remote.disconnect() }
        try await waitFor { remote.sweepAverage.target != nil }
        let model = remote.sweepAverage
        remote.comparisonSelectionID = "w1"; remote.pinComparisonReference()
        let rawTrace = remote.current, revision = remote.waterfallRevision, error = remote.errorText
        model.pin(); model.compare(); try await waitFor { model.result != nil }
        XCTAssertEqual(remote.current, rawTrace); XCTAssertEqual(remote.waterfallRevision, revision)
        XCTAssertEqual(remote.errorText, error); XCTAssertEqual(remote.comparisonReference?.id, "w1")
        generation = 5; await remote.refresh()
        XCTAssertEqual(model.target?.id, "w5"); XCTAssertEqual(model.result?.comparison.target.id, "w4")
        failed = true; await remote.refresh()
        XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.result); XCTAssertNil(model.target)
        failed = false; await remote.refresh()
        XCTAssertEqual(model.target?.id, "w5"); XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.result)
        remote.disconnect(); XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.result); XCTAssertNil(model.target)
    }
    func testCoincidentMarkersKeepReferenceOutlineVisible() {
        let center = CGPoint(x: 100, y: 50)
        let reference = SweepAverageMarker.bounds(center: center, reference: true)
        let target = SweepAverageMarker.bounds(center: center, reference: false)
        XCTAssertEqual(reference.midX, target.midX); XCTAssertEqual(reference.midY, target.midY)
        XCTAssertTrue(reference.contains(target)); XCTAssertGreaterThan(reference.width - target.width, 1.5)
        XCTAssertEqual(reference.width, 8); XCTAssertEqual(target.width, 4)
    }

    @MainActor func testActualHTTPNativePinCompareSmoke() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_AVERAGE_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_AVERAGE_SMOKE_BASE_URL to synthetic fixture") }
        let remote = RemoteDualModel(); remote.serverURL = base; remote.connect(startPolling: false)
        defer { remote.disconnect() }
        try await waitFor { remote.sweepAverage.receivedCount >= 4 }
        let model = remote.sweepAverage; model.pin()
        XCTAssertEqual(model.sources.map(\.id), ["one-sweep-1", "one-sweep-2", "one-sweep-3"])
        model.compare(); try await waitFor { !model.pending }
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.points, 101)
        XCTAssertEqual(model.result?.maxIncrease.frequencyHz, 103_000_000)
    }
}
