import XCTest
@testable import SpektrKomandy

final class SpectrumPresentationTests: XCTestCase {
    private func fixtureData() throws -> Data {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try Data(contentsOf: root.appendingPathComponent("tests/fixtures/spectrum-presentation-v1.json"))
    }
    private func fixture() throws -> [String: Any] { try JSONSerialization.jsonObject(with: fixtureData()) as! [String: Any] }
    private func raw() throws -> [[String: Any]] { try (fixture()["frame"] as! [String: Any])["sweeps"] as! [[String: Any]] }
    private func sweep(_ raw: [String: Any]) throws -> PlatformSweep {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweep.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    private var scope: PlatformSpectrumPresentation.Scope { .init(server: "http://127.0.0.1:9999", session: "session-A", stream: "stream-A") }
    func testSharedFixtureScalesAndExtremeFractions() throws {
        for item in try fixture()["scale_cases"] as! [[String: Any]] {
            let levels = item["levels"] as! [Double], scale = try XCTUnwrap(PlatformSpectrumScale.fit(levels))
            let lo = item["expected_lower"] as! Double, hi = item["expected_upper"] as! Double
            XCTAssertEqual(scale.lower, lo, accuracy: max(abs(lo) * 1e-14, 1e-12))
            XCTAssertEqual(scale.upper, hi, accuracy: max(abs(hi) * 1e-14, 1e-12))
            for level in levels { XCTAssertTrue(scale.fraction(level).isFinite); XCTAssertTrue((0...1).contains(scale.fraction(level))) }
        }
        let scale = PlatformSpectrumScale(lower: -1e-300, upper: 1e-300)!
        XCTAssertEqual(scale.fraction(.greatestFiniteMagnitude), 1)
        XCTAssertEqual(scale.fraction(-Double.greatestFiniteMagnitude), 0)
    }
    func testFrozenFrameScaleClippingAndResume() throws {
        let sweeps = try raw().map(sweep); var view = PlatformSpectrumPresentation()
        view.update(scope: scope, sweeps: sweeps); let original = view.scale
        XCTAssertEqual(view.scale!.lower, -104, accuracy: 1e-10)
        view.togglePause(); var changed = try raw()[1]; changed["levels"] = [-20, -10, 0]; changed["id"] = "new"
        view.update(scope: scope, sweeps: [try sweep(changed)])
        XCTAssertEqual(view.sweeps.map(\.id), sweeps.map(\.id)); XCTAssertEqual(view.scale, original)
        view.setBounds(lower: "-90", upper: "-70"); XCTAssertEqual(view.clippedCount, 3)
        XCTAssertEqual(view.sweeps.last?.levels, [-90, -70, -50]); view.fit(); XCTAssertEqual(view.scale, original)
        view.togglePause(); XCTAssertEqual(view.sweeps.last?.id, "new"); XCTAssertEqual(view.scale, original)
        XCTAssertEqual(view.clippedCount, 3)
    }
    func testManualRejectsMalformedAndKeepsRange() throws {
        var view = PlatformSpectrumPresentation(); view.update(scope: scope, sweeps: try raw().map(sweep)); let scale = view.scale
        for value in ["", " ", "NaN", "Infinity", "0x1p2", "-inf", "1e999", "\u{00a0}2", "2\u{feff}"] {
            view.setBounds(lower: value, upper: "100"); XCTAssertNotNil(view.error); XCTAssertEqual(view.scale, scale)
        }
        for pair in try fixture()["invalid_manual_bounds"] as! [[Double]] {
            view.setBounds(lower: String(pair[0]), upper: String(pair[1])); XCTAssertEqual(view.scale, scale)
        }
        view.setBounds(lower: " \t-1e2\r\n", upper: "-.5 "); XCTAssertNil(view.error)
        // JSONSerialization drops a leading U+FEFF inside strings on macOS;
        // JSONDecoder preserves the source bytes needed for this rejection test.
        struct ManualFixture: Decodable {
            struct Item: Decodable { let lower: String; let upper: String; let valid: Bool }
            let manual_cases: [Item]
        }
        for item in try JSONDecoder().decode(ManualFixture.self, from: fixtureData()).manual_cases {
            let previous = view.scale
            view.setBounds(lower: item.lower, upper: item.upper)
            XCTAssertEqual(view.error == nil, item.valid)
            if !item.valid { XCTAssertEqual(view.scale, previous) }
        }
    }
    func testCapacityContiguousSuffixResetsAndInvalidInput() throws {
        var view = PlatformSpectrumPresentation(); let source = try raw()[0]
        let many = try (0..<50).map { i -> PlatformSweep in var r = source; r["id"] = "id-\(i)"; return try sweep(r) }
        view.update(scope: scope, sweeps: many); XCTAssertEqual(view.sweeps.count, try fixture()["expected_capacity"] as? Int)
        view.togglePause(); var different = source; different["frequencies_hz"] = [1, 2, 3]
        view.update(scope: scope, sweeps: [many[0], try sweep(different), many[1]])
        // Latest grid is unchanged, so paused snapshot remains. On resume only contiguous suffix is shown.
        XCTAssertTrue(view.paused); view.togglePause(); XCTAssertEqual(view.sweeps.count, 1)
        view.togglePause(); view.update(scope: scope, sweeps: [try sweep(different)])
        XCTAssertFalse(view.paused); XCTAssertEqual(view.sweeps.last?.frequenciesHz, [1, 2, 3])
        for field in ["session_id", "stream_id", "level_unit", "id", "interval"] {
            var bad = source; bad[field] = field == "id" ? "" : "foreign"
            if field == "interval" { bad[field] = ["started_at": "invalid", "ended_at": "invalid"] }
            view.update(scope: scope, sweeps: [try sweep(bad)]); XCTAssertTrue(view.sweeps.isEmpty); XCTAssertNil(view.scale)
        }
        view.update(scope: scope, sweeps: many); view.togglePause(); view.update(scope: scope, sweeps: [])
        XCTAssertFalse(view.paused); XCTAssertNil(view.scale)
    }
    func testUnicodeScopeIdentityIsByteExact() throws {
        var view = PlatformSpectrumPresentation(); var r = try raw()[0]; r["session_id"] = "é"
        view.update(scope: .init(server: "é", session: "é", stream: "stream-A"), sweeps: [try sweep(r)])
        view.setBounds(lower: "-1", upper: "1"); view.togglePause()
        view.update(scope: .init(server: "e\u{301}", session: "é", stream: "stream-A"), sweeps: [try sweep(r)])
        XCTAssertFalse(view.paused); XCTAssertNotEqual(view.scale?.lower, -1)
        view.update(scope: .init(server: "é", session: "e\u{301}", stream: "stream-A"), sweeps: [try sweep(r)])
        XCTAssertTrue(view.sweeps.isEmpty)
    }
    func testSharedUTCIntervalsAndExactFrequencyLimit() throws {
        let fixture = try fixture(); var source = try raw()[0]; var view = PlatformSpectrumPresentation()
        for interval in fixture["valid_intervals"] as! [[String: Any]] {
            source["interval"] = interval; view.update(scope: scope, sweeps: [try sweep(source)])
            XCTAssertEqual(view.sweeps.count, 1)
        }
        for interval in fixture["invalid_intervals"] as! [[String: Any]] {
            source["interval"] = interval; view.update(scope: scope, sweeps: [try sweep(source)])
            XCTAssertTrue(view.sweeps.isEmpty, "\(interval)")
        }
        source = try raw()[0]
        let limit = (fixture["maximum_frequency_hz"] as! NSNumber).int64Value
        source["frequencies_hz"] = [0, limit, limit]
        view.update(scope: scope, sweeps: [try sweep(source)]); XCTAssertEqual(view.sweeps.count, 1)
        source["frequencies_hz"] = [0, limit, limit + 1]
        view.update(scope: scope, sweeps: [try sweep(source)]); XCTAssertTrue(view.sweeps.isEmpty)
        for scope in [PlatformSpectrumPresentation.Scope(server: "", session: "session-A", stream: "stream-A"), .init(server: "server", session: "", stream: "stream-A"), .init(server: "server", session: "session-A", stream: "")] {
            view.update(scope: scope, sweeps: try raw().map(sweep)); XCTAssertTrue(view.sweeps.isEmpty)
        }
    }

}
