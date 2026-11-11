import Foundation
import XCTest
import ORIContracts
import ORIMeasurementStream
@testable import SpektrKomandy

final class BinaryMeasurementTests: XCTestCase {
    private func capability() -> JSONObject {
        ["version":.integer("1"),"transport":.string("websocket"),"encoding":.string("protobuf"),
         "path":.string("/api/v1/events/ws"),"subprotocol":.string("ori.measurements.v1.protobuf"),
         "max_frame_bytes":.integer("16842752")]
    }
    private func golden() throws -> [JSONObject] {
        let root=URL(fileURLWithPath:#filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let value=try StrictJSON.parse(Data(contentsOf:root.appendingPathComponent("contracts/fixtures/stream-v1.json"))).requireObject()
        return try XCTUnwrap(value["valid"]).requireArray().map {try $0.requireObject()}
    }
    private func item(_ name:String) throws -> JSONObject {
        try XCTUnwrap(golden().first {try $0["name"]?.requireString()==name})
    }
    private func wire(_ item:JSONObject) throws -> Data {
        let text=Array(try XCTUnwrap(item["wire_hex"]).requireString().utf8)
        return Data(stride(from:0,to:text.count,by:2).map {UInt8(String(decoding:text[$0..<$0+2],as:UTF8.self),radix:16)!})
    }

    func testOnlySupportedExactCapabilityNegotiatesBinaryAndMalformedIsVisible() throws {
        XCTAssertEqual(try PlatformMeasurementTransport.negotiate(features:[:]),.serverSentEvents)
        XCTAssertEqual(try PlatformMeasurementTransport.negotiate(features:["measurement_binary_stream":.object(capability())]),.binaryV1)
        for (key,value) in [("version",JSONValue.integer("2")),("encoding",.string("other")),("path",.string("https://other.invalid/api/v1/events/ws")),("max_frame_bytes",.integer("16842753"))] {
            var descriptor=capability();descriptor[key]=value
            XCTAssertEqual(try PlatformMeasurementTransport.negotiate(features:["measurement_binary_stream":.object(descriptor)]),.serverSentEvents,key)
        }
        XCTAssertThrowsError(try PlatformMeasurementTransport.negotiate(features:["measurement_binary_stream":.bool(true)]))
        var missing=capability();missing.removeValue(forKey:"path")
        XCTAssertThrowsError(try PlatformMeasurementTransport.negotiate(features:["measurement_binary_stream":.object(missing)]))
    }

    func testTypedArraysAndOpaqueMetadataStayExactWithoutFoundationDictionaryLoss() throws {
        let value=try item("large-sequence-and-opaque-metadata"), bytes=try wire(value)
        let event=try PlatformMeasurementEvent.decodeBinary(bytes)
        XCTAssertEqual(event.sequence,9_007_199_254_740_993)
        XCTAssertNil(event.displayIssue)
        let sweep=try XCTUnwrap(event.committedEvent?.payload.sweep)
        XCTAssertEqual(sweep.frequenciesHz,[100000001,100000002,100000003,100000004])
        XCTAssertEqual(sweep.levels.map(\.bitPattern),[0xc059080000000000,0x8000000000000000,0xbff0000000000001,0xc040600000000000])
        XCTAssertEqual(sweep.settings?["generator_output"],.boolean(false))
        XCTAssertEqual(try PlatformTimestamp(sweep.interval.startedAt).utcMicroseconds,1_794_398_400_123_456)
        XCTAssertEqual(event.source.originalData,bytes)
        XCTAssertEqual(String(data:event.source.metadataJSON,encoding:.utf8),try value["metadata_json"]?.requireString())
        let extensions=try XCTUnwrap(event.source.metadata["unknown-envelope"]).requireObject()
        XCTAssertEqual(extensions.count,2)
        XCTAssertEqual(try extensions["é"]?.requireString(),"NFC")
        XCTAssertEqual(try extensions["e\u{301}"]?.requireString(),"NFD")
        let payload=try XCTUnwrap(event.source.metadata["data"]).requireObject()["payload"]?.requireObject()
        guard case .integer("1267650600228229401496703205376")?=try payload?["unknown"]?.requireObject()["huge"] else {return XCTFail("opaque integer was rounded")}
    }

    func testValidFutureRangesRemainSourceOnlyRatherThanThrowingOrPlottingRoundedData() throws {
        for (name,issue) in [("uint64-limits-and-f64-bits",PlatformMeasurementDisplayIssue.unsupportedCursor),
                             ("larger-than-uint64-hz-generic",.unsupportedFrequency)] {
            let bytes=try wire(item(name)),event=try PlatformMeasurementEvent.decodeBinary(bytes)
            XCTAssertEqual(event.displayIssue,issue,name)
            XCTAssertNil(event.committedEvent?.payload.sweep,name)
            XCTAssertEqual(event.source.originalData,bytes,name)
        }
        let generic=try PlatformMeasurementEvent.decodeBinary(wire(item("integer-level-kind-generic")))
        XCTAssertEqual(generic.committedEvent?.payload.sweep?.frequenciesHz,[1,2,3])
        XCTAssertEqual(generic.committedEvent?.payload.sweep?.levels,[-90,0,1])
        let unknown=try PlatformMeasurementEvent.decodeBinary(wire(item("unknown-event-generic")))
        XCTAssertEqual(unknown.committedEvent?.eventType,"future.extension")
        XCTAssertNil(unknown.displayIssue)
        XCTAssertEqual(unknown.source.originalData,try wire(item("unknown-event-generic")))
        let receiver=try PlatformMeasurementEvent.decodeBinary(wire(item("receiver-original-unit-generic")))
        XCTAssertEqual(receiver.committedEvent?.payload.observation?.level,137)
        XCTAssertEqual(receiver.committedEvent?.payload.observation?.levelUnit,"s_meter_count")
    }

    func testMalformedFrameNeverBecomesAnAcknowledgableSourceEvent() throws {
        let bytes=try wire(item("measurement-v1"))
        for end in [0,1,2,bytes.count-1] {XCTAssertThrowsError(try PlatformMeasurementEvent.decodeBinary(bytes.prefix(end)))}
    }

    func testWebSocketRequestRetainsOriginCredentialAndCookieFreePolicy() throws {
        let credential=try PlatformBearerCredential(token:"owned-binary-test-credential")
        let client=try PlatformClient(baseURL:"https://example.test:9443/root/api/v1",credential:credential)
        let request=try client.binaryMeasurementRequest(after:UInt64.max)
        XCTAssertEqual(request.url?.absoluteString,"wss://example.test:9443/root/api/v1/events/ws?after=18446744073709551615")
        XCTAssertEqual(request.value(forHTTPHeaderField:"Authorization"),"Bearer owned-binary-test-credential")
        XCTAssertEqual(request.value(forHTTPHeaderField:"Sec-WebSocket-Protocol"),"ori.measurements.v1.protobuf")
        XCTAssertNil(request.value(forHTTPHeaderField:"Cookie"))
        XCTAssertFalse(request.httpShouldHandleCookies)
        let loopback=try PlatformClient(baseURL:"http://127.0.0.1:9876")
        XCTAssertEqual(try loopback.binaryMeasurementRequest(after:0).url?.scheme,"ws")
        XCTAssertThrowsError(try PlatformClient(baseURL:"http://192.0.2.1"))
        XCTAssertThrowsError(try PlatformClient(baseURL:"https://user:password@example.test"))
        XCTAssertThrowsError(try PlatformClient(baseURL:"http://127.0.0.1",credential:credential))
    }
}
