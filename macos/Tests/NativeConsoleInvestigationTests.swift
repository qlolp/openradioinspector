import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeConsoleInvestigationTests: XCTestCase {
    @MainActor func testRoundTripWhileCreateIsInFlightCannotSubmitTwice() async throws {
        try await roundTrip(suspendEnqueue: false)
    }

    @MainActor func testRoundTripWhileEnqueueIsInFlightCannotSubmitTwice() async throws {
        try await roundTrip(suspendEnqueue: true)
    }

    @MainActor private func roundTrip(suspendEnqueue: Bool) async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        let a = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "a",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        let b = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "b",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        model.configure(a)
        let started = expectation(description: "POST in flight")
        var continuation: CheckedContinuation<String, Never>?
        let first = Task { @MainActor in
            try await model.enqueue(scope: a, original: original, startHz: 100_000_000, stopHz: 100_000_000,
                mode: nil, create: { _ in
                    if !suspendEnqueue { return await withCheckedContinuation { continuation = $0; started.fulfill() } }
                    return "candidate"
                }, enqueue: { _, _ in
                    if suspendEnqueue { return await withCheckedContinuation { continuation = $0; started.fulfill() } }
                    return "investigation"
                })
        }
        await fulfillment(of: [started], timeout: 2)
        model.configure(b); model.configure(a)
        XCTAssertTrue(model.pending, "An exact-scope in-flight POST outlives navigation")
        try await model.enqueue(scope: a, original: original, startHz: 100_000_000, stopHz: 100_000_000,
            mode: nil, create: { _ in XCTFail("Duplicate create"); return "bad" },
            enqueue: { _, _ in XCTFail("Duplicate enqueue"); return "bad" })
        continuation?.resume(returning: suspendEnqueue ? "investigation" : "candidate")
        try await first.value
        XCTAssertFalse(model.pending)
    }
    private func sweep(_ json: String) throws -> PlatformSweep {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweep.self, from: Data(json.utf8))
    }

    @MainActor func testQueueUsesOriginalPeakAndNeverStartsHardware() async throws {
        let model = NativeConsoleInvestigation()
        var creates = 0, enqueues = 0
        let source = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000,100100000,100200000],"levels":[-10,-40,-20],"level_unit":"dBm"}
        """)
        let scope = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        model.configure(scope)
        try await model.enqueue(scope: scope, original: source, startHz: 100_050_000, stopHz: 100_150_000,
            mode: nil, create: { draft in
                creates += 1
                XCTAssertEqual(draft.centerFrequencyHz, 100_100_000)
                XCTAssertEqual(draft.peakLevel, -40)
                XCTAssertEqual(draft.observationIds, ["original"])
                return "candidate"
            }, enqueue: { candidate, draft in
                enqueues += 1
                XCTAssertEqual(candidate, "candidate")
                XCTAssertEqual(draft.targetStreamId, "aor")
                XCTAssertEqual(draft.targetFrequencyHz, 100_100_000)
                return "queued-investigation"
            })
        XCTAssertEqual(model.investigationID, "queued-investigation")
        try await model.enqueue(scope: scope, original: source, startHz: 100_050_000, stopHz: 100_150_000,
            mode: nil, create: { _ in XCTFail("Duplicate candidate"); return "duplicate" },
            enqueue: { _, _ in XCTFail("Duplicate queue command"); return "duplicate" })
        XCTAssertEqual(creates, 1); XCTAssertEqual(enqueues, 1)
    }

    @MainActor func testChangingScopeBetweenCreateAndEnqueueCannotTuneAnotherSession() async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        let scope = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        model.configure(scope)
        try await model.enqueue(scope: scope, original: original, startHz: 100_000_000, stopHz: 100_000_000,
            mode: nil, create: { _ in model.configure(nil); return "candidate" },
            enqueue: { _, _ in XCTFail("Superseded context must not enqueue"); return "bad" })
        XCTAssertNil(model.investigationID)
        XCTAssertFalse(model.pending)
    }

    @MainActor func testReversedEventIntervalCannotCreateOrQueue() async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        let scope = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        model.configure(scope)
        do {
            try await model.enqueue(scope: scope, original: original, startHz: 100_000_000, stopHz: 99_000_000,
                mode: nil, create: { _ in XCTFail("Invalid range must not create"); return "bad" },
                enqueue: { _, _ in XCTFail("Invalid range must not enqueue"); return "bad" })
            XCTFail("Invalid range must be rejected")
        } catch { }
        XCTAssertNil(model.investigationID)
    }

    @MainActor func testUncertainPostCannotBeSilentlyRepeated() async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        let scope = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original")
        model.configure(scope)
        do {
            try await model.enqueue(scope: scope, original: original, startHz: 100_000_000, stopHz: 100_000_000,
                mode: nil, create: { _ in "candidate" }, enqueue: { _, _ in throw URLError(.networkConnectionLost) })
            XCTFail("The lost response must remain a failure")
        } catch { }
        XCTAssertTrue(model.requiresReconciliation)
        XCTAssertFalse(model.pending)
        try await model.enqueue(scope: scope, original: original, startHz: 100_000_000, stopHz: 100_000_000,
            mode: nil, create: { _ in XCTFail("Do not repeat"); return "bad" },
            enqueue: { _, _ in XCTFail("Do not repeat"); return "bad" })
        XCTAssertNil(model.investigationID)
    }

    @MainActor func testCapacityNeverEvictsAnUncertainOutcome() async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        func scope(_ index: Int) -> NativeConsoleInvestigation.Scope {
            .init(server: "https://core.test", project: "p", event: String(index), session: "s",
                  analyzer: "tiny", receiver: "aor", source: "original")
        }
        var creates = 0
        for index in 0...NativeConsoleInvestigation.capacity {
            let key = scope(index); model.configure(key)
            do {
                try await model.enqueue(scope: key, original: original, startHz: 100_000_000, stopHz: 100_000_000,
                    mode: nil, create: { _ in creates += 1; throw URLError(.networkConnectionLost) },
                    enqueue: { _, _ in XCTFail("Unknown candidate cannot enqueue"); return "bad" })
            } catch { }
        }
        XCTAssertEqual(creates, NativeConsoleInvestigation.capacity)
        XCTAssertFalse(model.canEnqueue)
        model.configure(scope(0))
        XCTAssertTrue(model.requiresReconciliation)
        XCTAssertFalse(model.error.isEmpty, "Revisiting an uncertain command must explain its disabled action")
    }

    @MainActor func testVerifiedArchiveReplacementDropsConfirmedHandles() async throws {
        let model = NativeConsoleInvestigation()
        let original = try sweep("""
        {"id":"original","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-11-18T00:00:00Z","ended_at":"2026-11-18T00:00:01Z"},"frequencies_hz":[100000000],"levels":[-40],"level_unit":"dBm"}
        """)
        let key = NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original", runtime: "one")
        model.configure(key)
        try await model.enqueue(scope: key, original: original, startHz: 100_000_000, stopHz: 100_000_000,
            mode: nil, create: { _ in "old-candidate" }, enqueue: { _, _ in "old-investigation" })
        model.configure(nil); model.configure(key)
        XCTAssertEqual(model.investigationID, "old-investigation", "Ordinary reconnect retains the receipt")
        model.invalidateArchiveEvidence()
        XCTAssertNil(model.investigationID)
        XCTAssertTrue(model.canEnqueue)
        XCTAssertNotEqual(key, NativeConsoleInvestigation.Scope(server: "https://core.test", project: "p", event: "e",
            session: "s", analyzer: "tiny", receiver: "aor", source: "original", runtime: "two"))
    }
}
