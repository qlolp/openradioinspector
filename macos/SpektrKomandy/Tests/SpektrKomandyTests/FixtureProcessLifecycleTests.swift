import Foundation
import XCTest

final class FixtureProcessLifecycleTests: XCTestCase {
    /// Models the sampled Darwin state: Foundation has published !isRunning,
    /// but its secondary run-loop wait has not received a termination wakeup.
    private final class ExitedProcessWithStalledWait: Process, @unchecked Sendable {
        private let releaseWait = DispatchSemaphore(value: 0)
        override func waitUntilExit() { releaseWait.wait() }
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

    private final class RunningProcessAfterTerminationRequest: Process, @unchecked Sendable {
        private(set) var terminationRequested = false
        override var isRunning: Bool { true }
        override func terminate() { terminationRequested = true }
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
        let process = ExitedProcessWithStalledWait()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", "exit 0"]
        try process.run()
        let exitDeadline = ProcessInfo.processInfo.systemUptime + 5
        while process.isRunning && ProcessInfo.processInfo.systemUptime < exitDeadline {
            Thread.sleep(forTimeInterval: 0.01)
        }
        guard !process.isRunning else {
            process.terminate()
            XCTFail("owned regression child did not exit")
            return
        }
        XCTAssertEqual(process.terminationStatus, 0)
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
