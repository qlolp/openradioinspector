import Foundation
import XCTest
#if canImport(DvoynoyObzorSession)
@testable import DvoynoyObzorSession
#endif

final class NativeConsoleNavigationTests: XCTestCase {
    func testChangingContextKeepsThePresentedSettingsSection() {
        var navigation = NativeConsoleNavigation()
        navigation.activate(.init(server: "core", project: "a", session: "one", stream: "tiny"))
        navigation.destination.settings = .guardSettings
        navigation.activate(.init(server: "core", project: "b", session: "two", stream: "tiny"))
        XCTAssertEqual(navigation.destination.settings, .guardSettings)
    }

    func testPristineVerdictTracksServerAndDirtyDraftWaitsForExplicitSave() {
        var draft = NativeConsoleVerdictDraft()
        draft.receive(context: 1, event: "e", disposition: "unreviewed", comment: "")
        draft.comment = "local edit"
        draft.receive(context: 1, event: "e", disposition: "authorized", comment: "remote edit")
        XCTAssertEqual(draft.comment, "local edit")
        XCTAssertTrue(draft.dirty)
        draft.receive(context: 1, event: "e", disposition: "investigate", comment: "accepted", force: true)
        XCTAssertEqual(draft.comment, "accepted")
        XCTAssertEqual(draft.disposition, "investigate")
        XCTAssertFalse(draft.dirty)
        draft.comment = String(repeating: "я", count: 1001)
        XCTAssertFalse(draft.valid)
        draft.receive(context: 2, event: "e", disposition: "unreviewed", comment: "another project")
        XCTAssertEqual(draft.comment, "another project")
    }

    func testPreflightEvidenceExpiresAndFailureInvalidatesIt() {
        var evidence = NativeConsolePreflightEvidence()
        let now = ContinuousClock.now
        XCTAssertFalse(evidence.isCurrent(at: now))
        evidence.verify(at: now)
        XCTAssertTrue(evidence.isCurrent(at: now.advanced(by: .seconds(15))))
        XCTAssertFalse(evidence.isCurrent(at: now.advanced(by: .seconds(16))))
        evidence.invalidate()
        XCTAssertFalse(evidence.isCurrent(at: now))
    }
    func testMegahertzInputKeepsExactHertzAndRejectsAmbiguousNotation() {
        XCTAssertEqual(NativeConsoleFrequency.hertz("88,700001"), "88700001")
        XCTAssertEqual(NativeConsoleFrequency.hertz("800.000000"), "800000000")
        for value in ["88e6", "88,700,001", "88.0000001", "NaN", "", "800.000001", "0.01"] {
            XCTAssertNil(NativeConsoleFrequency.hertz(value), value)
        }
    }
    func testLegacyTabsKeepArchiveAndExposeConnectionSettings() {
        XCTAssertEqual(NativeConsoleDestination(legacyTab: "archive").page, .archive)
        XCTAssertEqual(NativeConsoleDestination(legacyTab: "devices").settings, .connection)
        XCTAssertEqual(NativeConsoleDestination(legacyTab: "guard").settings, .guardSettings)
        for tab in ["range", "signals", "", "future-tab"] {
            XCTAssertEqual(NativeConsoleDestination(legacyTab: tab).page, .observation)
        }
    }

    func testExactContextDoesNotNormalizeUnicodeOrMixServers() throws {
        let first = NativeConsoleContext(server: "https://one.test", project: "é", session: "s", stream: "tiny")
        let second = NativeConsoleContext(server: "https://one.test", project: "e\u{0301}", session: "s", stream: "tiny")
        let third = NativeConsoleContext(server: "https://two.test", project: "é", session: "s", stream: "tiny")
        var navigation = NativeConsoleNavigation()
        navigation.activate(first)
        navigation.destination.page = .archive
        navigation.activate(second)
        XCTAssertEqual(navigation.destination.page, .observation)
        navigation.activate(third)
        XCTAssertEqual(navigation.destination.page, .observation)
        navigation.activate(first)
        XCTAssertEqual(navigation.destination.page, .archive)
        let restored = try JSONDecoder().decode(NativeConsoleNavigation.self, from: JSONEncoder().encode(navigation))
        XCTAssertEqual(restored.destination.page, .archive)
    }

    func testWorkspaceCacheIsBoundedAndKeepsTheCurrentContext() {
        var navigation = NativeConsoleNavigation()
        for index in 0..<100 {
            navigation.activate(.init(server: "https://core.test", project: String(index), session: "s", stream: "tiny"))
            navigation.destination.page = .archive
        }
        XCTAssertLessThanOrEqual(navigation.savedContextCount, 32)
        navigation.activate(.init(server: "https://core.test", project: "99", session: "s", stream: "tiny"))
        XCTAssertEqual(navigation.destination.page, .archive)
    }

    func testUnknownStatusAndBlockedStorageCannotOfferArm() {
        XCTAssertEqual(NativeConsoleNextAction.resolve(coreReachable: false, projectSelected: true,
            statusCurrent: true, armed: true, writesBlocked: false), .connection)
        XCTAssertEqual(NativeConsoleNextAction.resolve(coreReachable: true, projectSelected: true,
            statusCurrent: false, armed: true, writesBlocked: false), .verifyStatus)
        XCTAssertEqual(NativeConsoleNextAction.resolve(coreReachable: true, projectSelected: true,
            statusCurrent: true, armed: true, writesBlocked: true), .storage)
        XCTAssertEqual(NativeConsoleNextAction.resolve(coreReachable: true, projectSelected: false,
            statusCurrent: false, armed: false, writesBlocked: false), .preparation)
        XCTAssertEqual(NativeConsoleNextAction.resolve(coreReachable: true, projectSelected: true,
            statusCurrent: true, armed: true, writesBlocked: false), .events)
    }
}
