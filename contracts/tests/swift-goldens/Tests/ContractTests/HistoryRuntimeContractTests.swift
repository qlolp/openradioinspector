import XCTest
import Foundation
import ORIContracts

final class HistoryRuntimeContractTests: XCTestCase {
    private func fixture() throws -> JSONValue {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("fixtures/history-runtime-v1.json")
        return try StrictJSON.parse(Data(contentsOf: url))
    }

    func testHistorySignedTimeNullableUInt64AndNestedUnknownJSON() throws {
        let f = try fixture()
        let request = try HistoryRequest(json: XCTUnwrap(f.object?["request"]))
        XCTAssertEqual(request.filters?.started_at_us, Int64.min)
        XCTAssertEqual(request.filters?.ended_at_us, Int64.max)
        XCTAssertEqual(request.filters?.max_frequency_hz, Int64.max)
        XCTAssertEqual(request.threshold?.value.bitPattern, 0x8000000000000000)
        let requestRound = try StrictJSON.decode(HistoryRequest.self, from: StrictJSON.encode(request))
        XCTAssertEqual(requestRound.filters?.session_id, "history-é-e\u{301}/雪")
        let job = try HistoryJob(json: XCTUnwrap(f.object?["job"]))
        XCTAssertEqual(job.source_boundary, UInt64.max)
        let round = try StrictJSON.decode(HistoryJob.self, from: StrictJSON.encode(job))
        XCTAssertEqual(round.source_boundary, UInt64.max)
        XCTAssertNil(round.failure)
        let future = try XCTUnwrap(round.enforcement?.extra["future"]?.object)
        XCTAssertEqual(future["é"], .integer("1"))
        XCTAssertEqual(future["e\u{301}"], .integer("2"))
        XCTAssertEqual(future["$serde_json::private::Number"], .string("literal"))
        XCTAssertEqual(future["$ori::escaped::$serde_json::private::Number"], .string("literal-prefix"))
        XCTAssertEqual(future["big"], .integer("18446744073709551616"))
        XCTAssertEqual(future["negative_zero"]?.double?.bitPattern, 0x8000000000000000)
        XCTAssertEqual(future["adjacent"]?.double?.bitPattern, 0xbff0000000000001)
        let failed = try HistoryJob(json: XCTUnwrap(f.object?["failed_job"]))
        XCTAssertNil(failed.source_boundary)
        XCTAssertNil(failed.enforcement)
        XCTAssertEqual(failed.failure?.code, "source_record_invalid")
        let output = try HistoryJobResult(json: XCTUnwrap(f.object?["result"]))
        let outputRound = try StrictJSON.decode(HistoryJobResult.self, from: StrictJSON.encode(output))
        XCTAssertEqual(outputRound.metrics["io_bytes"], .integer("18446744073709551615"))
        XCTAssertEqual(outputRound.metrics["negative_zero"]?.double?.bitPattern, 0x8000000000000000)
        XCTAssertEqual(outputRound.metrics["$serde_json::private::Number"], .string("metrics-key"))
        XCTAssertEqual(outputRound.enforcement.extra["future"]?.object?["$serde_json::private::Number"], .string("result-enforcement-key"))
        XCTAssertEqual(outputRound.data["é"], .integer("1"))
        XCTAssertEqual(outputRound.data["e\u{301}"], .integer("2"))
    }

    func testHistoryViewportRetainsOriginalPointIdentityUnitsAndFloatBits() throws {
        let f = try fixture()
        let query = try HistoryViewportQuery(json: XCTUnwrap(f.object?["viewport_query"]))
        XCTAssertEqual(query.start_us, Int64.min)
        XCTAssertEqual(query.end_us, Int64.max)
        XCTAssertNil(query.cursor)
        let page = try HistoryViewportPage(json: XCTUnwrap(f.object?["viewport_page"]))
        let round = try StrictJSON.decode(HistoryViewportPage.self, from: StrictJSON.encode(page))
        XCTAssertTrue(round.derived)
        XCTAssertEqual(round.boundary, UInt64.max)
        let row = try XCTUnwrap(round.rows.first)
        XCTAssertEqual(row.started_at_us, Int64.min)
        XCTAssertEqual(row.ended_at_us, Int64.max)
        XCTAssertEqual(row.measurement_id, "sweep-e\u{301}/雪")
        XCTAssertEqual(row.level_unit, "s_meter_count")
        XCTAssertNil(row.settings_sha256)
        XCTAssertEqual(row.cells[0].level.bitPattern, 0x8000000000000000)
        XCTAssertEqual(row.cells[1].level.bitPattern, 0xbff0000000000001)
        XCTAssertEqual(row.cells[1].frequency_hz, Int64.max)
        XCTAssertEqual(row.cells[1].point_index, 65535)
        XCTAssertEqual(round.next_cursor?.boundary, UInt64.max)
        XCTAssertEqual(round.next_cursor?.after.started_at.rawValue, "1969-12-31T23:59:59.999999Z")
    }

    func testHistoryRejectsIntegerOverflowMissingRequiredNullAndNegativeFrequency() throws {
        let f = try fixture()
        var job = try XCTUnwrap(f.object?["job"]?.object)
        for value in ["-1", "18446744073709551616"] {
            job["source_boundary"] = .integer(value)
            XCTAssertThrowsError(try HistoryJob(json: .object(job)))
        }
        job.removeValue(forKey: "source_boundary")
        XCTAssertThrowsError(try HistoryJob(json: .object(job)))
        var request = try XCTUnwrap(f.object?["request"]?.object)
        var filters = try XCTUnwrap(request["filters"]?.object)
        for value in ["-9223372036854775809", "9223372036854775808"] {
            filters["started_at_us"] = .integer(value)
            request["filters"] = .object(filters)
            XCTAssertThrowsError(try HistoryRequest(json: .object(request)))
        }
        filters["started_at_us"] = .integer("0")
        filters["min_frequency_hz"] = .integer("-1")
        request["filters"] = .object(filters)
        XCTAssertThrowsError(try HistoryRequest(json: .object(request)))
        request = try XCTUnwrap(f.object?["request"]?.object)
        for pageSize in ["0", "501"] {
            request["page_size"] = .integer(pageSize)
            XCTAssertThrowsError(try HistoryRequest(json: .object(request)))
        }
        var query = try XCTUnwrap(f.object?["viewport_query"]?.object)
        for columns in ["0", "1025"] {
            query["columns"] = .integer(columns)
            XCTAssertThrowsError(try HistoryViewportQuery(json: .object(query)))
        }
    }

    func testHistoryOperationsRequireIndependentRouteFeatures() throws {
        let ids = { (features: Set<String>) throws in
            try selectRuntimeOperations(backend: "rust-candidate", features: features).map(\.operationId)
        }
        XCTAssertFalse(try ids([]).contains("post_api_v1_history_jobs"))
        XCTAssertTrue(try ids(["derived_history_jobs"]).contains("post_api_v1_history_jobs"))
        XCTAssertFalse(try ids(["derived_history_jobs"]).contains("get_api_v1_history_waterfall"))
        XCTAssertTrue(try ids(["derived_history_viewport"]).contains("get_api_v1_history_waterfall"))
        XCTAssertFalse(try selectRuntimeOperations(backend: "python-legacy").map(\.operationId).contains("post_api_v1_history_jobs"))
    }
}
