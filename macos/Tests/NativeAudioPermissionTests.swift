import AVFoundation
import XCTest
@testable import DvoynoyObzorSession

@MainActor
final class NativeAudioPermissionTests: XCTestCase {
    func testRc1NeverReadsOrRequestsMicrophonePermission() async {
        var reads = 0, prompts = 0
        let model = NativeAudioPermission(enabled: false, authorization: { reads += 1; return .authorized },
            request: { prompts += 1; return true })
        XCTAssertEqual(reads, 0)
        model.inspect(); await model.request()
        XCTAssertEqual(model.phase, .unavailable)
        XCTAssertEqual(reads, 0); XCTAssertEqual(prompts, 0)
    }

    func testPermissionIsRequestedOnlyByExplicitActionAndOnlyOnceWhilePending() async {
        var prompts = 0
        var continuation: CheckedContinuation<Bool, Never>?
        let model = NativeAudioPermission(enabled: true, authorization: { .notDetermined }, request: {
            prompts += 1
            return await withCheckedContinuation { continuation = $0 }
        })
        XCTAssertEqual(model.phase, .notRequested)
        XCTAssertEqual(prompts, 0)
        let pending = Task { await model.request() }
        let deadline = ContinuousClock.now.advanced(by: .seconds(3))
        while continuation == nil && ContinuousClock.now < deadline { await Task.yield() }
        XCTAssertNotNil(continuation)
        XCTAssertEqual(model.phase, .requesting)
        await model.request()
        XCTAssertEqual(prompts, 1)
        continuation?.resume(returning: true)
        await pending.value
        XCTAssertEqual(model.phase, .authorized)
    }

    func testDeniedAndRestrictedPermissionsNeverPromptAgain() async {
        var prompts = 0
        let denied = NativeAudioPermission(enabled: true, authorization: { .denied },
            request: { prompts += 1; return true })
        await denied.request()
        XCTAssertEqual(denied.phase, .denied)
        let restricted = NativeAudioPermission(enabled: true, authorization: { .restricted },
            request: { prompts += 1; return true })
        await restricted.request()
        XCTAssertEqual(restricted.phase, .restricted)
        XCTAssertEqual(prompts, 0)
    }

    func testRevocationIsVisibleAndDoesNotInventAnAudioCapture() async {
        var authorization: AVAuthorizationStatus = .authorized
        let model = NativeAudioPermission(enabled: true, authorization: { authorization }, request: { true })
        model.inspect(); XCTAssertEqual(model.phase, .authorized)
        authorization = .denied
        model.inspect(); XCTAssertEqual(model.phase, .denied)
    }
}
