import XCTest
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
}
