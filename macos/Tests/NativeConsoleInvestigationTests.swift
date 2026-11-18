import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeConsoleInvestigationTests: XCTestCase {
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
}
