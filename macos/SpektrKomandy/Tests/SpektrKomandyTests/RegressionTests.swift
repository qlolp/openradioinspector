import Foundation
import XCTest
@testable import SpektrKomandy

final class RegressionTests: XCTestCase {
    func testFrequencyOverflowReturnsNil() {
        XCTAssertNil(FrequencyText.parseMHz("100000000000000"))
        XCTAssertNil(FrequencyText.parseMHz("92233720368547.75808"))
        XCTAssertEqual(FrequencyText.parseMHz("92233720368547.75807"), Int64.max)
        XCTAssertEqual(FrequencyText.parseMHz("1300"), 130_000_000)
    }

    func testSessionsInSameSecondKeepIndependentFiles() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        for instrument in ["ardv1", "tinysa"] {
            let now = Date(timeIntervalSince1970: 1_700_000_000)
            let a = folder.appendingPathComponent(TraceClock.sessionFilename(instrument: instrument, date: now))
            let b = folder.appendingPathComponent(TraceClock.sessionFilename(instrument: instrument, date: now))
            XCTAssertNotEqual(a, b)
            try Data("first session".utf8).write(to: a, options: .atomic)
            try Data("second session".utf8).write(to: b, options: .atomic)
            XCTAssertEqual(try String(contentsOf: a, encoding: .utf8), "first session")
            XCTAssertEqual(try String(contentsOf: b, encoding: .utf8), "second session")
        }
    }
}
