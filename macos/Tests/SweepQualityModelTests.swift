import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SweepQualityModelTests: XCTestCase {
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
}
