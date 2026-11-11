import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class ResponseCancellationTests: XCTestCase {
    private final class State: @unchecked Sendable {
        let lock = NSLock(), release = DispatchSemaphore(value: 0)
        private var started = false
        private var outcome: String?
        private var invalidations = 0
        func enter() { lock.lock(); started = true; lock.unlock() }
        func finish(_ value: String) { lock.lock(); outcome = value; lock.unlock() }
        func invalidate() { lock.lock(); invalidations += 1; lock.unlock() }
        var snapshot: (Bool, String?, Int) {
            lock.lock(); defer { lock.unlock() }; return (started, outcome, invalidations)
        }
    }

    func testResponseAndPrefixCancellationAfterRegistrationDoesNotWaitForLateTransportCompletion() async throws {
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        // The task is never resumed: callbacks below model a transport that
        // withholds completion until after the cancelled caller has returned.
        let url = URL(string: "http://127.0.0.1:1/owned-cancellation")!
        let transport = session.dataTask(with: url)
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: [:])!
        for completionWins in [false, true] {
            let full = PlatformResponseDelegate(maximumBytes: 64)
            let prefix = PlatformPrefixDelegate(limit: 64)
            let cases: [(any PlatformCompletingResponse, @Sendable () -> Void)] = [
                (full, {
                    full.urlSession(session, dataTask: transport, didReceive: response) { _ in }
                    full.urlSession(session, dataTask: transport, didReceive: Data("late body".utf8))
                    full.urlSession(session, task: transport, didCompleteWithError: nil)
                }),
                (prefix, {
                    prefix.urlSession(session, dataTask: transport, didReceive: response) { _ in }
                    prefix.urlSession(session, dataTask: transport, didReceive: Data("late body".utf8))
                    prefix.urlSession(session, task: transport, didCompleteWithError: nil)
                }),
            ]
            for (delegate, lateCompletion) in cases {
                let state = State()
                let operation = Task.detached {
                    do {
                        _ = try await platformAwaitResponse(delegate, invalidate: { state.invalidate() }, start: {
                            // The cancellation check has passed and the checked
                            // continuation is registered. Hold actual task resume.
                            // Also test an already-queued success racing cancel.
                            if completionWins { lateCompletion() }
                            state.enter()
                            _ = state.release.wait(timeout: .now() + 5)
                        })
                        state.finish("body")
                    } catch is CancellationError { state.finish("cancelled") }
                    catch { state.finish("unexpected error") }
                }
                for _ in 0..<500 {
                    if state.snapshot.0 { break }
                    try await Task.sleep(nanoseconds: 1_000_000)
                }
                XCTAssertTrue(state.snapshot.0, "response registration did not become ready")
                operation.cancel()
                state.release.signal()
                for _ in 0..<500 {
                    if state.snapshot.1 != nil { break }
                    try await Task.sleep(nanoseconds: 1_000_000)
                }
                let beforeLateCompletion = state.snapshot
                // Always release a broken implementation after recording its
                // failure, so this regression itself never leaves a hung task.
                delegate.cancel()
                await operation.value
                XCTAssertEqual(beforeLateCompletion.1, "cancelled", "cancellation awaited a missing transport callback")
                XCTAssertEqual(beforeLateCompletion.2, 1)
                lateCompletion(); lateCompletion()
                XCTAssertEqual(state.snapshot.1, "cancelled", "late response published a body after cancellation")
            }
        }
    }
}
