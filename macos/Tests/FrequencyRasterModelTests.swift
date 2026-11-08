import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class FrequencyRasterModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, [String], String)] = []
        var continuations: [CheckedContinuation<PlatformFrequencyRounding, Error>] = []
        func fetch(_ session: String, _ ids: [String], _ draft: String) async throws -> PlatformFrequencyRounding {
            requests.append((session, ids, draft))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformFrequencyRounding) { continuations.removeFirst().resume(returning: value) }
        func fail(_ error: Error) { continuations.removeFirst().resume(throwing: error) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "invalid_request", "message": "synthetic"]]
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
    private func candidate(_ id: String = "c1", _ hz: Int64 = 101_234_567, session: String = "s") throws -> PlatformCandidate {
        try decode(["id": id, "session_id": session, "source_stream_id": "tiny",
                    "interval": ["started_at": "2026-11-08T01:00:04.000001Z", "ended_at": "2026-11-08T01:00:04.000002Z"],
                    "center_frequency_hz": hz, "level_unit": "dBm", "state": "new"],
                   as: PlatformCandidate.self)
    }
    private func tableJSON() -> String {
        """
        {"format":"openradioinspector-frequency-raster","format_version":1,"name":"Synthetic raster","frequency_unit":"Hz","entries":[{"name":"band","start_hz":100000000,"stop_hz":110000000,"grid_step_hz":100000}]}
        """
    }
    private func result(session: String = "s", ids: [String] = ["c1", "c2"]) throws -> PlatformFrequencyRounding {
        try decode(["session_id": session, "order": "request", "table": [
            "format": "openradioinspector-frequency-raster", "format_version": 1, "name": "Synthetic raster",
            "frequency_unit": "Hz",
            "entries": [["name": "band", "start_hz": 100_000_000, "stop_hz": 110_000_000, "grid_step_hz": 100_000]],
        ], "count": ids.count,
            "matched_count": 1, "unmatched_count": ids.count - 1,
            "items": ids.enumerated().map { index, id -> [String: Any] in
                index == 0
                    ? ["candidate_id": id, "frequency_hz": 101_234_567, "matched": true,
                       "rounded_frequency_hz": 101_200_000, "delta_hz": -34_567, "grid_step_hz": 100_000, "entry_name": "band"]
                    : ["candidate_id": id, "frequency_hz": 50_000_000, "matched": false,
                       "rounded_frequency_hz": NSNull(), "delta_hz": NSNull(), "grid_step_hz": NSNull(), "entry_name": NSNull()]
            }], as: PlatformFrequencyRounding.self)
    }
    @MainActor private var scope: FrequencyRasterModel.Scope { .init(server: "local", session: "s") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for raster state")
    }
    /// The model starts its request in a detached task: wait until fetch is registered.
    /// ``requests`` accumulate; ``continuations`` are consumed by release/fail, so a
    /// fresh registration is exactly one pending continuation.
    @MainActor private func waitForRequest(_ deferred: Deferred, count: Int = 1) async throws {
        try await waitFor { deferred.requests.count >= count && deferred.continuations.count >= 1 }
    }
    @MainActor func testSelectionGatesAnalyzeAndForeignCandidatesAreDropped() throws {
        Stub.handler = { _ in XCTFail("Invalid input reached network") }
        let model = FrequencyRasterModel(), client = try api()
        XCTAssertFalse(model.canAnalyze)
        model.configure(client: client, scope: scope,
                        candidates: [try candidate(), try candidate("foreign", 50_000_000, session: "other")])
        XCTAssertEqual(model.candidates.map(\.id), ["c1"], "foreign candidates never appear")
        model.toggle("c1"); model.draft = tableJSON()
        XCTAssertTrue(model.canAnalyze)
        model.toggle("c1")
        XCTAssertFalse(model.canAnalyze, "no selection disables analyze")
        model.draft = ""
        XCTAssertFalse(model.canAnalyze, "no draft disables analyze")
    }

    @MainActor func testRequestSubmitsRawDraftAndKeepsResultFrozen() async throws {
        let deferred = Deferred()
        let model = FrequencyRasterModel(timeoutNanoseconds: 60_000_000_000,
                                         fetch: { _, session, ids, draft in try await deferred.fetch(session, ids, draft) })
        let client = try api()
        model.configure(client: client, scope: scope,
                        candidates: [try candidate(), try candidate("c2", 50_000_000)])
        model.toggle("c1"); model.toggle("c2")
        model.draft = tableJSON()
        model.analyze()
        XCTAssertTrue(model.pending)
        model.analyze()
        try await waitForRequest(deferred)
        XCTAssertEqual(deferred.requests.count, 1, "single flight")
        XCTAssertEqual(deferred.requests[0].1, ["c1", "c2"], "request order follows the archive listing")
        XCTAssertEqual(deferred.requests[0].2, tableJSON(), "raw draft is never rewritten")
        deferred.release(try result())
        try await waitFor { model.result != nil }
        XCTAssertFalse(model.pending)
        XCTAssertEqual(model.result?.matchedCount, 1)
        // Polling updates the listing but never clears a frozen result.
        model.configure(client: client, scope: scope,
                        candidates: [try candidate(), try candidate("c2", 50_000_000), try candidate("c3")])
        XCTAssertNotNil(model.result)
        model.draft = model.draft + " "
        XCTAssertNil(model.result, "editing the draft clears the result")
    }

    @MainActor func testForgedResultAndFailuresNeverFreeze() async throws {
        let deferred = Deferred()
        let model = FrequencyRasterModel(timeoutNanoseconds: 60_000_000_000,
                                         fetch: { _, session, ids, draft in try await deferred.fetch(session, ids, draft) })
        let client = try api()
        model.configure(client: client, scope: scope, candidates: [try candidate(), try candidate("c2", 50_000_000)])
        model.toggle("c1"); model.toggle("c2")
        model.draft = tableJSON()
        model.analyze()
        let broken = try decode([
            "session_id": "s", "order": "request",
            "table": ["format": "openradioinspector-frequency-raster", "format_version": 1,
                      "name": "other", "frequency_unit": "Hz",
                      "entries": [["name": "band", "start_hz": 100_000_000, "stop_hz": 110_000_000, "grid_step_hz": 100_000]]],
            "count": 2, "matched_count": 1, "unmatched_count": 1,
            "items": [["candidate_id": "c1", "frequency_hz": 101_234_567, "matched": true,
                       "rounded_frequency_hz": 101_200_000, "delta_hz": -34_567, "grid_step_hz": 100_000, "entry_name": "band"],
                      ["candidate_id": "c2", "frequency_hz": 50_000_000, "matched": false,
                       "rounded_frequency_hz": NSNull(), "delta_hz": NSNull(), "grid_step_hz": NSNull(), "entry_name": NSNull()]],
        ], as: PlatformFrequencyRounding.self)
        try await waitForRequest(deferred)
        deferred.release(broken)
        try await waitFor { !model.pending }
        XCTAssertNil(model.result, "a table echo that differs from the draft is never accepted")
        XCTAssertNotNil(model.errorText)
        model.analyze()
        try await waitForRequest(deferred, count: 2)
        deferred.fail(PlatformError.http(status: 404, code: "candidate_not_found", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertNil(model.result)
        XCTAssertTrue(model.errorText?.contains("404") == true)
    }

    @MainActor func testTemplateCoversChosenCandidatesAndIsNonNormative() throws {
        let model = FrequencyRasterModel()
        model.configure(client: try api(), scope: scope,
                        candidates: [try candidate(), try candidate("c2", 109_000_000)])
        model.toggle("c1")
        XCTAssertTrue(model.canMakeTemplate)
        model.makeTemplate()
        XCTAssertTrue(model.draft.contains("НЕНОРМАТИВНЫЙ"))
        let object = try JSONSerialization.jsonObject(with: Data(model.draft.utf8)) as! [String: Any]
        let entries = object["entries"] as! [[String: Any]]
        XCTAssertEqual(entries[0]["grid_step_hz"] as? Int64 ?? Int64(entries[0]["grid_step_hz"] as! Int), 100_000)
        XCTAssertEqual(entries[0]["start_hz"] as? Int64 ?? Int64(entries[0]["start_hz"] as! Int), 101_234_567 - 1_000_000)
        XCTAssertEqual(entries[0]["stop_hz"] as? Int64 ?? Int64(entries[0]["stop_hz"] as! Int), 101_234_567 + 1_000_000)
    }
}
