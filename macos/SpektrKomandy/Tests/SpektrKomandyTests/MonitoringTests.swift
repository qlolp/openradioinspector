import Foundation
import XCTest
@testable import SpektrKomandy

final class MonitoringTests: XCTestCase {
    func testIntegerHzAndNestedMetadataRoundTripWithoutLosingPrecision() throws {
        let bytes = Data("{\"frequency_hz\":9007199254740991,\"items\":[true,null,\"Объект\"]}".utf8)
        let value = try JSONDecoder().decode(MonitoringValue.self, from: bytes)
        XCTAssertEqual(value["frequency_hz"], .integer(9007199254740991))
        XCTAssertEqual(value["items"].items, [.boolean(true), .null, .string("Объект")])
        XCTAssertEqual(try JSONDecoder().decode(MonitoringValue.self, from: JSONEncoder().encode(value)), value)
    }
    func testUnknownFieldsAndEmptyListsRemainReadable() throws {
        let value = try JSONDecoder().decode(MonitoringValue.self, from: Data("{\"items\":[],\"next_cursor\":null,\"future\":{\"version\":2}}".utf8))
        XCTAssertTrue(value["items"].items.isEmpty)
        XCTAssertEqual(value["future"]["version"], .integer(2))
        XCTAssertFalse(value.pretty.isEmpty)
    }
}
