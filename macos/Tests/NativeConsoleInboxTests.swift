import Foundation
import XCTest
import SpektrKomandy
#if canImport(DvoynoyObzorSession)
@testable import DvoynoyObzorSession
#endif

final class NativeConsoleInboxTests: XCTestCase {
    func testSelectionAloneDoesNotMarkAnUnloadedEventRead() {
        var queue = NativeGuardSignalQueue()
        let first: MonitoringValue = .object(["id": .string("first")])
        let arriving: MonitoringValue = .object(["id": .string("arriving")])
        queue.update(.object(["items": .array([first])]), at: Date())
        queue.update(.object(["items": .array([arriving, first])]), at: Date())
        queue.select(PlatformExactID("arriving"))
        XCTAssertEqual(queue.unread, [PlatformExactID("arriving")],
            "A failed or cancelled detail load must not silently acknowledge a new event")
    }
}
