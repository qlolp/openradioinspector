import XCTest
import Foundation
import ORIContracts

final class RuntimeContractTests: XCTestCase {
    private func fixture() throws -> JSONValue {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures/runtime-v1.json")
        return try StrictJSON.parse(Data(contentsOf: url))
    }

    func testRuntimeDTOsReuseStrictCodecsAndSummaryType() throws {
        let value = try fixture()
        let status = try SecurityStatus(json: XCTUnwrap(value.object?["security_status"]))
        XCTAssertEqual(status.mode.rawValue, "legacy_loopback")
        XCTAssertFalse(status.pairing_required)
        let round = try StrictJSON.parse(StrictJSON.encode(status))
        let extra = round.object?["future_metadata"]?.object
        XCTAssertEqual(extra?["big"], .integer("18446744073709551615"))
        XCTAssertEqual(extra?["negative_zero"]?.double?.bitPattern, 0x8000000000000000)
        XCTAssertEqual(extra?["adjacent"]?.double?.bitPattern, 0xbff0000000000001)
        XCTAssertEqual(extra?["é"], .integer("1"))
        XCTAssertEqual(extra?["e\u{301}"], .integer("2"))
        XCTAssertEqual(extra?["$serde_json::private::Number"], .string("literal"))
        let browser = try BrowserPairing(json: XCTUnwrap(value.object?["browser_pairing"]))
        XCTAssertEqual(browser.credential.rawValue, "browser_cookie")
        let native = try NativePairing(json: XCTUnwrap(value.object?["native_pairing"]))
        XCTAssertEqual(native.token, "synthetic-fixture-never-a-valid-credential")
        var socket = try MeasurementWebSocket(json: XCTUnwrap(value.object?["websocket_descriptor"]))
        XCTAssertEqual(socket.path, "/api/v1/events/ws")
        socket.max_frame_bytes = UInt64.max
        XCTAssertEqual(try StrictJSON.decode(MeasurementWebSocket.self, from: StrictJSON.encode(socket)).max_frame_bytes, UInt64.max)
        let page = try SweepSummaryPage(json: XCTUnwrap(value.object?["summary_page"]))
        XCTAssertNil(page.next_cursor)
        XCTAssertEqual(page.items[0].peak_level.bitPattern, 0x8000000000000000)
    }

    func testRuntimeBackendAndRequestSelectionFailsClosed() throws {
        XCTAssertEqual(runtimeOperations.first { $0.operationId == "post_api_v1_security_pair" }?.maxRequestBytes, 4096)
        XCTAssertEqual(runtimeOperations.first { $0.operationId == "get_api_v1_sessions_id_sweep_summaries" }?.pathParameters, ["id"])
        XCTAssertThrowsError(try selectRuntimeOperations(backend: "unknown"))
        XCTAssertEqual(try selectRuntimeOperations(backend: "python-legacy").count, 136)
        let bare = try selectRuntimeOperations(backend: "rust-candidate").map(\.operationId)
        XCTAssertTrue(bare.contains("get_api_v1_security_status"))
        XCTAssertFalse(bare.contains("get_api_v1_events_ws"))
        XCTAssertTrue(try selectRuntimeOperations(backend: "rust-candidate", features: ["sweep_summaries"]).map(\.operationId).contains("get_api_v1_sessions_id_sweep_summaries"))
        XCTAssertTrue(try selectRuntimeOperations(backend: "rust-candidate", capabilities: ["ori.measurements.v1.protobuf"]).map(\.operationId).contains("get_api_v1_events_ws"))
        XCTAssertFalse(try selectRuntimeOperations(backend: "rust-candidate", features: ["control_task_planning"], request: ["enabled": .bool(true)]).map(\.operationId).contains("post_api_v1_task_scheduler"))
    }

    func testUnknownWebSocketProtocolVersionIsRejected() throws {
        let value = try fixture()
        var object = try XCTUnwrap(value.object?["websocket_descriptor"]?.object)
        object["version"] = .integer("2")
        XCTAssertThrowsError(try MeasurementWebSocket(json: .object(object)))
    }
}
