import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class NativeAcceptanceProfilePolicyTests: XCTestCase {
    @MainActor private func profile(_ phase: String, url: String = "http://127.0.0.1:12345",
                                    data: String? = nil, transport: String? = nil) -> NativeAcceptanceTrace.Profile {
        .init(version: 1, runID: "ui-fixture", outputDirectory: "/private/fixture",
              expectedUI: String(repeating: "a", count: 64), coreURL: url,
              sessionID: "s", warmupSessionID: nil, phase: phase,
              coreDataDirectory: data, transport: transport, smokeSeconds: nil)
    }

    @MainActor func testUIAutomationAcceptsOnlyItsOwnLoopbackContextWithoutServiceArchive() throws {
        XCTAssertEqual(try NativeAcceptanceProfilePolicy.origin(for: profile("ui-test")).port, 12345)
        XCTAssertThrowsError(try NativeAcceptanceProfilePolicy.origin(for: profile("ui-test", data: "/private/live")))
        XCTAssertThrowsError(try NativeAcceptanceProfilePolicy.origin(for: profile("ui-test", transport: "binary-v1")))
    }

    @MainActor func testUnknownPurposeAndNonLoopbackOrCredentialedOriginsFailClosed() {
        for value in [profile("unknown"), profile("ui-test", url: "http://example.test:12345"),
                      profile("ui-test", url: "http://user:secret@127.0.0.1:12345"),
                      profile("ui-test", url: "http://127.0.0.1:12345/path")] {
            XCTAssertThrowsError(try NativeAcceptanceProfilePolicy.origin(for: value))
        }
    }

    @MainActor func testHardwarePurposeStillRequiresDedicatedPortAndArchiveScope() throws {
        XCTAssertThrowsError(try NativeAcceptanceProfilePolicy.origin(for: profile("hardware")))
        XCTAssertThrowsError(try NativeAcceptanceProfilePolicy.origin(for: profile("hardware", url: "http://127.0.0.1:8774")))
        XCTAssertEqual(try NativeAcceptanceProfilePolicy.origin(for: profile("hardware",
            url: "http://127.0.0.1:8774", data: "/private/hardware")).port, 8774)
    }
}
