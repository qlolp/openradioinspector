import Foundation

enum FixtureProcessLifecycleError: Error {
    case exitDeadline
}

enum FixtureProcessLifecycle {
    static func note(_ stage: String, process: Process, label: String?, status: Int32? = nil) {
        guard let label else { return }
        let boundedLabel = String(label.prefix(80)).replacingOccurrences(of: "\n", with: "_")
        let exit = status.map { " status=\($0)" } ?? ""
        let line = "[sdk-fixture] \(boundedLabel) pid=\(process.processIdentifier) stage=\(stage)\(exit)\n"
        // Write the short lifecycle marker directly; XCTest stdout may still
        // be buffered when a hosted process sample is taken.
        try? FileHandle.standardError.write(contentsOf: Data(line.utf8))
    }

    static func waitForExit(_ process: Process, timeout: TimeInterval = 5,
                            label: String? = nil) throws -> Int32 {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while process.isRunning {
            let remaining = deadline - ProcessInfo.processInfo.systemUptime
            guard remaining > 0 else {
                note("exit-deadline", process: process, label: label)
                throw FixtureProcessLifecycleError.exitDeadline
            }
            Thread.sleep(forTimeInterval: min(0.01, remaining))
        }
        // Foundation has already published termination. Calling waitUntilExit
        // again can enter an unrelated worker-thread run loop indefinitely on
        // Darwin, even though isRunning is false and the status is available.
        let status = process.terminationStatus
        note("exit-observed", process: process, label: label, status: status)
        return status
    }

    static func dispose(_ process: Process, input: FileHandle, output: FileHandle,
                        timeout: TimeInterval = 5, label: String? = nil) throws {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        defer { try? output.close() }
        try? input.close()
        if process.isRunning {
            note("fallback-terminate", process: process, label: label)
            process.terminate() // The exact Process created by this fixture.
        }
        _ = try waitForExit(process,
                           timeout: max(0, deadline - ProcessInfo.processInfo.systemUptime), label: label)
    }
}
