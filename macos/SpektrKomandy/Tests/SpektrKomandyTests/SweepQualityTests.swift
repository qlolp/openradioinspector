import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Foundation
import XCTest
@testable import SpektrKomandy

final class SweepQualityTests: XCTestCase {
    typealias Stub = SweepThresholdTests.Stub
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
    func client() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/api/v1", session: URLSession(configuration: config))
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    func testInclusiveBoundariesAndFrozenSource() throws {
        let result = try decode(fixture(), as: PlatformSweepQuality.self)
        try result.validate(source: source())
        XCTAssertEqual(result.flags, ["first_edge_jump", "level_threshold"])
        XCTAssertTrue(PlatformSweepQuality.Parameters.valid(edge: 200, high: -200))
        XCTAssertTrue(PlatformSweepQuality.Parameters.valid(edge: 0.001, high: 100))
        for edge in [0, -1, 200.01, Double.infinity, Double.nan] {
            XCTAssertFalse(PlatformSweepQuality.Parameters.valid(edge: edge, high: 0))
        }
    }
    func testMalformedSummariesFailClosed() throws {
        for (key, value) in [("points", 1 as Any), ("points", 16385), ("peak_index", 3), ("peak_frequency_hz", -1),
            ("high_level_count", 4), ("high_level_count", 0), ("hardware_diagnosis", true), ("raw_modified", true),
            ("flags", ["level_threshold", "first_edge_jump"]), ("assessment", "no_flags"), ("method", "diagnosis")] {
            var object = fixture(); object[key] = value
            XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate(), key)
        }
        var object = fixture(); var edges = object["edge_assessments"] as! [[String: Any]]
        edges[0]["flagged"] = false; object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        object = fixture(); object["stream_id"] = "other"
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate(source: source()))
    }
    func testSourceMismatchAndArbitrarySettings() throws {
        var object = fixture()
        var sweep = object["sweep"] as! [String: Any]
        sweep["settings"] = ["array", NSNull(), true, 2] as [Any]
        object["sweep"] = sweep
        let arbitrary = try decode(object, as: PlatformSweepQuality.self)
        try arbitrary.validate()
        XCTAssertThrowsError(try arbitrary.validate(source: source()))
        object = fixture(); object["peak_frequency_hz"] = 101
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate(source: source()))
        object = fixture(); var edges = object["edge_assessments"] as! [[String: Any]]
        edges[1]["neighbor_level_dbm"] = -21; edges[1]["delta_db"] = -9
        object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate(source: source()))
    }
    func testNoFlagsIsNotHardwareDiagnosis() throws {
        var object = fixture()
        object["parameters"] = ["edge_margin_db": 21, "high_level_dbm": 1]
        object["high_level_count"] = 0; object["flags"] = [] as [String]; object["assessment"] = "no_flags"
        var edges = object["edge_assessments"] as! [[String: Any]]
        edges[0]["flagged"] = false; object["edge_assessments"] = edges
        let value = try decode(object, as: PlatformSweepQuality.self)
        try value.validate(source: source()); XCTAssertFalse(value.hardwareDiagnosis); XCTAssertFalse(value.rawModified)
    }
    func testKnownSamplesConstrainStandaloneSummary() throws {
        // A three-point report reveals every level, so a locally plausible count is still invalid.
        var object = fixture(); object["high_level_count"] = 2
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        object = fixture(); var edges = object["edge_assessments"] as! [[String: Any]]
        edges[1]["neighbor_level_dbm"] = -21; edges[1]["delta_db"] = -9
        object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        // Integer strictly increasing bins need enough frequency space for missing samples.
        object = fixture(); edges = object["edge_assessments"] as! [[String: Any]]
        edges[1]["frequency_hz"] = 101; object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        // Endpoint frequencies cannot coincide; an interior maximum must be inside them.
        object = fixture(); edges = object["edge_assessments"] as! [[String: Any]]
        edges[1]["frequency_hz"] = 100; object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        object = fixture(); object["points"] = 5; object["peak_index"] = 2; object["peak_frequency_hz"] = 400
        object["peak_level_dbm"] = 1; object["high_level_count"] = 2
        edges = object["edge_assessments"] as! [[String: Any]]; edges[1]["index"] = 4; edges[1]["neighbor_index"] = 3
        object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
        // First-max ties are enforced even when some interior samples are not returned.
        object["peak_frequency_hz"] = 200; object["peak_level_dbm"] = 0
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
    }
    func testTwoPointSharedNeighborConsistency() throws {
        var object = fixture(); object["points"] = 2
        var edges = object["edge_assessments"] as! [[String: Any]]
        edges[0]["neighbor_level_dbm"] = -30; edges[0]["delta_db"] = 30
        edges[1]["index"] = 1; edges[1]["neighbor_index"] = 0
        edges[1]["neighbor_level_dbm"] = 0; edges[1]["delta_db"] = -30
        object["edge_assessments"] = edges
        try decode(object, as: PlatformSweepQuality.self).validate()
        edges[1]["neighbor_level_dbm"] = -1; edges[1]["delta_db"] = -29
        object["edge_assessments"] = edges
        XCTAssertThrowsError(try decode(object, as: PlatformSweepQuality.self).validate())
    }
    func testExactWireBodyAndEcho() async throws {
        Stub.handler = { stub in
            let request = try! bufferedTestRequest(stub.request)
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/api/v1/sessions/s/sweep-quality")
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            XCTAssertEqual(Set(body.keys), ["sweep_id", "edge_margin_db", "high_level_dbm"])
            XCTAssertEqual(body["sweep_id"] as? String, "w")
            XCTAssertEqual(body["edge_margin_db"] as? Double, 20)
            XCTAssertEqual(body["high_level_dbm"] as? Double, 0)
            stub.respond(self.fixture())
        }
        let value = try await client().sweepQuality(sessionID: "s", sweepID: "w")
        try value.validate(source: source())
    }
    func testMalformedWireAndStaleEchoRejected() async throws {
        for (key, value) in [("points", true as Any), ("peak_index", 0.5), ("unknown", 1), ("session_id", "other"), ("peak_level_dbm", true)] {
            var object = fixture(); object[key] = value
            Stub.handler = { $0.respond(object) }
            do { _ = try await client().sweepQuality(sessionID: "s", sweepID: "w"); XCTFail(key) } catch { }
        }
    }
    func testInvalidInputsNeverReachNetwork() async throws {
        Stub.handler = { _ in XCTFail("Invalid input must not reach server") }
        for (id, edge, high) in [(String(repeating: "a", count: 129), 20.0, 0.0), ("", 20, 0), ("w", 0, 0), ("w", 20, 101), ("w", .nan, 0), ("w", 20, -.infinity)] {
            do { _ = try await client().sweepQuality(sessionID: "s", sweepID: id, edgeMarginDb: edge, highLevelDbm: high); XCTFail() } catch { }
        }
    }
}
