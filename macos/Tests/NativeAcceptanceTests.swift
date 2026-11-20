import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class NativeAcceptanceTests: XCTestCase {
    func testDelayedRefreshAndOldViewCancellationCannotLoseScenarioOwnership() {
        var lifecycle=NativeAcceptanceScenarioLifecycle()
        XCTAssertNil(lifecycle.attach(session:"work",stream:"",sourceSession:nil,sourceStream:nil,expected:"work"))
        let first=lifecycle.attach(session:"work",stream:"analyzer",sourceSession:"work",sourceStream:"analyzer",expected:"work")
        XCTAssertNotNil(first)
        let replacement=lifecycle.attach(session:"work",stream:"analyzer",sourceSession:"work",sourceStream:"analyzer",expected:"work")
        XCTAssertNotEqual(first,replacement)
        XCTAssertFalse(lifecycle.detach(first!))
        XCTAssertEqual(lifecycle.owner,replacement)
        XCTAssertTrue(lifecycle.detach(replacement!))
        XCTAssertNotNil(lifecycle.attach(session:"work",stream:"analyzer",sourceSession:"work",sourceStream:"analyzer",expected:"work"))
    }
    func testMarkerKeepsAllSixtyFourBitsAndRejectsCorruptPixels() throws {
        let bits = NativeAcceptanceMarker.bits(id: 0xfedcba9876543210, tag: 0x12345678)
        XCTAssertEqual(bits.count, 128)
        XCTAssertEqual(Array(bits.prefix(8)), [false,false,false,true,false,false,true,false])
        XCTAssertEqual(NativeAcceptanceMarker.decode(bits, tag: 0x12345678), 0xfedcba9876543210)
        XCTAssertNil(NativeAcceptanceMarker.decode(bits, tag: 0x12345679))
        var damaged=bits;damaged[77].toggle()
        XCTAssertNil(NativeAcceptanceMarker.decode(damaged, tag: 0x12345678))
    }
    func testOldDrawableKeepsItsOwnSourceAndDoesNotBorrowLaterInteraction() throws {
        var registry=NativeAcceptanceFrames(capacity: 2)
        let old=try registry.issue(sourceID: "old", sequence: 9007199254740993, requests: ["first"])
        let new=try registry.issue(sourceID: "new", sequence: 9007199254740994, requests: ["second"])
        XCTAssertEqual(registry.frame(old.id)?.sourceID,"old")
        XCTAssertEqual(registry.frame(old.id)?.requests,["first"])
        XCTAssertEqual(registry.frame(new.id)?.sequence,9007199254740994)
        _=try registry.issue(sourceID:"last",sequence:9007199254740995,requests:[])
        XCTAssertNil(registry.frame(old.id))
    }
}
