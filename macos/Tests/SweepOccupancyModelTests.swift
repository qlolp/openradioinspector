import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite @MainActor
struct SweepOccupancyModelTests {
    private func fixture() throws -> (PlatformSweepOccupancy, PlatformSweep) {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<3 { url.deleteLastPathComponent() }
        let bytes = try Data(contentsOf: url.appendingPathComponent("tests/fixtures/sweep-occupancy-v1.json"))
        let analysis = try PlatformSweepOccupancy.validatedResponse(bytes)
        let envelope = try #require(JSONSerialization.jsonObject(with: bytes) as? [String: Any])
        let data = try #require(envelope["data"] as? [String: Any])
        var source = try #require((data["items"] as? [[String: Any]])?[2])
        source["session_id"] = data["session_id"]; source["stream_id"] = data["stream_id"]
        source["frequencies_hz"] = data["frequencies_hz"]
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return (analysis, try decoder.decode(PlatformSweep.self, from: JSONSerialization.data(withJSONObject: source)))
    }
    private func configure(_ model: SweepContextModel, source: PlatformSweep) throws -> PlatformClient {
        let client = try PlatformClient(baseURL: "https://example.test")
        model.thresholdText = "-97"
        model.configure(client: client, scope: .init(server: "local", session: "session", stream: "s"), sweeps: [source])
        return client
    }
    private func wait(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 {
            if predicate() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        Issue.record("Timed out waiting for occupancy state")
    }

    @Test func occupancySelectionPollingAndExactExport() async throws {
        let (analysis, source) = try fixture()
        var calls = 0
        let model = SweepContextModel(fetchOccupancy: { _, session, sweep, before, after, threshold in
            calls += 1
            #expect(session == "session" && sweep == "sweep-3" && before == 2 && after == 2 && threshold == -97)
            return analysis
        })
        let client = try configure(model, source: source)
        model.load(withOccupancy: true); model.load(withOccupancy: true)
        try await wait { !model.pending }
        #expect(calls == 1 && model.occupancyResult == analysis)
        model.configure(client: client, scope: .init(server: "local", session: "session", stream: "s"), sweeps: [])
        #expect(model.occupancyResult == analysis && model.anchor?.id == "sweep-3")
        let ticket = try #require(model.beginExport())
        #expect(model.beginExport() == nil)
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
        defer { try? FileManager.default.removeItem(at: url) }
        model.finishExport(ticket, to: url)
        #expect(try Data(contentsOf: url) == analysis.exportData())
        #expect(!model.exporting)
    }

    @Test func staleExportAndCancelledDialogDoNotWrite() async throws {
        let (analysis, source) = try fixture()
        for action in ["threshold", "reset", "cancel"] {
            let model = SweepContextModel(fetchOccupancy: { _, _, _, _, _, _ in analysis })
            _ = try configure(model, source: source)
            model.load(withOccupancy: true); try await wait { !model.pending }
            let ticket = try #require(model.beginExport())
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".json")
            defer { try? FileManager.default.removeItem(at: url) }
            if action == "threshold" { model.thresholdText = "-90" }
            if action == "reset" { model.reset() }
            model.finishExport(ticket, to: action == "cancel" ? nil : url)
            #expect(!FileManager.default.fileExists(atPath: url.path))
            #expect(!model.exporting)
        }
    }

    @Test func thresholdEditDiscardsLateResponse() async throws {
        let (analysis, source) = try fixture()
        var continuation: CheckedContinuation<PlatformSweepOccupancy, Error>?
        let model = SweepContextModel(fetchOccupancy: { _, _, _, _, _, _ in
            try await withCheckedThrowingContinuation { continuation = $0 }
        })
        _ = try configure(model, source: source)
        model.load(withOccupancy: true); try await wait { continuation != nil }
        model.thresholdText = "-90"
        continuation?.resume(returning: analysis)
        for _ in 0..<10 { await Task.yield() }
        #expect(model.result == nil && model.occupancyResult == nil && !model.pending)
        #expect(model.beginExport() == nil)
    }

    @Test func invalidAndMismatchedThresholdNeverBecomeResults() async throws {
        let (analysis, source) = try fixture()
        var calls = 0
        let model = SweepContextModel(fetchOccupancy: { _, _, _, _, _, _ in calls += 1; return analysis })
        _ = try configure(model, source: source)
        for text in ["", "NaN", "inf", "1e999"] {
            model.thresholdText = text; model.load(withOccupancy: true)
            #expect(!model.pending && model.errorText != nil)
        }
        #expect(calls == 0)
        model.thresholdText = "-96"; model.load(withOccupancy: true)
        try await wait { !model.pending }
        #expect(calls == 1 && model.errorText != nil && model.occupancyResult == nil)
    }
}
