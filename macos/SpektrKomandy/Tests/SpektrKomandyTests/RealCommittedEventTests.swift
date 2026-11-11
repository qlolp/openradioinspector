import Foundation
import XCTest
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif
@testable import SpektrKomandy

/// This launches production Python routes, with an archive created only by this test.
/// Set both ORI_NATIVE_SSE_SOURCE_ROOT and ORI_NATIVE_SSE_SWEEPS=1205 for the
/// integrated durable-core acceptance; the default also runs on the legacy core.
final class RealCommittedEventTests: XCTestCase {
    private enum FixtureError: Error { case failed(String) }
    private final class Fixture {
        let process = Process(), input = Pipe(), output = Pipe()
        private var buffered = Data()
        private var closed = false

        init() throws {
            let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            var environment = ProcessInfo.processInfo.environment
            let source = environment["ORI_NATIVE_SSE_SOURCE_ROOT"] ?? root.path
            let count = environment["ORI_NATIVE_SSE_SWEEPS"] ?? "5"
            for key in Array(environment.keys) where key.hasPrefix("ORI_RUNTIME_") ||
                ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONINSPECT"].contains(key) {
                environment.removeValue(forKey: key)
            }
            environment["PYTHONNOUSERSITE"] = "1"; environment["PYTHONUTF8"] = "1"
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            process.arguments = ["python3", "-u", root.appendingPathComponent("macos/ci/committed_event_fixture.py").path,
                                 "--source-root", source, "--count", count]
            process.environment = environment; process.standardInput = input; process.standardOutput = output
            try process.run()
            FixtureProcessLifecycle.note("started", process: process, label: "committed-sse")
            try input.fileHandleForReading.close(); try output.fileHandleForWriting.close()
        }

        func reply() throws -> [String: Any] {
            let deadline = Date().addingTimeInterval(5)
            while Date() < deadline {
                if let newline = buffered.firstIndex(of: 10) {
                    let line = buffered[..<newline]; buffered.removeSubrange(...newline)
                    return try XCTUnwrap(JSONSerialization.jsonObject(with: line) as? [String: Any])
                }
                var descriptor = pollfd(fd: output.fileHandleForReading.fileDescriptor, events: Int16(POLLIN), revents: 0)
                if poll(&descriptor, 1, 50) > 0 {
                    let bytes = output.fileHandleForReading.availableData
                    if bytes.isEmpty { throw FixtureError.failed("owned Python fixture exited before its reply") }
                    buffered.append(bytes)
                    if buffered.count > 8192 { throw FixtureError.failed("oversized fixture reply") }
                }
            }
            throw FixtureError.failed("owned Python fixture did not reply")
        }
        func command(_ name: String) throws {
            try input.fileHandleForWriting.write(contentsOf: Data("{\"\(name)\":true}\n".utf8))
            let result = try reply()
            XCTAssertEqual(result[name == "restart" ? "restarted" : "appended"] as? Bool, true)
        }
        func close() throws {
            try input.fileHandleForWriting.close()
            let result = try reply()
            XCTAssertEqual(result["closed"] as? Bool, true)
            XCTAssertEqual(result["archive_unchanged"] as? Bool, true)
            FixtureProcessLifecycle.note("final-json-read", process: process, label: "committed-sse")
            let status = try FixtureProcessLifecycle.waitForExit(process, label: "committed-sse")
            XCTAssertEqual(status, 0)
            closed = true
        }
        deinit {
            try? input.fileHandleForWriting.close()
            if !closed && process.processIdentifier > 0 {
                do {
                    try FixtureProcessLifecycle.dispose(process, input: input.fileHandleForWriting,
                                                        output: output.fileHandleForReading, label: "committed-sse")
                } catch { XCTFail("owned SSE fixture cleanup failed: \(error)") }
            } else { try? output.fileHandleForReading.close() }
        }
    }

    func testProductionPythonEnvelopeReplayRestartAndLiveHandoff() async throws {
        let fixture = try Fixture(), ready = try fixture.reply()
        XCTAssertEqual(ready["source_python_clean"] as? Bool, true, "recorded source revision must identify the actual Python implementation")
        let api = try PlatformClient(baseURL: XCTUnwrap(ready["url"] as? String))
        let end = try XCTUnwrap(ready["expected_events"] as? Int64)
        print("[native-real-sse] source=\(ready["source_revision"] ?? "missing") replay_events=\(end) hardware=not_run")
        var live = PlatformLiveMeasurements(sessionID: "synthetic-é", streamID: "a", after: 0, receiverStreamID: "r")
        var sequences: [Int64] = [], opaque: Data?, firstIDs: [[UInt8]] = []
        // Overrun is delivery backpressure: drain the bounded prefix and resume
        // from what was applied, rather than replacing it with a fresh watermark.
        for _ in 0..<128 {
            do {
                for try await event in try api.committedEvents(after: live.cursor) {
                    _ = try live.apply(event); sequences.append(event.sequence)
                    if let sweep = event.payload.sweep {
                        XCTAssertEqual(sweep.levels.map(\.bitPattern), [0x8000000000000000, 0x3ff0000000000001, 0xc059080000000000])
                        XCTAssertEqual(try PlatformTimestamp(sweep.interval.startedAt).utcMicroseconds,
                                       1_794_398_400_000_000 + event.sequence - 2)
                        if firstIDs.count < 2 { firstIDs.append(Array(sweep.id.utf8)) }
                    }
                    if event.eventType == "future.opaque-event" { opaque = event.sourceJSON }
                    if event.sequence == end { break }
                }
            } catch PlatformEventError.consumerOverrun { continue }
            if live.cursor == end { break }
        }
        XCTAssertEqual(sequences, Array(1...end))
        XCTAssertEqual(firstIDs, [Array("é".utf8), Array("e\u{301}".utf8)])
        XCTAssertLessThanOrEqual(live.sweeps.count, PlatformSpectrumPresentation.capacity)
        XCTAssertEqual(live.observation?.level, 137); XCTAssertEqual(live.observation?.levelUnit, "s_meter_count")
        let source = try XCTUnwrap(opaque)
        for literal in ["\"é\"", "\"e\u{301}\"", "36893488147419103233", "-0.0"] {
            XCTAssertNotNil(source.range(of: Data(literal.utf8)))
        }
        try fixture.command("restart")
        try fixture.command("append")
        for try await event in try api.committedEvents(after: live.cursor) {
            _ = try live.apply(event)
            XCTAssertEqual(event.sequence, end + 1)
            XCTAssertEqual(live.cursor, end + 1)
            break
        }
        try fixture.close()
    }
}
