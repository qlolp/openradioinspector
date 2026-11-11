import Foundation
import XCTest
@testable import SpektrKomandy

final class ModernMeasurementContractTests: XCTestCase {
    private struct Fixture: Decodable {
        let sweep: PlatformSweep
        let observation: PlatformObservation
    }

    func testSharedFixturePreservesRawUnitsIntegerHzUTF8AndFloat64Bits() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let fixture = try decoder.decode(Fixture.self, from: Data(contentsOf: root.appendingPathComponent("contracts/fixtures/measurement-v1.json")))
        XCTAssertEqual(Array(fixture.sweep.id.utf8), Array("fixture-sweep-α".utf8))
        XCTAssertEqual(Array(fixture.sweep.sessionId.utf8), Array("fixture-сессия".utf8))
        XCTAssertEqual(fixture.sweep.frequenciesHz, [100000001, 100000002, 100000003, 100000004])
        XCTAssertEqual(fixture.sweep.levels.map(\.bitPattern), [0xc059080000000000, 0x8000000000000000, 0xbff0000000000001, 0xc040600000000000])
        XCTAssertEqual(fixture.observation.levelUnit, "s_meter_count")
        XCTAssertEqual(fixture.observation.level, 137)
        XCTAssertEqual(fixture.sweep.interval.startedAt, "2026-11-11T15:00:00.123456+03:00")
        XCTAssertEqual(try PlatformTimestamp(fixture.sweep.interval.startedAt).utcMicroseconds, 1_794_398_400_123_456)
        XCTAssertEqual(try PlatformTimestamp(fixture.observation.interval.startedAt).utcMicroseconds, 1_794_398_400_123_456)
        XCTAssertEqual(try PlatformTimestamp(fixture.sweep.interval.startedAt).canonicalUTC, "2026-11-11T12:00:00.123456Z")
    }

    func testCanonicalUTCUsesProlepticGregorianCalendarAtContractBoundaries() throws {
        XCTAssertEqual(try PlatformTimestamp("1500-03-01T00:00:00.000001Z").canonicalUTC, "1500-03-01T00:00:00.000001Z")
        XCTAssertEqual(try PlatformTimestamp("0001-01-01T00:00:00Z").canonicalUTC, "0001-01-01T00:00:00.000000Z")
        XCTAssertThrowsError(try PlatformTimestamp("0001-01-01T00:00:00+01:00"))
        XCTAssertThrowsError(try PlatformTimestamp("9999-12-31T23:59:59-01:00"))
    }

    func testScopeHashingKeepsCanonicallyEquivalentUTF8IDsDistinct() {
        let first = PlatformSpectrumPresentation.Scope(server: "server", session: "é", stream: "stream")
        let second = PlatformSpectrumPresentation.Scope(server: "server", session: "e\u{301}", stream: "stream")
        XCTAssertEqual(Set([first, second]).count, 2)
    }

    func testUTCParsingRejectsNormalizationAndRetainsMicroseconds() throws {
        XCTAssertEqual(try PlatformTimestamp("1970-01-01T00:00:00.000001Z").utcMicroseconds, 1)
        XCTAssertEqual(try PlatformTimestamp("1969-12-31T23:59:59.999999Z").utcMicroseconds, -1)
        XCTAssertEqual(try PlatformTimestamp("2024-02-29T23:59:59.000002-01:00").canonicalUTC, "2024-03-01T00:59:59.000002Z")
        for value in ["2026-02-29T00:00:00Z", "2026-11-11T24:00:00Z", "2026-11-11T00:00:00.1234567Z", "2026-11-11T00:00:00", "2026-11-11T00:00:00+24:00"] {
            XCTAssertThrowsError(try PlatformTimestamp(value), value)
        }
    }
}
