import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class FieldStrengthModelTests: XCTestCase {
    @MainActor func testTotalDeadlineClearsPendingAndRejectsLateSuccessfulReply() async throws {
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../tests/fixtures/field-strength-rms-v2.json")
        let f = try JSONDecoder().decode(MonitoringValue.self, from: Data(contentsOf: fixtureURL))
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let sweep = try decoder.decode(PlatformSweep.self, from: JSONEncoder().encode(f["source"]))
        let result = try decoder.decode(PlatformFieldStrengthResult.self, from: JSONEncoder().encode(f["result"]))
        var release: CheckedContinuation<PlatformFieldStrengthResult, Never>?
        let model = FieldStrengthModel(timeoutNanoseconds: 20_000_000, measure: { _, _, _, _ in
            await withCheckedContinuation { release = $0 }
        })
        model.configure(client: try PlatformClient(baseURL: "http://127.0.0.1:8770"),
            scope: .init(server: "local", session: sweep.sessionId, stream: sweep.streamId), sweeps: [sweep])
        model.frequencyText = "200"; model.calibrationText = f["request"]["calibration"].pretty
        model.analyze()
        for _ in 0..<200 { if !model.pending { break }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTAssertFalse(model.pending)
        XCTAssertNotNil(release)
        XCTAssertNotNil(model.errorText)
        release?.resume(returning: result)
        for _ in 0..<5 { await Task.yield() }
        XCTAssertNil(model.result)
    }
    @MainActor func testUnavailableDraftAndCancelledScopeCannotPublishStaleResult() async throws {
        let fixtureURL = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../tests/fixtures/field-strength-rms-v2.json")
        let fixture = try JSONDecoder().decode(MonitoringValue.self, from: Data(contentsOf: fixtureURL))
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let sweep = try decoder.decode(PlatformSweep.self, from: JSONEncoder().encode(fixture["source"]))
        let result = try decoder.decode(PlatformFieldStrengthResult.self, from: JSONEncoder().encode(fixture["result"]))
        var calls = 0
        var release: CheckedContinuation<PlatformFieldStrengthResult, Never>?
        let model = FieldStrengthModel(measure: { _, _, _, _ in
            calls += 1
            return await withCheckedContinuation { release = $0 }
        })
        let api = try PlatformClient(baseURL: "http://127.0.0.1:8770")
        let scope = FieldStrengthModel.Scope(server: "local", session: sweep.sessionId, stream: sweep.streamId)
        model.configure(client: api, scope: scope, sweeps: [sweep])
        model.analyze()
        XCTAssertEqual(calls, 0)
        XCTAssertNotNil(model.errorText)
        model.frequencyText = "200"; model.calibrationText = fixture["request"]["calibration"].pretty
        model.analyze()
        for _ in 0..<100 { if release != nil { break }; try await Task.sleep(nanoseconds: 1_000_000) }
        XCTAssertEqual(calls, 1)
        model.reset(); release?.resume(returning: result)
        for _ in 0..<5 { await Task.yield() }
        XCTAssertNil(model.result)
        XCTAssertFalse(model.pending)
    }
}
