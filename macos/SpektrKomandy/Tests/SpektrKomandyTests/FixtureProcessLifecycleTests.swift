import Foundation
import XCTest

final class FixtureProcessLifecycleTests: XCTestCase {
    /// Models the sampled Darwin state: Foundation has published !isRunning,
    /// but its secondary run-loop wait has not received a termination wakeup.
    private final class ExitedProcessWithStalledWait: FixtureManagedProcess, @unchecked Sendable {
        private let process: Process
        private let releaseWait = DispatchSemaphore(value: 0)
        init(_ process: Process) { self.process = process }
        var processIdentifier: Int32 { process.processIdentifier }
        var isRunning: Bool { process.isRunning }
        var terminationStatus: Int32 { process.terminationStatus }
        func terminate() { process.terminate() }
        func waitUntilExit() { releaseWait.wait() }
        func release() { releaseWait.signal() }
    }

    private final class Outcome: @unchecked Sendable {
        private let lock = NSLock()
        private var result: Result<Int32, Error>?
        func store(_ value: Result<Int32, Error>) {
            lock.lock(); result = value; lock.unlock()
        }
        func value() -> Result<Int32, Error>? {
            lock.lock(); defer { lock.unlock() }; return result
        }
    }

    private final class RunningProcessAfterTerminationRequest: FixtureManagedProcess, @unchecked Sendable {
        private(set) var terminationRequested = false
        var processIdentifier: Int32 { 0 }
        var isRunning: Bool { true }
        var terminationStatus: Int32 { -1 }
        func terminate() { terminationRequested = true }
        func waitUntilExit() { XCTFail("unexpected unbounded secondary wait") }
    }

    func testFailureTeardownStopsAtItsDeadlineWithoutAnUnboundedWait() throws {
        let process = RunningProcessAfterTerminationRequest()
        let input = Pipe(), output = Pipe()
        defer {
            try? input.fileHandleForReading.close()
            try? output.fileHandleForWriting.close()
        }
        let started = ProcessInfo.processInfo.systemUptime
        XCTAssertThrowsError(try FixtureProcessLifecycle.dispose(
            process, input: input.fileHandleForWriting, output: output.fileHandleForReading, timeout: 0.03
        )) { error in
            guard case FixtureProcessLifecycleError.exitDeadline = error else {
                return XCTFail("unexpected teardown error: \(error)")
            }
        }
        XCTAssertTrue(process.terminationRequested)
        XCTAssertLessThan(ProcessInfo.processInfo.systemUptime - started, 0.5)
    }

    func testAlreadyExitedProcessDoesNotWaitForAnotherFoundationRunLoopWakeup() throws {
        let owned = Process()
        owned.executableURL = URL(fileURLWithPath: "/bin/sh")
        owned.arguments = ["-c", "exit 0"]
        try owned.run()
        let exitDeadline = ProcessInfo.processInfo.systemUptime + 5
        while owned.isRunning && ProcessInfo.processInfo.systemUptime < exitDeadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        guard !owned.isRunning else {
            owned.terminate()
            XCTFail("owned regression child did not exit")
            return
        }
        XCTAssertEqual(owned.terminationStatus, 0)
        // NSTask is a class cluster on Darwin. Wrap its real concrete object;
        // only the secondary wait is replaced by the controlled stalled event.
        let process = ExitedProcessWithStalledWait(owned)
        let outcome = Outcome()
        let completed = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            outcome.store(Result { try FixtureProcessLifecycle.waitForExit(process, timeout: 0.1) })
            completed.signal()
        }
        let result = completed.wait(timeout: .now() + 0.25)
        // Release even a regressed implementation so this test cannot hang CI.
        process.release()
        if result == .timedOut {
            XCTAssertEqual(completed.wait(timeout: .now() + 1), .success)
        }
        XCTAssertEqual(result, .success,
                       "an observed exit must not wait for a second Foundation termination event")
        XCTAssertEqual(try XCTUnwrap(outcome.value()).get(), 0)
    }
}
