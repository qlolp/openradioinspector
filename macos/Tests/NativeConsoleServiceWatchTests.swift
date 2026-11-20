import XCTest
@testable import DvoynoyObzorSession

final class NativeConsoleServiceWatchTests: XCTestCase {
    @MainActor func testDisabledProfilesNeverInspectOrWait() async {
        await NativeConsoleServiceWatch.run(enabled: false, inspect: { XCTFail("No service access") },
                                           wait: { XCTFail("No poll task") })
    }

    @MainActor func testWatcherKeepsInspectingWithoutASettingsPane() async {
        var inspections = 0
        await NativeConsoleServiceWatch.run(enabled: true, inspect: { inspections += 1 }, wait: {
            if inspections == 2 { throw CancellationError() }
        })
        XCTAssertEqual(inspections, 2)
    }
}
