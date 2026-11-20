import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class NativeAcceptanceDisplayReadinessTests:XCTestCase {
    @MainActor func testCaptureAdmissionWaitsForDelayedVisibleWindow() async throws {
        var visible=false
        let showing=Task { @MainActor in await Task.yield();visible=true }
        try await NativeAcceptanceDisplayReadiness.wait(timeout:1){visible}
        await showing.value
        XCTAssertTrue(visible)
    }
    @MainActor func testUnavailableWindowReportsRecoveryInsteadOfInvalidDataFormat() async {
        do {
            try await NativeAcceptanceDisplayReadiness.wait(timeout:0){false}
            XCTFail("Invisible window admitted for presentation measurement")
        } catch {
            let value=error as NSError
            XCTAssertEqual(value.domain,"ORI.Acceptance")
            XCTAssertTrue(value.localizedDescription.contains("Разблокируйте"))
            XCTAssertTrue(value.localizedDescription.contains("окно"))
        }
    }
}
