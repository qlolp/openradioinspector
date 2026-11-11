import Foundation
import XCTest
import ORIContracts
@testable import ORIMeasurementStream

final class WireTests: XCTestCase {
    func fixtures() throws -> JSONObject {
        let root=URL(fileURLWithPath:#filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try StrictJSON.parse(Data(contentsOf:root.appendingPathComponent("fixtures/stream-v1.json"))).requireObject()
    }
    func text(_ object:JSONObject,_ key:String) throws -> String {try XCTUnwrap(object[key]).requireString()}
    func bytes(_ hex:String) -> Data {
        let chars=Array(hex.utf8)
        return Data(stride(from:0,to:chars.count,by:2).map {UInt8(String(decoding:chars[$0..<$0+2],as:UTF8.self),radix:16)!})
    }
    func bits(_ value:Double) -> String {String(format:"%016llx",value.bitPattern)}
    func testSharedGoldenFramesMetadataAndExactBits() throws {
        for item in try XCTUnwrap(fixtures()["valid"]).requireArray() {
            let fixture=try item.requireObject(), name=try text(fixture,"name")
            let frame=try MeasurementStream.decode(bytes(text(fixture,"wire_hex")))
            XCTAssertEqual(frame.protocolVersion,1,name)
            XCTAssertEqual(String(frame.sequence),try text(fixture,"sequence"),name)
            XCTAssertEqual(String(data:frame.envelopeJSON,encoding:.utf8),try text(fixture,"metadata_json"),name)
            XCTAssertEqual(frame.envelope,try StrictJSON.parse(Data(text(fixture,"metadata_json").utf8)),name)
            let typed=try XCTUnwrap(fixture["typed"]).requireBool()
            XCTAssertEqual(frame.spectrum != nil,typed,name)
            if let spectrum=frame.spectrum {
                XCTAssertEqual(spectrum.frequenciesHz.map(String.init),try XCTUnwrap(fixture["frequencies_hz"]).requireArray().map {try $0.requireString()},name)
                XCTAssertEqual(spectrum.levels.map(bits),try XCTUnwrap(fixture["level_bits"]).requireArray().map {try $0.requireString()},name)
            }
            let original=try StrictJSON.parse(Data(text(fixture,"input_json").utf8))
            let restored=try frame.intoEnvelope()
            XCTAssertEqual(restored,original,name)
        }
    }
    func testInvalidMalformedAndAmbiguousFrames() throws {
        for item in try XCTUnwrap(fixtures()["invalid"]).requireArray() {
            let fixture=try item.requireObject(),name=try text(fixture,"name")
            XCTAssertThrowsError(try MeasurementStream.decode(bytes(text(fixture,"wire_hex"))),name)
        }
    }
    func testAllPrefixesAndLoweringOnlyLimits() throws {
        let valid=try XCTUnwrap(fixtures()["valid"]).requireArray()
        let first=try valid[0].requireObject(), wire=try bytes(text(first,"wire_hex"))
        for end in 0..<wire.count {XCTAssertThrowsError(try MeasurementStream.decode(wire.prefix(end)),"prefix \(end)")}
        XCTAssertThrowsError(try MeasurementStream.decode(wire,limits:.init(maxFrameBytes:wire.count-1,maxPoints:MeasurementStream.maxPoints)))
        XCTAssertThrowsError(try MeasurementStream.decode(wire,limits:.init(maxPoints:3)))
        XCTAssertThrowsError(try MeasurementStream.decode(wire,limits:.init(maxFrameBytes:MeasurementStream.maxFrameBytes+1)))
        XCTAssertThrowsError(try MeasurementStream.decode(wire,limits:.init(maxPoints:MeasurementStream.maxPoints+1)))
        for item in valid {
            let fixture=try item.requireObject(), name=try text(fixture,"name")
            if name=="integer-level-kind-generic" {XCTAssertThrowsError(try MeasurementStream.decode(bytes(text(fixture,"wire_hex")),limits:.init(maxPoints:2)))}
            if name=="unknown-event-generic" {XCTAssertNoThrow(try MeasurementStream.decode(bytes(text(fixture,"wire_hex")),limits:.init(maxPoints:0)))}
        }
    }
}
