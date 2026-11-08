import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepAverageModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [([String], String)] = []
        var parameters: [(Double, Int)] = []
        var continuations: [CheckedContinuation<PlatformSweepAverageChangeDetection, Error>] = []
        func fetch(_ refs: [String], _ target: String, _ threshold: Double, _ bins: Int) async throws -> PlatformSweepAverageChangeDetection {
            requests.append((refs, target))
            parameters.append((threshold, bins))
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
         "target": raw(target).filter { ["id", "interval", "settings", "levels", "level_unit"].contains($0.key) },
         "delta_levels": [10, -10, 10], "delta_unit": "dB", "points": 3,
         "max_increase": ["frequency_hz": 100, "delta_db": 10], "min_change": ["frequency_hz": 100, "delta_db": -10]]
    }
    private func detectionBody(_ comparison: [String: Any]? = nil) -> [String: Any] {
        ["comparison": comparison ?? body(),
         "detection": ["minimum_increase_db": 6.0, "minimum_bins": 2, "regions": [], "count": 0]]
    }
    private func exportableResult() throws -> PlatformSweepAverageChangeDetection {
        try PlatformSweepAverageChangeDetection.validatedResponse(JSONSerialization.data(withJSONObject: [
            "contract": "openradioinspector-platform", "contract_version": 1, "data": detectionBody(),
        ]))
    }
    @MainActor func testExportTicketFreezesResultAndRejectsStaleReentryAndDismissal() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let destination = directory.appendingPathComponent("analysis.json")
        let value = try exportableResult(), client = try api()
        let model = SweepAverageModel(fetch: { _, _, _, _, _, _ in value })
        XCTAssertNil(model.beginExport())
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        XCTAssertNil(model.beginExport())
        try await waitFor { model.result != nil }
        let ticket = try XCTUnwrap(model.beginExport())
        XCTAssertNil(model.beginExport())
        model.configure(client: client, scope: scope, sweeps: try sweeps(1...5))
        XCTAssertTrue(model.finishExport(ticket, to: destination))
        let saved = try Data(contentsOf: destination)
        XCTAssertEqual(saved, try PlatformSweepAverageExportArtifact(data: value).canonicalData())
        XCTAssertFalse(model.finishExport(ticket, to: destination))
        let cancelled = try XCTUnwrap(model.beginExport())
        XCTAssertFalse(model.finishExport(cancelled, to: nil))
        XCTAssertFalse(model.exporting); XCTAssertNotNil(model.result)
        let stale = try XCTUnwrap(model.beginExport())
        // An identical replacement result must not revive a dialog from the previous revision.
        model.configure(client: client, scope: scope, sweeps: try sweeps())
        model.compare(); try await waitFor { model.result != nil }
        XCTAssertEqual(model.result, value)
        XCTAssertFalse(model.finishExport(stale, to: destination))
        XCTAssertEqual(try Data(contentsOf: destination), saved)
        let parameterTicket = try XCTUnwrap(model.beginExport())
        model.minimumBinsText = "1"
        XCTAssertFalse(model.finishExport(parameterTicket, to: destination))
        model.minimumBinsText = "2"; model.compare(); try await waitFor { model.result != nil }
        let closing = try XCTUnwrap(model.beginExport())
        model.clear()
        XCTAssertFalse(model.finishExport(closing, to: destination))
        model.pin(); model.compare(); try await waitFor { model.result != nil }
        let changedScope = try XCTUnwrap(model.beginExport())
        model.reset()
        XCTAssertFalse(model.finishExport(changedScope, to: destination))
        XCTAssertEqual(try Data(contentsOf: destination), saved)
    }
    @MainActor func testExportWriteFailureKeepsCompletedResultAndDestination() async throws {
        let client = try api(), value = try exportableResult()
        let model = SweepAverageModel(fetch: { _, _, _, _, _, _ in value })
        model.configure(client: client, scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        try await waitFor { model.result != nil }
        let ticket = try XCTUnwrap(model.beginExport())
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathComponent("analysis.json")
        XCTAssertFalse(model.finishExport(ticket, to: missing))
        XCTAssertFalse(FileManager.default.fileExists(atPath: missing.path))
        XCTAssertNotNil(model.errorText); XCTAssertNotNil(model.result); XCTAssertFalse(model.exporting)
        let retry = try XCTUnwrap(model.beginExport())
        XCTAssertFalse(model.finishExport(retry, to: URL(string: "https://example.test/analysis.json")))
        XCTAssertNotNil(model.result)
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
        for reason in ["count", "clear", "pin", "threshold", "bins", "server", "session", "stream", "client", "disconnect"] {
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
            case "threshold": model.minimumIncreaseText = "7"
            case "bins": model.minimumBinsText = "3"
            case "disconnect": model.reset()
            default:
                model.configure(client: reason == "client" ? try api() : client,
                    scope: .init(server: reason == "server" ? "other" : "local", session: reason == "session" ? "other" : "s", stream: reason == "stream" ? "other" : "tiny"), sweeps: try sweeps())
            }
            deferred.release(try decode(detectionBody())); await Task.yield()
            XCTAssertNil(model.result, reason); XCTAssertFalse(model.pending, reason)
            if ["pin", "threshold", "bins"].contains(reason) { XCTAssertEqual(model.sources.count, 3, reason) }
            else { XCTAssertTrue(model.sources.isEmpty, reason) }
        }
    }

    @MainActor func testInvalidDetectorFieldsNeverRequestAndKeepPins() throws {
        Stub.handler = { _ in XCTFail("Invalid parameters must not request") }
        let model = SweepAverageModel(); model.configure(client: try api(), scope: scope, sweeps: try sweeps()); model.pin()
        for (threshold, bins) in [("-1", "2"), ("121", "2"), ("nan", "2"), ("inf", "2"), ("", "2"),
                                  ("6", "0"), ("6", "16385"), ("6", "2.5"), ("6", "nan"), ("6", "")] {
            model.minimumIncreaseText = threshold; model.minimumBinsText = bins; model.compare()
            XCTAssertFalse(model.pending); XCTAssertNotNil(model.errorText); XCTAssertNil(model.result)
            XCTAssertEqual(model.sources.map(\.id), ["w1", "w2", "w3"])
        }
    }

    @MainActor func testParameterEditInvalidatesOldResponseAndNewResultWithoutClearingPin() async throws {
        let deferred = Deferred(), model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
            try await deferred.fetch(refs, target, threshold, bins)
        })
        model.configure(client: try api(), scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        try await waitFor { deferred.requests.count == 1 }
        model.minimumIncreaseText = "7"; model.minimumBinsText = "3"; model.compare()
        try await waitFor { deferred.requests.count == 2 }
        XCTAssertEqual(deferred.parameters[0].0, 6); XCTAssertEqual(deferred.parameters[0].1, 2)
        XCTAssertEqual(deferred.parameters[1].0, 7); XCTAssertEqual(deferred.parameters[1].1, 3)
        deferred.release(try decode(detectionBody())); await Task.yield()
        XCTAssertTrue(model.pending); XCTAssertNil(model.result)
        var current = detectionBody(); current["detection"] = ["minimum_increase_db": 7, "minimum_bins": 3, "regions": [], "count": 0]
        deferred.release(try decode(current)); try await waitFor { model.result != nil }
        model.minimumIncreaseText = "8"
        XCTAssertNil(model.result); XCTAssertNil(model.errorText); XCTAssertEqual(model.sources.count, 3)
    }

    @MainActor func testDetectorRejectsResponseWithDifferentValidParameters() async throws {
        let deferred = Deferred(), model = SweepAverageModel(fetch: { _, _, refs, target, threshold, bins in
            try await deferred.fetch(refs, target, threshold, bins)
        })
        model.configure(client: try api(), scope: scope, sweeps: try sweeps()); model.pin(); model.compare()
        try await waitFor { deferred.requests.count == 1 }
        var current = detectionBody(); current["detection"] = ["minimum_increase_db": 7, "minimum_bins": 2, "regions": [], "count": 0]
        deferred.release(try decode(current)); try await waitFor { !model.pending }
        XCTAssertNil(model.result); XCTAssertNotNil(model.errorText); XCTAssertEqual(model.sources.count, 3)
    }

    @MainActor func testCanonicallyEquivalentScopeNamesStillInvalidatePin() throws {
        let composed = "é", decomposed = "e\u{301}", client = try api(), model = SweepAverageModel()
        let window: [PlatformSweep] = try (1...4).map { var value = raw($0); value["session_id"] = composed; return try decode(value) }
        model.configure(client: client, scope: .init(server: "local", session: composed, stream: "tiny"), sweeps: window); model.pin()
        XCTAssertEqual(model.sources.count, 3)
        model.configure(client: client, scope: .init(server: "local", session: decomposed, stream: "tiny"), sweeps: window)
        XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.target); XCTAssertEqual(model.receivedCount, 0)
    }

    @MainActor func testZeroWidthIDsPinAndDetectWithoutNormalization() async throws {
        for exact in ["\u{200B}", "\u{FEFF}"] {
            for targetIsExact in [false, true] {
                let ids = targetIsExact ? ["w1", "w2", "w3", exact] : [exact, "w2", "w3", "w4"]
                let window: [PlatformSweep] = try (1...4).map { n in
                    var value = raw(n); value["id"] = ids[n - 1]
                    value["session_id"] = exact; value["stream_id"] = exact
                    return try decode(value)
                }
                var comparison = body(), reference = body()["reference"] as! [String: Any], target = body()["target"] as! [String: Any]
                var sources = reference["sources"] as! [[String: Any]]
                for index in sources.indices { sources[index]["id"] = ids[index] }
                reference["sources"] = sources; reference["source_ids"] = Array(ids.prefix(3))
                target["id"] = ids[3]; comparison["target"] = target; comparison["reference"] = reference
                comparison["session_id"] = exact; comparison["stream_id"] = exact
                Stub.handler = { $0.respond(self.detectionBody(comparison)) }
                let model = SweepAverageModel()
                model.configure(client: try api(), scope: .init(server: "local", session: exact, stream: exact), sweeps: window)
                model.pin(); XCTAssertNil(model.errorText); XCTAssertEqual(model.sources.count, 3)
                XCTAssertEqual(model.sources.map { Array($0.id.utf8) }, ids.prefix(3).map { Array($0.utf8) })
                model.compare(); try await waitFor { !model.pending }
                XCTAssertNil(model.errorText); XCTAssertNotNil(model.result)
                XCTAssertEqual(model.result.map { Array($0.comparison.target.id.utf8) }, Array(ids[3].utf8))
                XCTAssertEqual(model.result.map { Array($0.comparison.sessionId.utf8) }, Array(exact.utf8))
            }
        }
    }

    @MainActor func testEveryPythonWhitespaceOnlySourceAndScopeRejectsPin() throws {
        let scalars = Array(0x0009...0x000D) + Array(0x001C...0x0020) + [0x0085, 0x00A0, 0x1680]
            + Array(0x2000...0x200A) + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
        XCTAssertEqual(scalars.count, 29)
        Stub.handler = { _ in XCTFail("Whitespace-only IDs must not request") }
        for blank in [""] + scalars.map({ String(UnicodeScalar($0)!) }) {
            for field in ["session", "stream", "source", "target"] {
                let session = field == "session" ? blank : "s", stream = field == "stream" ? blank : "tiny"
                let window: [PlatformSweep] = try (1...4).map { n in
                    var value = raw(n); value["session_id"] = session; value["stream_id"] = stream
                    if (field == "source" && n == 1) || (field == "target" && n == 4) { value["id"] = blank }
                    return try decode(value)
                }
                let model = SweepAverageModel()
                model.configure(client: try api(), scope: .init(server: "local", session: session, stream: stream), sweeps: window)
                model.pin(); model.compare()
                XCTAssertNotNil(model.errorText, "\(field): \(Array(blank.utf8))")
                XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.result); XCTAssertFalse(model.pending)
            }
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
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.comparison.points, 101)
        XCTAssertEqual(model.result?.comparison.maxIncrease.frequencyHz, 103_000_000)
        XCTAssertEqual(model.result?.detection.count, 0)
    }

    @MainActor func testActualHTTPNativeChangeDetectionAndParameterInvalidation() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_AVERAGE_DETECTION_SMOKE_BASE_URL"] else {
            throw XCTSkip("Set ORI_AVERAGE_DETECTION_SMOKE_BASE_URL to synthetic detector fixture")
        }
        let remote = RemoteDualModel(); remote.serverURL = base; remote.connect(startPolling: false)
        defer { remote.disconnect() }
        try await waitFor { remote.sweepAverage.receivedCount >= 4 }
        let model = remote.sweepAverage; model.pin()
        XCTAssertEqual(model.sources.map(\.id), ["one-sweep-1", "one-sweep-2", "one-sweep-3"])
        let rawTrace = remote.current, revision = remote.waterfallRevision
        model.compare(); try await waitFor { !model.pending }
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.detection.regions.map(\.startIndex), [30, 69])
        XCTAssertEqual(model.result?.detection.regions.map(\.endIndex), [33, 70])
        XCTAssertEqual(model.result?.detection.regions.map(\.peakIndex), [31, 70])
        XCTAssertEqual(model.result?.detection.regions.map(\.peakIncreaseDb), [12, 9])
        await remote.refresh()
        XCTAssertEqual(model.result?.detection.count, 2); XCTAssertEqual(model.sources.count, 3)
        model.minimumIncreaseText = "12"
        XCTAssertNil(model.result); XCTAssertEqual(model.sources.count, 3)
        model.compare(); try await waitFor { !model.pending }
        XCTAssertNil(model.errorText); XCTAssertEqual(model.result?.detection.regions.map(\.startIndex), [31])
        XCTAssertEqual(model.result?.detection.regions.map(\.endIndex), [32])
        XCTAssertEqual(model.result?.detection.regions.map(\.peakIndex), [31])
        XCTAssertEqual(remote.current, rawTrace); XCTAssertEqual(remote.waterfallRevision, revision)
        remote.disconnect(); XCTAssertNil(model.result); XCTAssertTrue(model.sources.isEmpty); XCTAssertNil(model.target)
    }
}
