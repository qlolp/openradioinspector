import XCTest
@testable import DvoynoyObzorSession

final class NativeAcceptanceScheduleTests:XCTestCase {
    func testSmokeContainsThreeCompleteTenSecondPanWindowsAndOneHundredInputs() throws {
        let value=try NativeAcceptanceSchedule(seconds:60)
        XCTAssertEqual(value.panStarts,[6_000_000_000,24_000_000_000,42_000_000_000])
        XCTAssertEqual(value.interactionDue(0),0);XCTAssertEqual(value.interactionDue(99),59_400_000_000)
        XCTAssertEqual(value.panDuration,10_000_000_000)
        XCTAssertLessThan(value.panStarts.last!+value.panDuration,value.duration)
    }
    func testFullRunKeepsOriginalTimesAndRejectsNonfiniteDuration() throws {
        let value=try NativeAcceptanceSchedule(seconds:1800)
        XCTAssertEqual(value.panStarts,[180_000_000_000,720_000_000_000,1_260_000_000_000])
        XCTAssertEqual(value.interactionDue(10),180_000_000_000)
        for duration in [Double.nan,.infinity,0,1801] {
            XCTAssertThrowsError(try NativeAcceptanceSchedule(seconds:duration))
        }
    }
}
