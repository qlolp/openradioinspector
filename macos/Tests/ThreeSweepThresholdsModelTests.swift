import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class ThreeSweepThresholdsModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, [Double])] = []
        var continuations: [CheckedContinuation<PlatformThreeSweepThresholds, Error>] = []
        func fetch(_ sweep: String, _ levels: [Double]) async throws -> PlatformThreeSweepThresholds {
            requests.append((sweep, levels))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformThreeSweepThresholds) { continuations.removeFirst().resume(returning: value) }
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
                : ["error": ["code": "three_thresholds_incompatible", "message": "synthetic"]]
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
    private func raw(_ n: Int = 1, samples: [Double] = [-80, -100, -80]) -> [String: Any] {
        ["id": "w\(n)", "session_id": "s", "stream_id": "tiny", "interval": ["started_at": "2026-11-08T01:00:0\(n).000001Z", "ended_at": "2026-11-08T01:00:0\(n).000002Z"],
         "settings": ["rbw_hz": 1000, "sequence": n, "flag": false], "frequencies_hz": Array(repeating: 100, count: samples.count), "levels": samples, "level_unit": "dBm"]
    }
    private func body(_ n: Int = 1, levels: [Double] = [-90, -80, -70], samples: [Double] = [-80, -100, -80]) -> [String: Any] {
        let lines: [[String: Any]] = levels.enumerated().map { index, threshold in
            let excess = samples.map { $0 - threshold }
            var cursor = 0, regions: [[String: Any]] = []
            while cursor < samples.count {
                if excess[cursor] <= 0 { cursor += 1; continue }
                let first = cursor; var peak = cursor
                while cursor < samples.count && excess[cursor] > 0 {
                    if excess[cursor] > excess[peak] { peak = cursor }; cursor += 1
                }
                regions.append(["start_index": first, "end_index": cursor - 1, "start_frequency_hz": 100, "end_frequency_hz": 100,
                                "peak_index": peak, "peak_frequency_hz": 100, "peak_excess_db": excess[peak], "points": cursor - first])
            }
            return ["id": "line-\(index + 1)", "level_dbm": threshold, "excess_levels": excess, "regions": regions,
                    "exceedance_points": excess.filter { $0 > 0 }.count, "region_count": regions.count]
        }
        return ["session_id": "s", "stream_id": "tiny", "sweep": raw(n, samples: samples), "frequencies_hz": Array(repeating: 100, count: samples.count),
                "method": "three_horizontal", "comparison": "strictly_greater", "level_unit": "dBm", "delta_unit": "dB", "points": samples.count, "lines": lines]
    }
    @MainActor private var scope: ThreeSweepThresholdsModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for three-threshold state")
    }
    @MainActor func testFiniteThreeDefaultsAndInvalidInputDoesNotRequest() throws {
        Stub.handler = { _ in XCTFail("Invalid input reached network") }
        let model = ThreeSweepThresholdsModel(), client = try api()
        XCTAssertEqual(model.levels, [-90, -80, -70]); XCTAssertFalse(model.canAnalyze)
        model.configure(client: client, scope: scope, source: try decode(raw()))
        for value in ["", "nan", "inf", "1e309", "-80 -70", "1/2"] {
            model.line1Text = value; model.analyze()
            XCTAssertNil(model.levels, value); XCTAssertFalse(model.pending); XCTAssertNotNil(model.errorText)
        }
        model.line1Text = " -60,5 "; model.line2Text = "-90"; model.line3Text = "-60.5"
        XCTAssertEqual(model.levels, [-60.5, -90, -60.5])
    }
    @MainActor func testWrongScopeUnitLegacyAndInvalidUTCDoNotAnalyze() throws {
        Stub.handler = { _ in XCTFail("Invalid source reached network") }
        let model = ThreeSweepThresholdsModel(), client = try api()
        for key in ["session_id", "stream_id", "level_unit"] {
            var item = raw(); item[key] = "other"
            model.configure(client: client, scope: scope, source: try decode(item)); model.analyze()
            XCTAssertNil(model.source); XCTAssertFalse(model.pending)
        }
        for reason in ["settings", "time", "microseconds"] {
            var item = raw()
            if reason == "settings" { item.removeValue(forKey: "settings") }
            else { item["interval"] = ["started_at": "2026-11-08T01:00:01.000003Z", "ended_at": reason == "time" ? "2026-11-08T01:00:01.000002Z" : "2026-11-08T01:00:01.0000040Z"] }
            model.configure(client: client, scope: scope, source: try decode(item)); model.analyze()
            XCTAssertFalse(model.pending); XCTAssertNotNil(model.errorText)
        }
    }
    @MainActor func testPollingFrozenResultSingleFlightAndExplicitRepeat() async throws {
        let deferred = Deferred(), client = try api()
        let model = ThreeSweepThresholdsModel(fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
        model.configure(client: client, scope: scope, source: try decode(raw()))
        model.analyze(); model.analyze(); try await waitFor { deferred.requests.count == 1 }
        model.configure(client: client, scope: scope, source: try decode(raw(2)))
        XCTAssertTrue(model.pending); XCTAssertEqual(deferred.requests[0].0, "w1")
        deferred.release(try decode(body())); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.sweep.id, "w1"); XCTAssertEqual(model.source?.id, "w2")
        model.configure(client: client, scope: scope, source: try decode(raw(3)))
        XCTAssertEqual(model.result?.sweep.id, "w1"); XCTAssertEqual(deferred.requests.count, 1)
        model.analyze(); try await waitFor { deferred.requests.count == 2 }
        XCTAssertNil(model.result); XCTAssertEqual(deferred.requests[1].0, "w3")
        deferred.release(try decode(body(3))); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.sweep.settings["sequence"], .number(3))
    }
    @MainActor func testEveryEditClearCloseScopeAndDisconnectInvalidatesLateResponse() async throws {
        for reason in ["line1", "line2", "line3", "clear", "close", "server", "session", "stream", "client", "disconnect"] {
            let deferred = Deferred(), client = try api()
            let model = ThreeSweepThresholdsModel(fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
            model.configure(client: client, scope: scope, source: try decode(raw())); model.analyze()
            try await waitFor { deferred.requests.count == 1 }
            switch reason {
            case "line1": model.line1Text = "-91"
            case "line2": model.line2Text = "-81"
            case "line3": model.line3Text = "-71"
            case "clear", "close": model.clear() // The pane's onDisappear uses this exact operation.
            case "disconnect": model.reset()
            default:
                model.configure(client: reason == "client" ? try api() : client,
                    scope: .init(server: reason == "server" ? "other" : "local", session: reason == "session" ? "other" : "s", stream: reason == "stream" ? "other" : "tiny"), source: try decode(raw()))
            }
            deferred.release(try decode(body())); await Task.yield()
            XCTAssertNil(model.result, reason); XCTAssertFalse(model.pending, reason)
        }
    }
    @MainActor func testLateResultCannotReplaceNewRequestOrEditedLevels() async throws {
        let deferred = Deferred(), client = try api()
        let model = ThreeSweepThresholdsModel(fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
        model.configure(client: client, scope: scope, source: try decode(raw())); model.analyze()
        try await waitFor { deferred.requests.count == 1 }; model.line1Text = "-70"; model.line3Text = "-90"
        model.configure(client: client, scope: scope, source: try decode(raw(2))); model.analyze()
        try await waitFor { deferred.requests.count == 2 }
        XCTAssertEqual(deferred.requests[1].1, [-70, -80, -90])
        deferred.release(try decode(body())); await Task.yield()
        XCTAssertTrue(model.pending); XCTAssertNil(model.result)
        deferred.release(try decode(body(2, levels: [-70, -80, -90]))); try await waitFor { !model.pending }
        XCTAssertEqual(model.result?.sweep.id, "w2"); XCTAssertEqual(model.result?.lines.map(\.levelDbm), [-70, -80, -90])
        model.line2Text = "-71"; XCTAssertNil(model.result)
    }
    @MainActor func testBoundedTimeoutSurvivesPollingIgnoresLateAndAllowsRetry() async throws {
        let deferred = Deferred(), client = try api()
        let model = ThreeSweepThresholdsModel(timeoutNanoseconds: 40_000_000, fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
        model.configure(client: client, scope: scope, source: try decode(raw())); model.analyze()
        try await waitFor { deferred.requests.count == 1 }
        model.configure(client: client, scope: scope, source: try decode(raw(2)))
        try await waitFor { !model.pending }; XCTAssertTrue(model.errorText?.contains("истекло") == true)
        deferred.release(try decode(body())); await Task.yield(); XCTAssertNil(model.result)
        model.analyze(); try await waitFor { deferred.requests.count == 2 }
        deferred.release(try decode(body(2))); try await waitFor { model.result != nil }; XCTAssertNil(model.errorText)
    }
    @MainActor func testExactFrozenProvenanceRejectsResponseSubstitution() async throws {
        for reason in ["sequence", "typed", "time", "session", "stream", "levels", "input"] {
            let deferred = Deferred(), client = try api()
            let model = ThreeSweepThresholdsModel(fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
            model.configure(client: client, scope: scope, source: try decode(raw())); model.analyze()
            try await waitFor { deferred.requests.count == 1 }
            var response = body(), sweep = raw()
            switch reason {
            case "sequence": sweep["settings"] = ["rbw_hz": 1000, "sequence": 99, "flag": false]; response["sweep"] = sweep
            case "typed": sweep["settings"] = ["rbw_hz": 1000, "sequence": 1, "flag": 0]; response["sweep"] = sweep
            case "time": sweep["interval"] = ["started_at": "2026-11-08T01:00:01.000001Z", "ended_at": "2026-11-08T01:00:01.000003Z"]; response["sweep"] = sweep
            case "session": response["session_id"] = "other"
            case "stream": response["stream_id"] = "other"
            case "levels": response = body(samples: [-81, -100, -80])
            default: response = body(levels: [-91, -80, -70])
            }
            deferred.release(try decode(response)); try await waitFor { !model.pending }
            XCTAssertNil(model.result, reason); XCTAssertNotNil(model.errorText, reason)
        }
    }
    @MainActor func testActualTransportCancelledByClearAndTimeoutAndErrorsRetry() async throws {
        let client = try api()
        for timed in [false, true] {
            let model = ThreeSweepThresholdsModel(timeoutNanoseconds: timed ? 40_000_000 : 6_000_000_000)
            model.configure(client: client, scope: scope, source: try decode(raw()))
            let started = expectation(description: "started"), stopped = expectation(description: "stopped")
            Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
            model.analyze(); await fulfillment(of: [started], timeout: 1)
            if !timed { model.clear() }
            await fulfillment(of: [stopped], timeout: 1); Stub.onStop = nil
            XCTAssertFalse(model.pending); XCTAssertNil(model.result)
            if timed { XCTAssertTrue(model.errorText?.contains("истекло") == true) }
        }
        let model = ThreeSweepThresholdsModel(); model.configure(client: client, scope: scope, source: try decode(raw()))
        for status in [400, 404, 409, 413, 200] {
            Stub.handler = { $0.respond([:], status: status) }
            model.analyze(); try await waitFor { !model.pending }
            XCTAssertNotNil(model.errorText); XCTAssertNil(model.result)
        }
        Stub.handler = { $0.respond(self.body()) }; model.analyze(); try await waitFor { model.result != nil }
        XCTAssertNil(model.errorText)
    }
    @MainActor func testNativeRefreshFailureDisconnectAndIndependentPanelState() async throws {
        var generation = 1, failed = false
        Stub.handler = { stub in
            let path = stub.request.url!.path
            if failed && path == "/api/v1/sessions/s" { stub.client?.urlProtocol(stub, didFailWithError: URLError(.notConnectedToInternet)); return }
            let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": "tiny", "device": "tinySA Ultra", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
            if path == "/api/v1/sessions" { stub.respond([session]) }
            else if path == "/api/v1/sessions/s" { stub.respond(session) }
            else if path.hasSuffix("/sweeps") { stub.respond((1...generation).map { self.raw($0) }) }
            else if path.hasSuffix("/workers") { stub.respond([]) }
            else if path.hasSuffix("/sweep-three-thresholds") { stub.respond(self.body(generation)) }
            else { stub.respond([:], status: 404) }
        }
        let remote = RemoteDualModel(makeClient: { _ in try self.api() })
        remote.connect(startPolling: false); defer { remote.disconnect() }
        let model = remote.threeSweepThresholds
        try await waitFor { model.source != nil }
        remote.comparisonSelectionID = "w1"; remote.pinComparisonReference()
        let trace = remote.current, revision = remote.waterfallRevision, error = remote.errorText
        remote.sweepThreshold.marginDb = "11"; remote.sweepAverage.countText = "5"
        model.analyze(); try await waitFor { model.result != nil }
        XCTAssertEqual(remote.current, trace); XCTAssertEqual(remote.waterfallRevision, revision)
        XCTAssertEqual(remote.errorText, error); XCTAssertEqual(remote.comparisonReference?.id, "w1")
        XCTAssertEqual(remote.sweepThreshold.marginDb, "11"); XCTAssertEqual(remote.sweepAverage.countText, "5")
        generation = 2; await remote.refresh(); XCTAssertEqual(model.source?.id, "w2"); XCTAssertEqual(model.result?.sweep.id, "w1")
        failed = true; await remote.refresh(); XCTAssertNil(model.source); XCTAssertNil(model.result)
        failed = false; await remote.refresh(); XCTAssertEqual(model.source?.id, "w2"); XCTAssertNil(model.result)
        model.analyze(); try await waitFor { model.result != nil }
        remote.serverURL = "https://other.test"; XCTAssertNil(model.source); XCTAssertNil(model.result)
        remote.disconnect(); XCTAssertNil(model.source); XCTAssertNil(model.result)
    }
    @MainActor func testUnicodeByteDifferentScopeImmediatelyClearsIncludingRemoteHooks() async throws {
        let composed = "é", decomposed = "e\u{301}", client = try api()
        XCTAssertEqual(composed, decomposed)
        XCTAssertFalse(PlatformThreeSweepThresholds.sameID(composed, decomposed))
        for field in ["server", "session", "stream"] {
            let deferred = Deferred()
            let model = ThreeSweepThresholdsModel(fetch: { _, _, id, levels in try await deferred.fetch(id, levels) })
            var item = raw()
            if field == "session" { item["session_id"] = composed }; if field == "stream" { item["stream_id"] = composed }
            model.configure(client: client, scope: .init(server: field == "server" ? composed : "local", session: field == "session" ? composed : "s", stream: field == "stream" ? composed : "tiny"), source: try decode(item))
            model.analyze(); try await waitFor { deferred.requests.count == 1 }
            model.configure(client: client, scope: .init(server: field == "server" ? decomposed : "local", session: field == "session" ? decomposed : "s", stream: field == "stream" ? decomposed : "tiny"), source: try decode(item))
            XCTAssertFalse(model.pending); XCTAssertNil(model.result)
            deferred.release(try decode(body())); await Task.yield(); XCTAssertNil(model.result)
        }
        for field in ["server", "session", "stream", "selection"] {
            let remote = RemoteDualModel(), model = remote.threeSweepThresholds
            func session(_ id: String) throws -> PlatformSession {
                try decode(["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": id, "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]])
            }
            switch field {
            case "server": remote.serverURL = "https://example.test/" + composed
            case "stream": remote.session = try session(composed)
            default: remote.selectedSessionID = composed
            }
            model.configure(client: client, scope: scope, source: try decode(raw()))
            switch field {
            case "server": remote.serverURL = "https://example.test/" + decomposed
            case "stream": remote.session = try session(decomposed)
            case "selection": remote.selectSession(decomposed)
            default: remote.selectedSessionID = decomposed
            }
            XCTAssertNil(model.source, field); XCTAssertNil(model.result, field)
        }
    }
    func testAllRawBinsAndNestedIdentitiesForSingleDuplicateCollapsedAndMaximumGrids() throws {
        for count in [1, 2, 16_384] {
            let result: PlatformThreeSweepThresholds = try decode(body(levels: [-80, -80, -80], samples: Array(repeating: -80, count: count)))
            let traces = ThreeSweepThresholdsChart.traces(result, excess: false)
            XCTAssertEqual(traces.count, 4); XCTAssertTrue(traces.allSatisfy { $0.count == count })
            XCTAssertEqual(traces[0], result.sweep.levels)
            XCTAssertEqual(ThreeSweepThresholdsChart.traces(result, excess: true), result.lines.map(\.excessLevels))
            XCTAssertEqual(ThreeSweepThresholdsChart.x(100, first: 100, last: 100, width: 200), 100)
        }
        let center = CGPoint(x: 100, y: 50)
        let markers = (0...3).map { ThreeThresholdMarker.bounds(center: center, identity: $0) }
        XCTAssertEqual(markers.map(\.width), [4, 8, 14, 20])
        for index in 0..<3 {
            XCTAssertEqual(markers[index].midX, markers[index + 1].midX)
            XCTAssertTrue(markers[index + 1].contains(markers[index]))
            XCTAssertGreaterThan(markers[index + 1].width - markers[index].width, 2.6)
        }
        XCTAssertEqual(ThreeThresholdMarker.path(center: center, identity: 1).boundingRect, markers[1])
        XCTAssertEqual(ThreeThresholdMarker.path(center: center, identity: 2).boundingRect, markers[2])
        XCTAssertEqual(ThreeThresholdMarker.path(center: center, identity: 3).boundingRect, markers[3])
    }
    func testThresholdAndExcessLabelsRetainRoundTripPrecision() {
        XCTAssertEqual(ThreeThresholdDisplay.number(-90.0000001), "-90.0000001")
        for value in [-90.0000001, Double.leastNonzeroMagnitude, Double.greatestFiniteMagnitude, -1e-100, 10.1234567890123] {
            XCTAssertEqual(Double(ThreeThresholdDisplay.number(value)), value)
        }
    }
    @MainActor func testActualHTTPNativeThreeThresholdSmoke() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_THREE_THRESHOLDS_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_THREE_THRESHOLDS_SMOKE_BASE_URL to synthetic fixture") }
        let remote = RemoteDualModel(); remote.serverURL = base; remote.connect(startPolling: false)
        defer { remote.disconnect() }
        try await waitFor { remote.threeSweepThresholds.source != nil }
        if remote.selectedSessionID != "three-one" { remote.selectSession("three-one"); try await waitFor { remote.threeSweepThresholds.source?.sessionId == "three-one" } }
        let model = remote.threeSweepThresholds
        XCTAssertEqual(model.source?.id, "one-sweep-2")
        let trace = remote.current, revision = remote.waterfallRevision
        model.analyze(); try await waitFor { !model.pending }
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.points, 101)
        XCTAssertEqual(model.result?.lines.map(\.exceedancePoints), [4, 2, 1]); XCTAssertEqual(model.result?.lines.map(\.regionCount), [2, 2, 1])
        XCTAssertEqual(remote.current, trace); XCTAssertEqual(remote.waterfallRevision, revision)
        XCTAssertEqual(model.result?.sweep.settings["sequence"], .number(2))
    }
}
