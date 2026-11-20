import Foundation
import XCTest
@testable import SpektrKomandy

final class UTCMicrosecondParsingTests: XCTestCase {
    func testExactUTCInstantsAcrossEpochAndGregorianRange() {
        let examples: [(String, Int64)] = [
            ("1970-01-01T00:00:00Z", 0),
            ("1969-12-31T23:59:59.999999Z", -1),
            ("2000-02-29T12:34:56.123456Z", 951827696123456),
            ("0001-01-01T00:00:00Z", -62135596800000000),
            ("9999-12-31T23:59:59.999999Z", 253402300799999999)
        ]
        for (source, expected) in examples {
            XCTAssertEqual(PlatformUTCInstant.microseconds(source), expected, source)
        }
    }

    func testFractionalPrecisionAndEquivalentUTCSpelling() {
        let examples: [(String, Int64)] = [
            ("1970-01-01T00:00:00.1Z", 100000),
            ("1970-01-01T00:00:00.000001Z", 1),
            ("1970-01-01T00:00:00.001020+00:00", 1020),
            ("1970-01-01T00:00:01+00:00", 1000000)
        ]
        for (source, expected) in examples {
            XCTAssertEqual(PlatformUTCInstant.microseconds(source), expected, source)
        }
    }

    func testInvalidGregorianDatesOffsetsAndLossyFractionsAreRefused() {
        for source in [
            "1900-02-29T00:00:00Z", "2100-02-29T00:00:00Z",
            "2026-02-30T00:00:00Z", "0000-01-01T00:00:00Z",
            "2026-13-01T00:00:00Z", "2026-01-00T00:00:00Z",
            "2026-04-31T00:00:00Z", "2026-01-01T24:00:00Z",
            "2026-01-01T00:60:00Z", "2026-01-01T00:00:60Z",
            "2026-01-01T00:00:00.0000001Z", "2026-01-01T00:00:00.Z",
            "2026-01-01T00:00:00+01:00", "2026-01-01T00:00:00",
            "2026-01-01 00:00:00Z", "2026-01-01T00:00:00z",
            "2026-01-01T00:00:00Z\n", "", "２０２６-01-01T00:00:00Z"
        ] {
            XCTAssertNil(PlatformUTCInstant.microseconds(source), source)
        }
    }

    func testIntervalConsumersPreserveOneMicrosecondAndRejectReversal() throws {
        let decoder = JSONDecoder()
        let interval = try decoder.decode(PlatformInterval.self, from: Data(
            #"{"startedAt":"1969-12-31T23:59:59.999999Z","endedAt":"1970-01-01T00:00:00Z"}"#.utf8))
        let value = try XCTUnwrap(averageInterval(interval))
        XCTAssertEqual(value.0, -1)
        XCTAssertEqual(value.1, 0)
        let reversed = try decoder.decode(PlatformInterval.self, from: Data(
            #"{"startedAt":"1970-01-01T00:00:00.000002Z","endedAt":"1970-01-01T00:00:00.000001Z"}"#.utf8))
        XCTAssertNil(averageInterval(reversed))
    }
}
