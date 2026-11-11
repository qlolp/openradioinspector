import XCTest
import Foundation
import ORIContracts

final class ContractTests: XCTestCase {
    func testIntegerMinusZeroMatchesLegacyPythonSemantics() throws {
        let json = try StrictJSON.parse(Data(#"{"integer":-0,"float":-0.0}"#.utf8))
        XCTAssertEqual(json.object?["integer"], .integer("0"))
        XCTAssertEqual(json.object?["float"]?.double?.bitPattern, (-0.0).bitPattern)
    }
    func testRequiredNullableCursorKeepsExplicitNull() throws {
        let value = try StrictJSON.decode(SweepSummaryPage.self,from:Data(#"{"items":[],"next_cursor":null}"#.utf8))
        let round = try StrictJSON.parse(StrictJSON.encode(value))
        XCTAssertEqual(round.object?["next_cursor"], .null)
        XCTAssertThrowsError(try StrictJSON.decode(SweepSummaryPage.self,from:Data(#"{"items":[]}"#.utf8)))
    }
    private var fixture: Data { get throws { try Data(contentsOf: URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures/measurement-v1.json")) } }

    func testSharedGoldenPreservesFloat64IDsUTCAndUnits() throws {
        let json = try StrictJSON.parse(fixture)
        let sweep = try Sweep(json: try XCTUnwrap(json.object?["sweep"]))
        XCTAssertEqual(sweep.id, "fixture-sweep-α")
        XCTAssertEqual(sweep.session_id, "fixture-сессия")
        XCTAssertEqual(sweep.stream_id, "spectrum/one")
        XCTAssertEqual(sweep.frequencies_hz, [100000001,100000002,100000003,100000004])
        XCTAssertEqual(sweep.levels.map(\.bitPattern), [0xc059080000000000,0x8000000000000000,0xbff0000000000001,0xc040600000000000])
        XCTAssertEqual(sweep.interval.started_at.rawValue, "2026-11-11T12:00:00.123456Z")
        XCTAssertEqual(sweep.interval.ended_at.rawValue, "2026-11-11T12:00:00.223456Z")
        let round = try StrictJSON.decode(Sweep.self, from: StrictJSON.encode(sweep))
        XCTAssertEqual(round.levels.map(\.bitPattern), [0xc059080000000000,0x8000000000000000,0xbff0000000000001,0xc040600000000000])
        let observation = try ReceiverObservation(json: try XCTUnwrap(json.object?["observation"]))
        XCTAssertEqual(observation.stream_id,"receiver+one")
        XCTAssertEqual(observation.level,137)
        XCTAssertEqual(observation.level_unit,"s_meter_count")
        XCTAssertEqual(observation.metadata?["hardware_validated"],.bool(false))
    }

    func testUnknownNestedDataAndBigIntegersSurviveWithoutRounding() throws {
        let text = #"{"id":"x/%+?&=雪","session_id":"s","stream_id":"r","interval":{"started_at":"2026-11-11 12:00:00.000001","ended_at":"2026-11-11T12:00:01Z","unknown":{"v":-0.0}},"frequency_hz":9007199254740993,"mode":"FM","level_unit":"s_meter_count","metadata":{"big":18446744073709551616,"nested":{"v":-0.0}},"new_field":[1,{"x":2}]}"#
        let value = try StrictJSON.decode(ReceiverObservation.self,from:Data(text.utf8))
        XCTAssertEqual(value.id,"x/%+?&=雪")
        XCTAssertEqual(value.frequency_hz,9007199254740993)
        let round = try StrictJSON.parse(StrictJSON.encode(value))
        XCTAssertEqual(round.object?["metadata"]?.object?["big"],.integer("18446744073709551616"))
        XCTAssertEqual(round.object?["metadata"]?.object?["nested"]?.object?["v"]?.double?.bitPattern,0x8000000000000000)
        XCTAssertEqual(round.object?["new_field"]?.array?[1].object?["x"],.integer("2"))
        XCTAssertEqual(encodePathSegment(value.id),"x%2F%25%2B%3F%26%3D%E9%9B%AA")
        XCTAssertEqual(try operationPath("/sessions/{id}/streams/{stream_id}", parameters:["id":"雪/?","stream_id":"r+one"]),"/sessions/%E9%9B%AA%2F%3F/streams/r%2Bone")
    }

    func testStrictJSONRejectsAmbiguityInvalidNumbersAndSizeOverflow() throws {
        for bad in [#"{"x":1,"x":2}"#,#"{"x":{"a":1,"\u0061":2}}"#,#"[{"x":0,"x":1}]"#,#"{"x":NaN}"#,#"{"x":Infinity}"#,#"{"x":1e400}"#,#"{"x":1} trailing"#,"[1,]",""] {
            XCTAssertThrowsError(try StrictJSON.parse(Data(bad.utf8)),bad)
        }
        XCTAssertThrowsError(try StrictJSON.parse(Data([0xff])))
        let boundary = Data(#"{"雪":0}"#.utf8)
        XCTAssertNoThrow(try StrictJSON.parse(boundary,maxBytes:boundary.count))
        XCTAssertThrowsError(try StrictJSON.parse(boundary,maxBytes:boundary.count-1))
        for number in [Double.nan,.infinity,-.infinity] {
            XCTAssertThrowsError(try StrictJSON.serialize(.array([.number(number)])))
        }
    }

    func testLegacyTimestampNormalizationKeepsMicrosecondsExactly() throws {
        XCTAssertEqual(try UtcTimestamp("2026-11-11 12:00:00").rawValue,"2026-11-11T12:00:00.000000Z")
        XCTAssertEqual(try UtcTimestamp("2026-11-11T15:00:00.123456789+03:00").rawValue,"2026-11-11T12:00:00.123456Z")
        XCTAssertEqual(try UtcTimestamp("2026-01-01T00:00:00.000001+01:00").rawValue,"2025-12-31T23:00:00.000001Z")
        XCTAssertEqual(try UtcTimestamp("1969-12-31T23:59:59.999999Z").rawValue,"1969-12-31T23:59:59.999999Z")
        XCTAssertThrowsError(try UtcTimestamp("2026-02-30T12:00:00Z"))
        XCTAssertThrowsError(try UtcTimestamp("2026-11-11T12:00:00Z\n"))
        XCTAssertThrowsError(try UtcTimestamp("٢٠٢٦-11-11T12:00:00Z"))
    }
    func testFullFiniteFloat64RangeAndUnicodeCodePoints() throws {
        let json = try StrictJSON.parse(fixture)
        var sweep = try Sweep(json: try XCTUnwrap(json.object?["sweep"]))
        sweep.levels = [Double.leastNonzeroMagnitude,Double.greatestFiniteMagnitude,-0.0,-1.0000000000000002]
        let round = try StrictJSON.decode(Sweep.self,from:StrictJSON.encode(sweep))
        XCTAssertEqual(round.levels.map(\.bitPattern),[0x0000000000000001,0x7fefffffffffffff,0x8000000000000000,0xbff0000000000001])
        let value = try StrictJSON.parse(Data(#"{"é":1,"e\u0301":2,"id":"é-e\u0301"}"#.utf8))
        XCTAssertEqual(value.object?.keys.count,3)
        XCTAssertEqual(Array(try XCTUnwrap(value.object?["id"]).requireString().utf8),[0xc3,0xa9,0x2d,0x65,0xcc,0x81])
        let objectRound = try StrictJSON.parse(StrictJSON.serialize(value))
        XCTAssertEqual(objectRound.object?.keys.count,3)
        XCTAssertEqual(objectRound.object?["é"],.integer("1"))
        XCTAssertEqual(objectRound.object?["e\u{301}"],.integer("2"))
        XCTAssertThrowsError(try StrictJSON.parse(Data(#""\ud800""#.utf8)))
    }
    func testCommonEnvelopeVersionsAndErrorDetails() throws {
        let error = try StrictJSON.decode(ErrorEnvelope.self,from:Data(#"{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":"busy","message":"Receiver in use","retry_after":2}}"#.utf8))
        XCTAssertEqual(error.error.code,"busy")
        XCTAssertEqual(error.error.message,"Receiver in use")
        XCTAssertEqual(error.error.extra["retry_after"],.integer("2"))
        XCTAssertThrowsError(try StrictJSON.decode(Envelope.self,from:Data(#"{"contract":"wrong","contract_version":1,"data":{}}"#.utf8)))
        XCTAssertThrowsError(try StrictJSON.decode(Envelope.self,from:Data(#"{"contract":"openradioinspector-platform","contract_version":2,"data":{}}"#.utf8)))
    }
    func testSharedMetadataFixtureRetainsPrivateNamesPrefixesAndUnicode() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("tests/metadata-edge-cases.json")
        let value = try StrictJSON.parse(Data(contentsOf:url))
        XCTAssertEqual(value.object?["sole"]?.object?["$serde_json::private::Number"],.string("1e999"))
        XCTAssertEqual(value.object?["multiple"]?.object?["$serde_json::private::Number"],.string("123"))
        XCTAssertEqual(value.object?["multiple"]?.object?["ordinary"],.bool(true))
        XCTAssertEqual(value.object?["prefix_collision"]?.object?["$serde_json::private::Number"],.string("raw-key"))
        XCTAssertEqual(value.object?["prefix_collision"]?.object?["$ori::escaped::$serde_json::private::Number"],.string("literal-prefix"))
        XCTAssertEqual(value.object?["prefix_collision"]?.object?["$ori::escaped::$ori::escaped::$serde_json::private::Number"],.string("double-prefix"))
        XCTAssertEqual(value.object?["huge_integer"],.integer("18446744073709551616"))
        XCTAssertEqual(value.object?["negative_integer"],.integer("-18446744073709551616"))
        XCTAssertEqual(value.object?["unicode"]?.object?["é"]?.object?["e\u{301}"],.string("ID/雪+%?"))
        XCTAssertEqual(value.object?["unicode"]?.object?["e\u{301}"],.string("distinct"))
        XCTAssertEqual(value.object?["negative_zero"]?.double?.bitPattern,0x8000000000000000)
        XCTAssertEqual(try StrictJSON.parse(StrictJSON.serialize(value)),value)
    }
}
