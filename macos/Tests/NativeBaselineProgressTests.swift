import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeBaselineProgressTests: XCTestCase {
    private func progress(_ count: Int64, _ elapsed: Double, cycle: Double? = nil,
                          ready: Bool = false) -> NativeBaselineProgress {
        NativeBaselineProgress(preflight: .object([
            "baseline_collection": .object(["sweeps": .integer(count),
                "elapsed_seconds": .number(elapsed), "ready": .boolean(ready)]),
            "performance": .object(["mean_observed_cycle_seconds": cycle.map(MonitoringValue.number) ?? .null])]))
    }

    func testTimeProgressDoesNotHideTheRemainingNineteenSlowSweeps() {
        let value = progress(1, 77, cycle: 77)
        XCTAssertEqual(value.timeFraction, 1)
        XCTAssertEqual(value.sweepFraction, 0.05)
        XCTAssertEqual(value.estimatedRemainingSeconds, 1463)
        XCTAssertFalse(value.ready)
    }

    func testTwentyFastSweepsStillRequireTheFullMinuteAndCoreConfirmation() {
        let early = progress(20, 59.5, cycle: 0.5, ready: true)
        XCTAssertEqual(early.estimatedRemainingSeconds, 0.5)
        XCTAssertFalse(early.ready)
        XCTAssertTrue(progress(20, 60, ready: true).ready)
        XCTAssertFalse(progress(20, 60, ready: false).ready)
        XCTAssertNil(progress(20, 60, ready: false).estimatedRemainingSeconds)
    }

    func testUnknownOrInvalidPeriodDoesNotInventAnEstimate() {
        for period in [nil, 0, -1, Double.infinity, Double.nan] {
            XCTAssertNil(progress(10, 60, cycle: period).estimatedRemainingSeconds)
        }
        XCTAssertFalse(progress(20, .infinity, ready: true).ready)
        XCTAssertFalse(progress(-1, 60, cycle: 1, ready: true).ready)
    }
}
