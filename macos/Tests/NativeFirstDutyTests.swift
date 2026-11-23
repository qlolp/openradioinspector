import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeFirstDutyTests: XCTestCase {
    func testIndependentInstrumentsAndOptionalSecondary() throws {
        var draft = NativeRCPreparationDraft()
        draft.tinySAPort = "/dev/ttyACM0"
        guard case let .tinySA(_, _, secondary) = try draft.validated() else { return XCTFail("Expected tinySA") }
        XCTAssertNil(secondary)
        draft.source = .rfExplorer
        draft.explorer.port = "COM7"
        draft.explorer.applySurveyRange(100_000_000...400_000_000)
        guard case let .rfExplorer(plan, receiver) = try draft.validated() else { return XCTFail("Expected RF Explorer") }
        XCTAssertNil(receiver)
        XCTAssertEqual(plan.plannedSurveyTiles, 268)
        draft.receiverPort = "COM7"
        XCTAssertThrowsError(try draft.validated())
        draft.receiverPort = "COM9"
        guard case let .rfExplorer(_, attached) = try draft.validated() else { return XCTFail("Expected pair") }
        XCTAssertEqual(attached, "COM9")
        draft.source = .ardv1
        draft.startHz = "100000"; draft.stopHz = "1300000000"; draft.stepHz = "10000"
        guard case let .ardv1(port, grid) = try draft.validated() else { return XCTFail("Expected single primary receiver") }
        XCTAssertEqual(port, "COM9")
        XCTAssertEqual(grid.pointCount, 129_991)
        XCTAssertTrue(grid.cyclic)
    }

    func testBaselineRequiresFullPerPointOrWindowEvidence() {
        func check(countKey: String, readyKey: String, ready: Int64) -> NativeBaselineProgress {
            NativeBaselineProgress(preflight: .object(["baseline_collection": .object([
                "ready":.boolean(true),"passes":.integer(20),"minimum_passes":.integer(20),"elapsed_seconds":.integer(90),
                countKey:.integer(3),readyKey:.integer(ready)])]))
        }
        XCTAssertFalse(check(countKey: "windows", readyKey: "ready_windows", ready: 2).ready)
        XCTAssertFalse(check(countKey: "points", readyKey: "ready_points", ready: 2).ready)
        XCTAssertTrue(check(countKey: "points", readyKey: "ready_points", ready: 3).ready)
    }

    func testWeeklySchedulePreservesLocalOvernightWindowAndSortedISODays() throws {
        var schedule = NativeDutyScheduleDraft()
        XCTAssertFalse(schedule.resume)
        schedule.mode = "weekly"; schedule.weekdays = [7, 1]; schedule.timezone = "Europe/Moscow"
        schedule.start = "22:30"; schedule.end = "06:00"
        let saved = try schedule.value()
        XCTAssertEqual(saved["weekdays"].items.map(\.integer), [1,7])
        XCTAssertEqual(saved["timezone"].text, "Europe/Moscow")
        XCTAssertEqual(saved["end_local"].text, "06:00")
        schedule.start = "25:01"
        XCTAssertThrowsError(try schedule.value())
        schedule.mode = "until"; schedule.until = Date(timeIntervalSince1970: 0)
        XCTAssertThrowsError(try schedule.value())
    }

    func testReceiverEventUsesCountsAndImmutableObservationReference() {
        let source = MonitoringValue.object(["session_id":.string("s"),"stream_id":.string("r"),
            "observation_id":.string("o"),"scan_pass":.integer(2),"frequency_hz":.integer(145_000_000)])
        let event = NativeGuardSignal(value: .object(["level_unit":.string("s_meter_count"),"peak_level":.integer(11),
            "excess_counts":.integer(7),"run_session_id":.string("s"),"peak_source":source]))
        XCTAssertTrue(event.peakReading.hasSuffix("s_meter_count"))
        XCTAssertEqual(event.observationSource?.observation, "o")
        XCTAssertEqual(event.observationSource?.archivedEvent, true)
        XCTAssertNil(event.source)
        let foreign = NativeGuardSignal(value: .object(["level_unit":.string("s_meter_count"),"run_session_id":.string("other"),"peak_source":source]))
        XCTAssertNil(foreign.observationSource)
    }
}
