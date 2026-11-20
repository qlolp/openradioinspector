import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeContinuityTests: XCTestCase {
    func testRetainedWideCoverageIsUnconfirmedAfterCoreOrProjectStatusFailure() {
        XCTAssertEqual(NativeContinuityLabels.coverage("wide", coreReachable: false, statusVerified: true), "Покрытие не подтверждено")
        XCTAssertEqual(NativeContinuityLabels.coverage("wide", coreReachable: true, statusVerified: false), "Покрытие не подтверждено")
        XCTAssertEqual(NativeContinuityLabels.coverage("wide", coreReachable: true, statusVerified: true), "Широкий обзор")
        XCTAssertEqual(NativeContinuityLabels.guardState("armed", coreReachable: true, selected: true,
            statusVerified: false, armed: true), "Статус охраны не подтверждён")
        XCTAssertEqual(NativeContinuityLabels.guardState("armed", coreReachable: true, selected: true,
            statusVerified: true, armed: true), "Охрана включена")
    }
    func testCoverageRequiresMatchingSessionStreamAndPlanRevision() throws {
        let status = try JSONDecoder().decode(MonitoringValue.self, from: Data("""
            {"project":{"active_session_id":"a","analyzer_stream_id":"tiny","scan_plan":{"id":"p","revision":3,
             "config":{"bands":[{"id":"VHF"},{"id":"UHF"}]}}},
             "workers":[{"session_id":"b","stream_id":"tiny","current":{"scan_coverage":{"plan_id":"p","revision":3,
                         "bands":[{"id":"UHF","state":"fresh"}]}}},
                        {"session_id":"a","stream_id":"tiny","current":{"scan_coverage":{"plan_id":"p","revision":3,
                         "bands":[{"id":"VHF","state":"partial","age_seconds":4,"measurement_qualification":{
                             "status":"partial","reason":"reference_required_above_800_mhz","verified_through_hz":800000000}}]}}}]}
            """.utf8))
        let rows = NativeBandCoverage.rows(status: status, sessionID: "a")
        XCTAssertEqual(rows.map(\.state), ["Частичное покрытие", "Покрытие неизвестно"])
        XCTAssertEqual(NativeBandCoverage.duration(rows[1].report["age_seconds"]), "нет данных")
        XCTAssertTrue(NativeBandCoverage.quality(rows[0].report["measurement_qualification"]).contains("выше 800 МГц"))
        XCTAssertEqual(NativeBandCoverage.quality(rows[1].report["measurement_qualification"]), "Нет сведений о качестве")
        XCTAssertTrue(NativeBandCoverage.rows(status: status, sessionID: "b").isEmpty)
        guard case .object(var root) = status, case .object(var project) = status["project"],
              case .object(var plan) = project["scan_plan"] else { return XCTFail("fixture") }
        plan["revision"] = .integer(4)
        project["scan_plan"] = .object(plan)
        root["project"] = .object(project)
        let newRevision = MonitoringValue.object(root)
        XCTAssertTrue(NativeBandCoverage.rows(status: newRevision, sessionID: "a").allSatisfy { $0.report == .null })
    }

    func testEditingAnIntegerGridKeepsItsStepAndRecalculatesPointCount() throws {
        var draft = ScanBandDraft(.object(["id": .string("grid"), "name": .string("Grid"),
            "start_hz": .integer(799_000_000), "stop_hz": .integer(803_000_000), "points": .integer(401),
            "rbw_hz": .integer(10_000), "step_hz": .integer(10_000), "max_revisit_seconds": .integer(120)]))
        XCTAssertEqual(draft.payload?["step_hz"], .integer(10_000))
        draft.stop = "803010000"
        XCTAssertEqual(draft.payload?["points"], .integer(402))
        draft.stop = "803010001"
        XCTAssertNil(draft.payload)
    }
}
