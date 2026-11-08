import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepContextModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, Int, Int)] = []
        var continuations: [CheckedContinuation<PlatformSweepContext, Error>] = []
        func fetch(_ sweep: String, _ before: Int, _ after: Int) async throws -> PlatformSweepContext {
            requests.append((sweep, before, after))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformSweepContext) { continuations.removeFirst().resume(returning: value) }
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
                : ["error": ["code": "context_incompatible", "message": "synthetic"]]
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
    private func raw(_ n: Int = 4, points: Int = 3) -> [String: Any] {
        ["id": "w\(n)", "session_id": "s", "stream_id": "tiny", "interval": ["started_at": String(format: "2026-11-08T01:00:%02d.000001Z", n), "ended_at": String(format: "2026-11-08T01:00:%02d.000002Z", n)],
         "settings": ["rbw_hz": 1000, "sequence": n, "flag": false], "frequencies_hz": Array(repeating: 100, count: points),
         "levels": (0..<points).map { -100.0 + Double(n) + Double($0) / 10 }, "level_unit": "dBm"]
    }
    private func body(_ n: Int = 4, before: Int = 2, after: Int = 2, points: Int = 3) -> [String: Any] {
        let items = (-before...after).map { offset -> [String: Any] in
            var item = raw(n + offset, points: points); item["offset"] = offset
            item.removeValue(forKey: "frequencies_hz"); item.removeValue(forKey: "session_id"); item.removeValue(forKey: "stream_id")
            return item
        }
        return ["session_id": "s", "stream_id": "tiny", "anchor_id": "w\(n)", "order": "started_at_then_id", "requested": ["before": before, "after": after],
                "available": ["before": before, "after": after], "missing": ["before": 0, "after": 0], "complete": true,
                "frequencies_hz": Array(repeating: 100, count: points), "points": points, "level_unit": "dBm", "items": items]
    }
    @MainActor private var scope: SweepContextModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for context state")
    }
    @MainActor func testDefaultsStrictCountsAndForeignInvalidSourcesCannotLoad() throws {
        Stub.handler = { _ in XCTFail("Invalid input reached network") }
        let model = SweepContextModel(), client = try api()
        XCTAssertEqual(model.before, 2); XCTAssertEqual(model.after, 2); XCTAssertFalse(model.canLoad)
        model.configure(client: client, scope: scope, sweeps: [try decode(raw())])
        for text in ["", "1.5", "2.0", "-1", "17", "+2", "NaN", "2e0", "true", "99999999999999999999999"] {
            model.beforeText = text; model.load(); XCTAssertFalse(model.pending, text); XCTAssertNotNil(model.errorText, text)
            model.beforeText = "2"; model.afterText = text; model.load(); XCTAssertFalse(model.pending, text); XCTAssertNotNil(model.errorText, text)
            model.afterText = "2"
        }
        for reason in ["session", "stream", "settings", "unit", "utc", "nanoseconds"] {
            var value = raw()
            switch reason {
            case "session": value["session_id"] = "foreign"
            case "stream": value["stream_id"] = "foreign"
            case "unit": value["level_unit"] = "S"
            case "settings": value.removeValue(forKey: "settings")
            default: value["interval"] = ["started_at": "2026-11-08T01:00:04.000003Z", "ended_at": reason == "utc" ? "2026-11-08T01:00:04.000002Z" : "2026-11-08T01:00:04.0000040Z"]
            }
            model.reset(); model.configure(client: client, scope: scope, sweeps: [try decode(value)])
            XCTAssertTrue(model.choices.isEmpty, reason); XCTAssertNil(model.anchor, reason); XCTAssertFalse(model.canLoad, reason)
        }
    }
    @MainActor func testChooseEarlierAnchorSingleFlightPollingPreservesFullValueAndResult() async throws {
        let deferred = Deferred(), client = try api()
        let model = SweepContextModel(fetch: { _, _, id, before, after in try await deferred.fetch(id, before, after) })
        model.configure(client: client, scope: scope, sweeps: try [raw(7), raw(4), raw(1)].map { try decode($0) })
        XCTAssertEqual(model.choices.map(\.id), ["w1", "w4", "w7"]); XCTAssertEqual(model.anchor?.id, "w7")
        model.selectAnchor(id: "w4"); model.load(); model.load()
        try await waitFor { deferred.requests.count == 1 }; XCTAssertEqual(deferred.requests[0].0, "w4")
        var changed = raw(); changed["levels"] = [-1, -2, -3]
        model.configure(client: client, scope: scope, sweeps: try [changed, raw(8)].map { try decode($0) })
        XCTAssertEqual(model.anchor?.levels.first, -96); XCTAssertEqual(model.frozenAnchor?.levels.first, -96)
        deferred.release(try decode(body())); try await waitFor { model.result != nil }
        model.selectRow(offset: 2); XCTAssertEqual(model.selectedItem?.id, "w6")
        model.configure(client: client, scope: scope, sweeps: [try decode(raw(9))])
        XCTAssertEqual(model.anchor?.id, "w4"); XCTAssertEqual(model.result?.anchorId, "w4"); XCTAssertEqual(model.selectedOffset, 2)
        XCTAssertEqual(deferred.requests.count, 1)
        model.selectAnchor(id: "w9"); XCTAssertNil(model.result); XCTAssertNil(model.frozenAnchor)
        model.load(); try await waitFor { deferred.requests.count == 2 }
        deferred.release(try decode(body(9))); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.anchorId, "w9"); XCTAssertEqual(model.selectedOffset, 0)
    }
    @MainActor func testEditsClearCloseAnchorScopeClientAndDisconnectInvalidateLateResponses() async throws {
        for reason in ["before", "after", "clear", "close", "anchor", "server", "session", "stream", "client", "disconnect"] {
            let deferred = Deferred(), client = try api()
            let model = SweepContextModel(fetch: { _, _, id, b, a in try await deferred.fetch(id, b, a) })
            model.configure(client: client, scope: scope, sweeps: try [raw(4), raw(5)].map { try decode($0) })
            model.selectAnchor(id: "w4"); model.load(); try await waitFor { deferred.requests.count == 1 }
            switch reason {
            case "before": model.beforeText = "1"
            case "after": model.afterText = "0"
            case "clear", "close": model.clear() // Pane onDisappear calls this exact cancellation operation.
            case "anchor": model.selectAnchor(id: "w5")
            case "disconnect": model.reset()
            default:
                model.configure(client: reason == "client" ? try api() : client,
                    scope: .init(server: reason == "server" ? "other" : "local", session: reason == "session" ? "other" : "s", stream: reason == "stream" ? "other" : "tiny"), sweeps: [try decode(raw())])
            }
            deferred.release(try decode(body())); await Task.yield()
            XCTAssertNil(model.result, reason); XCTAssertNil(model.frozenAnchor, reason); XCTAssertFalse(model.pending, reason)
        }
    }
    @MainActor func testLateOldResultCannotReplaceNewWindowAndTimeoutAllowsRetry() async throws {
        let deferred = Deferred(), client = try api()
        let model = SweepContextModel(fetch: { _, _, id, b, a in try await deferred.fetch(id, b, a) })
        model.configure(client: client, scope: scope, sweeps: [try decode(raw())]); model.load()
        try await waitFor { deferred.requests.count == 1 }
        model.beforeText = "1"; model.afterText = "0"; model.load(); try await waitFor { deferred.requests.count == 2 }
        deferred.release(try decode(body())); await Task.yield(); XCTAssertTrue(model.pending); XCTAssertNil(model.result)
        deferred.release(try decode(body(before: 1, after: 0))); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.requested.before, 1); XCTAssertEqual(model.result?.requested.after, 0)
        let timed = SweepContextModel(timeoutNanoseconds: 500_000_000, fetch: { _, _, id, b, a in try await deferred.fetch(id, b, a) })
        timed.configure(client: client, scope: scope, sweeps: [try decode(raw())]); timed.load()
        try await waitFor { deferred.requests.count == 3 }; timed.configure(client: client, scope: scope, sweeps: [try decode(raw(7))])
        try await waitFor { !timed.pending }; XCTAssertTrue(timed.errorText?.contains("истекло") == true)
        deferred.release(try decode(body())); await Task.yield(); XCTAssertNil(timed.result)
        timed.load(); try await waitFor { deferred.requests.count == 4 }
        deferred.release(try decode(body())); try await waitFor { timed.result != nil }; XCTAssertNil(timed.errorText)
    }
    @MainActor func testAllReturnedRowsSelectableAndRawChartDoesNotSampleBins() async throws {
        for points in [1, 2, 16_384] {
            let client = try api(), value: PlatformSweepContext = try decode(body(before: 0, after: 0, points: points))
            let model = SweepContextModel(fetch: { _, _, _, _, _ in value })
            model.beforeText = "0"; model.afterText = "0"; model.configure(client: client, scope: scope, sweeps: [try decode(raw(points: points))]); model.load()
            try await waitFor { model.result != nil }
            let selected = try XCTUnwrap(model.selectedItem), bins = SweepContextChart.points(frequenciesHz: value.frequenciesHz, levels: selected.levels)
            XCTAssertEqual(bins.count, points); XCTAssertEqual(bins.map(\.y), selected.levels); XCTAssertEqual(bins.map(\.x), Array(repeating: 100, count: points))
        }
        let client = try api(), value: PlatformSweepContext = try decode(body(17, before: 16, after: 16))
        let model = SweepContextModel(fetch: { _, _, _, _, _ in value }); model.beforeText = "16"; model.afterText = "16"
        model.configure(client: client, scope: scope, sweeps: [try decode(raw(17))]); model.load(); try await waitFor { model.result != nil }
        for offset in -16...16 {
            model.selectRow(offset: offset); XCTAssertEqual(model.selectedItem?.id, "w\(17 + offset)")
            XCTAssertEqual(model.selectedItem?.levels.first, -100 + Double(17 + offset)); XCTAssertEqual(model.selectedItem?.settings["sequence"], .number(Double(17 + offset)))
        }
        model.selectRow(offset: 17); XCTAssertEqual(model.selectedOffset, 16)
    }
    @MainActor func testExactFrozenProvenanceRejectsSubstitutions() async throws {
        for reason in ["sequence", "typed", "time", "session", "stream", "levels", "before", "after"] {
            let client = try api(); var response = body(), items = response["items"] as! [[String: Any]]
            switch reason {
            case "sequence": items[2]["settings"] = ["rbw_hz": 1000, "sequence": 99, "flag": false]
            case "typed": for i in items.indices { items[i]["settings"] = ["rbw_hz": 1000, "sequence": i + 2, "flag": 0] }
            case "time": items[2]["interval"] = ["started_at": "2026-11-08T01:00:04.000001Z", "ended_at": "2026-11-08T01:00:04.000003Z"]
            case "session": response["session_id"] = "other"
            case "stream": response["stream_id"] = "other"
            case "levels": items[2]["levels"] = [-95, -95.9, -95.8]
            default: break
            }
            response["items"] = items
            if reason == "before" { response = body(before: 1) }; if reason == "after" { response = body(after: 1) }
            let value: PlatformSweepContext = try decode(response)
            let model = SweepContextModel(fetch: { _, _, _, _, _ in value })
            model.configure(client: client, scope: scope, sweeps: [try decode(raw())]); model.load(); try await waitFor { !model.pending }
            XCTAssertNil(model.result, reason); XCTAssertNotNil(model.errorText, reason)
        }
    }
    @MainActor func testRealTransportCancellationAndHTTPFailureRetry() async throws {
        let client = try api()
        for reason in ["clear", "before", "after", "close", "disconnect", "scope", "timeout"] {
            let model = SweepContextModel(timeoutNanoseconds: reason == "timeout" ? 500_000_000 : 6_000_000_000)
            model.configure(client: client, scope: scope, sweeps: [try decode(raw())])
            let started = expectation(description: "started"), stopped = expectation(description: "stopped")
            Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
            model.load(); await fulfillment(of: [started], timeout: 1)
            switch reason {
            case "before": model.beforeText = "1"
            case "after": model.afterText = "1"
            case "disconnect": model.reset()
            case "scope": model.configure(client: client, scope: .init(server: "other", session: "s", stream: "tiny"), sweeps: [])
            case "timeout": break
            default: model.clear()
            }
            await fulfillment(of: [stopped], timeout: 1); Stub.onStop = nil
            XCTAssertFalse(model.pending); XCTAssertNil(model.result)
            if reason == "timeout" { XCTAssertTrue(model.errorText?.contains("истекло") == true) }
        }
        let model = SweepContextModel(); model.configure(client: client, scope: scope, sweeps: [try decode(raw())])
        for status in [400, 404, 409, 413, 200] {
            Stub.handler = { $0.respond([:], status: status) }; model.load(); try await waitFor { !model.pending }
            XCTAssertNotNil(model.errorText); XCTAssertNil(model.result)
        }
        Stub.handler = { $0.respond(self.body()) }; model.load(); try await waitFor { model.result != nil }; XCTAssertNil(model.errorText)
    }
    @MainActor func testCanonicalEquivalentIDsRemainSeparateChoicesAndRows() async throws {
        let client = try api(), ids = ["e\u{301}", "é"]
        let sweeps: [PlatformSweep] = try ids.map { id in var value = raw(); value["id"] = id; return try decode(value) }
        var response = body(before: 0, after: 1), items = response["items"] as! [[String: Any]]
        for i in items.indices { items[i]["id"] = ids[i]; items[i]["interval"] = raw()["interval"] }
        response["items"] = items; response["anchor_id"] = ids[0]
        let value: PlatformSweepContext = try decode(response)
        let model = SweepContextModel(fetch: { _, _, id, _, _ in XCTAssertEqual(Data(id.utf8), Data(ids[0].utf8)); return value })
        model.beforeText = "0"; model.afterText = "1"; model.configure(client: client, scope: scope, sweeps: sweeps)
        XCTAssertEqual(model.choices.count, 2); model.selectAnchor(id: ids[0]); XCTAssertEqual(model.anchor?.id.utf8.map { $0 }, Array(ids[0].utf8))
        model.load(); try await waitFor { model.result != nil }
        model.selectRow(offset: 1); XCTAssertEqual(model.selectedItem?.id.utf8.map { $0 }, Array(ids[1].utf8))
        model.selectRow(offset: 0); XCTAssertEqual(model.selectedItem?.id.utf8.map { $0 }, Array(ids[0].utf8))
    }
    @MainActor func testUnicodeByteDifferentScopeClearsModelAndRemoteHooks() async throws {
        let composed = "é", decomposed = "e\u{301}", client = try api()
        for field in ["server", "session", "stream"] {
            let deferred = Deferred(), model = SweepContextModel(fetch: { _, _, id, b, a in try await deferred.fetch(id, b, a) })
            var item = raw(); if field == "session" { item["session_id"] = composed }; if field == "stream" { item["stream_id"] = composed }
            model.configure(client: client, scope: .init(server: field == "server" ? composed : "local", session: field == "session" ? composed : "s", stream: field == "stream" ? composed : "tiny"), sweeps: [try decode(item)])
            model.load(); try await waitFor { deferred.requests.count == 1 }
            model.configure(client: client, scope: .init(server: field == "server" ? decomposed : "local", session: field == "session" ? decomposed : "s", stream: field == "stream" ? decomposed : "tiny"), sweeps: [try decode(item)])
            XCTAssertFalse(model.pending); XCTAssertNil(model.result); deferred.release(try decode(body())); await Task.yield(); XCTAssertNil(model.result)
        }
        for field in ["server", "session", "stream", "selection"] {
            let remote = RemoteDualModel(), model = remote.sweepContext
            func session(_ id: String) throws -> PlatformSession {
                try decode(["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": id, "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]])
            }
            switch field { case "server": remote.serverURL = composed; case "stream": remote.session = try session(composed); default: remote.selectedSessionID = composed }
            model.configure(client: client, scope: scope, sweeps: [try decode(raw())])
            switch field {
            case "server": remote.serverURL = decomposed
            case "stream": remote.session = try session(decomposed)
            case "selection": remote.selectSession(decomposed)
            default: remote.selectedSessionID = decomposed
            }
            XCTAssertNil(model.anchor, field); XCTAssertTrue(model.choices.isEmpty, field)
        }
    }
    @MainActor func testRemotePollingFailureDisconnectAndIndependentOtherPanels() async throws {
        var count = 4, failed = false
        Stub.handler = { stub in
            let path = stub.request.url!.path
            if failed && path == "/api/v1/sessions/s" { stub.client?.urlProtocol(stub, didFailWithError: URLError(.notConnectedToInternet)); return }
            let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": "tiny", "device": "tinySA Ultra", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
            if path == "/api/v1/sessions" { stub.respond([session]) }
            else if path == "/api/v1/sessions/s" { stub.respond(session) }
            else if path.hasSuffix("/sweeps") { stub.respond((1...count).map { self.raw($0) }) }
            else if path.hasSuffix("/workers") { stub.respond([]) }
            else if path.hasSuffix("/sweep-context") { stub.respond(self.body()) }
            else { stub.respond([:], status: 404) }
        }
        let remote = RemoteDualModel(makeClient: { _ in try self.api() }); remote.connect(startPolling: false); defer { remote.disconnect() }
        let model = remote.sweepContext; try await waitFor { model.anchor != nil }
        remote.comparisonSelectionID = "w1"; remote.pinComparisonReference(); remote.sweepAverage.countText = "5"; remote.threeSweepThresholds.line1Text = "-91"
        let trace = remote.current, revision = remote.waterfallRevision, error = remote.errorText
        model.load(); try await waitFor { model.result != nil }
        XCTAssertEqual(remote.current, trace); XCTAssertEqual(remote.waterfallRevision, revision); XCTAssertEqual(remote.errorText, error)
        XCTAssertEqual(remote.comparisonReference?.id, "w1"); XCTAssertEqual(remote.sweepAverage.countText, "5"); XCTAssertEqual(remote.threeSweepThresholds.line1Text, "-91")
        count = 7; await remote.refresh(); XCTAssertEqual(model.choices.count, 7); XCTAssertEqual(model.anchor?.id, "w4"); XCTAssertEqual(model.result?.anchorId, "w4")
        failed = true; await remote.refresh(); XCTAssertNil(model.anchor); XCTAssertNil(model.result); XCTAssertTrue(model.choices.isEmpty)
        failed = false; await remote.refresh(); XCTAssertEqual(model.anchor?.id, "w7"); XCTAssertNil(model.result)
        remote.serverURL = "https://other.test"; XCTAssertNil(model.anchor); XCTAssertTrue(model.choices.isEmpty)
        remote.disconnect(); XCTAssertNil(model.anchor); XCTAssertNil(model.result)
    }
    @MainActor func testActualHTTPNativeSyntheticContextAndSessionChange() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_CONTEXT_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_CONTEXT_SMOKE_BASE_URL to synthetic fixture") }
        let remote = RemoteDualModel(); remote.serverURL = base; remote.connect(startPolling: false); defer { remote.disconnect() }
        let model = remote.sweepContext; try await waitFor { model.anchor != nil }
        if remote.selectedSessionID != "context-one" { remote.selectSession("context-one"); try await waitFor { model.anchor?.sessionId == "context-one" } }
        XCTAssertEqual(model.choices.count, 7); model.selectAnchor(id: "one-sweep-4")
        let trace = remote.current, revision = remote.waterfallRevision
        model.load(); try await waitFor { !model.pending }
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.points, 101); XCTAssertEqual(model.result?.items.map(\.id), (2...6).map { "one-sweep-\($0)" })
        XCTAssertEqual(model.selectedItem?.levels.first, -96)
        for offset in -2...2 { model.selectRow(offset: offset); XCTAssertEqual(model.selectedItem?.levels.first, -96 + Double(offset)) }
        await remote.refresh(); XCTAssertEqual(model.anchor?.id, "one-sweep-4"); XCTAssertEqual(model.result?.anchorId, "one-sweep-4"); XCTAssertEqual(model.selectedOffset, 2)
        XCTAssertEqual(remote.current, trace); XCTAssertEqual(remote.waterfallRevision, revision)
        remote.selectSession("context-two"); XCTAssertNil(model.result); try await waitFor { model.anchor?.sessionId == "context-two" }
        XCTAssertTrue(model.choices.allSatisfy { $0.sessionId == "context-two" }); XCTAssertNil(model.frozenAnchor)
    }
}
