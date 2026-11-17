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
    func testPanRemainsVisibleWhenFramesCoalesceAtTwentyThirtyOrSixtyHz() throws {
        let schedule=try NativeAcceptanceSchedule(seconds:60)
        for window in 0..<3 {
            var positions=[0.0]
            for _ in 1...600 {
                positions.append(positions.last!+schedule.panFraction(window:window))
            }
            for strideSize in [1,2,3] {
                // An ideal renderer samples the latest viewport. The input must
                // still move when two or three 60 Hz updates coalesce, at any phase.
                for phase in 0..<strideSize {
                    let sampled=stride(from:phase,through:600,by:strideSize).map{positions[$0]}
                    let changes=zip(sampled,sampled.dropFirst()).filter{$0 != $1}.count
                    XCTAssertEqual(changes,sampled.count-1,"window=\(window), stride=\(strideSize), phase=\(phase)")
                }
            }
        }
    }
    func testEachPanWindowMovesContinuouslyAndReversesBetweenWindows() throws {
        let schedule=try NativeAcceptanceSchedule(seconds:1800)
        var center=0.0
        for window in 0..<3 {
            let direction=window%2==0 ? 1.0:-1.0
            let start=center
            XCTAssertGreaterThan(schedule.panFraction(window:window)*direction,0)
            for _ in 1...600 {
                let delta=schedule.panFraction(window:window)
                center+=delta
                // Half-width zoom leaves 0.5 visible spans on each side. Keep
                // room for the separate +/-0.02 input probes without edge clamp.
                XCTAssertLessThan(abs(center)+0.02,0.5)
            }
            XCTAssertEqual((center-start)*direction,0.45,accuracy:1e-10)
        }
    }
}
