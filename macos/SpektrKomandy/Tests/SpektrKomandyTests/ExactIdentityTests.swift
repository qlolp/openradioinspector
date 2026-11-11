import Foundation
import XCTest
@testable import SpektrKomandy

final class ExactIdentityTests: XCTestCase {
    func testExactIdentityKeysRetainBothUTF8SpellingsAndWireValues() {
        let composed = PlatformExactID("é"), decomposed = PlatformExactID("e\u{301}")
        let values = [composed: 1, decomposed: 2]
        XCTAssertEqual(values.count, 2)
        XCTAssertEqual(values[composed], 1); XCTAssertEqual(values[decomposed], 2)
        XCTAssertEqual([composed, decomposed].sorted().map { Array($0.rawValue.utf8) }, [[101, 204, 129], [195, 169]])
    }

    func testSessionAndEventListKeyPathsDoNotCollapseCanonicalAliases() throws {
        let bytes = Data("[{\"id\":\"é\",\"name\":\"one\",\"state\":\"running\",\"streams\":[]},{\"id\":\"e\u{301}\",\"name\":\"two\",\"state\":\"running\",\"streams\":[]}]".utf8)
        let sessions = try JSONDecoder().decode([PlatformSession].self, from: bytes)
        XCTAssertEqual(Set(sessions.map(\.exactID)).count, 2)
        let events: [MonitoringValue] = [.object(["id": .string("é")]), .object(["id": .string("e\u{301}")])]
        XCTAssertEqual(Set(events.map(\.exactID)).count, 2)
        XCTAssertEqual(sessions.map { Array($0.id.utf8) }, [[195, 169], [101, 204, 129]])
    }

    func testProjectPagesRetainExactSelectionRatherThanCanonicalAlias() {
        func page(_ id: String, next: String? = nil) -> MonitoringValue {
            .object(["items": .array([.object(["id": .string(id)])]), "next_cursor": next.map(MonitoringValue.string) ?? .null])
        }
        var pages = MonitoringProjectPageState()
        pages.apply(page("é", next: "next"), page: 0)
        pages.apply(page("e\u{301}"), page: 1)
        XCTAssertEqual(pages.items.map { Array($0["id"].text.utf8) }, [[195, 169], [101, 204, 129]])
        pages.select("e\u{301}")
        XCTAssertEqual(Array(pages.selectedID.utf8), [101, 204, 129])
        pages.created(.object(["id": .string("é")]))
        XCTAssertEqual(pages.items.count, 2)
    }

    func testDistinctUnicodeAlertIsNotMistakenForPreviouslySeenEvent() {
        func alert(_ id: String) -> MonitoringValue { .object(["id": .string(id), "acknowledged_at": .null]) }
        var sound = MonitoringAlertSoundState()
        XCTAssertFalse(sound.observe([alert("é")], enabled: true))
        XCTAssertTrue(sound.observe([alert("e\u{301}")], enabled: true))
        XCTAssertFalse(sound.observe([alert("e\u{301}")], enabled: true))
    }

    func testProjectSessionFollowKeysAndSessionValuesAreByteExact() {
        var follow = MonitoringProjectSessionFollowState()
        XCTAssertNotNil(follow.sessionToFollow(endpoint: "server", projectID: "é", activeSessionID: "é"))
        XCTAssertNotNil(follow.sessionToFollow(endpoint: "server", projectID: "e\u{301}", activeSessionID: "é"))
        XCTAssertNotNil(follow.sessionToFollow(endpoint: "server", projectID: "é", activeSessionID: "e\u{301}"))
        XCTAssertNil(follow.sessionToFollow(endpoint: "server", projectID: "é", activeSessionID: "e\u{301}"))
    }
}
