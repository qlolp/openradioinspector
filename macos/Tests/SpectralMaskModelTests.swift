import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SpectralMaskModelTests: XCTestCase {
    typealias Responses = SweepThresholdModelTests.Responses
    typealias Stub = SweepThresholdModelTests.Stub
    @MainActor final class Deferred {
        var pending: [CheckedContinuation<PlatformSweepMaskAnalysis, Error>] = []
        var sources: [String] = []
        var drafts: [String] = []
        func fetch(_ id: String, _ draft: String) async throws -> PlatformSweepMaskAnalysis {
            sources.append(id); drafts.append(draft)
            return try await withCheckedThrowingContinuation { pending.append($0) }
        }
        func release(_ value: PlatformSweepMaskAnalysis, at index: Int = 0) { pending.remove(at: index).resume(returning: value) }
        func fail(_ error: Error) { pending.removeFirst().resume(throwing: error) }
    }
    func artifact() -> [String: Any] {
        ["format": "openradioinspector-spectral-mask", "format_version": 1, "name": "Synthetic mask",
         "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
         "vertices": [["frequency_hz": 100, "limit_dbm": -80], ["frequency_hz": 500, "limit_dbm": -80]]]
    }
    func result(id: String = "w", stream: String = "tiny", settings: [String: Any]? = nil) throws -> PlatformSweepMaskAnalysis {
        var body = Responses.result(sweepID: id, stream: stream, margin: 10)
        body.removeValue(forKey: "parameters"); body.removeValue(forKey: "baseline_levels")
        body["comparison"] = "strictly_greater"; body["mask"] = artifact()
        if let settings { var source = body["sweep"] as! [String: Any]; source["settings"] = settings; body["sweep"] = source }
        return try decode(body, as: PlatformSweepMaskAnalysis.self)
    }
    func source(id: String = "w", stream: String = "tiny") throws -> PlatformSweep { try decode(Responses.sweep(id, stream: stream), as: PlatformSweep.self) }
    func decode<T: Decodable>(_ object: Any, as: T.Type) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func client(_ base: String = "https://example.test") throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: base, session: URLSession(configuration: config))
    }
    @MainActor func waitFor(_ predicate: @escaping () -> Bool) async throws {
        for _ in 0..<400 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for mask state")
    }
    @MainActor func model(_ deferred: Deferred, timeout: UInt64 = 6_000_000_000) throws -> SpectralMaskModel {
        let model = SpectralMaskModel(timeoutNanoseconds: timeout, fetch: { _, _, id, draft in try await deferred.fetch(id, draft) })
        model.configure(client: try client(), scope: .init(server: "local", session: "s", stream: "tiny"), source: try source())
        model.makeTemplate()
        return model
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }

    @MainActor func testNoImplicitRequestsTemplateRawDraftAndSingleFlight() async throws {
        let d = Deferred(), m = try model(d)
        XCTAssertTrue(m.canMakeTemplate); XCTAssertTrue(m.canAnalyze); XCTAssertNil(m.result)
        XCTAssertTrue(m.draft.contains("NON-NORMATIVE")); XCTAssertEqual(d.sources, [])
        m.draft = "{\"name\":\"a\",\"name\":\"b\"}"; let raw = m.draft
        m.analyze(); m.analyze(); try await waitFor { d.pending.count == 1 }
        XCTAssertEqual(d.drafts, [raw]); XCTAssertEqual(d.sources, ["w"])
        d.fail(PlatformError.http(status: 400, code: "invalid_request", message: "Duplicate field"))
        try await waitFor { !m.pending }; XCTAssertNil(m.result); XCTAssertNotNil(m.errorText)
        XCTAssertEqual(d.sources.count, 1)
    }
    @MainActor func testLatestSourceFrozenResultAndRepeatedExplicitAnalysis() async throws {
        let d = Deferred(), m = SpectralMaskModel(fetch: { _, _, id, draft in try await d.fetch(id, draft) })
        let c = try client(), scope = SpectralMaskModel.Scope(server: "local", session: "s", stream: "tiny")
        m.configure(client: c, scope: scope, source: try source()); m.makeTemplate(); m.analyze()
        try await waitFor { d.pending.count == 1 }
        m.configure(client: c, scope: scope, source: try source(id: "new"))
        d.release(try result()); try await waitFor { m.result != nil }
        XCTAssertEqual(m.source?.id, "new"); XCTAssertEqual(m.result?.sweep.id, "w")
        XCTAssertEqual(m.result?.sweep.interval.startedAt, "2026-11-08T01:00:00Z")
        XCTAssertEqual(d.sources, ["w"])
        m.analyze(); try await waitFor { d.pending.count == 1 }
        d.release(try result(id: "new")); try await waitFor { m.result?.sweep.id == "new" }
        XCTAssertEqual(d.sources, ["w", "new"])
    }
    @MainActor func testDraftEditCancelsClearsAndSuppressesLateReplyAndExport() async throws {
        let d = Deferred(), m = try model(d)
        m.analyze(); try await waitFor { d.pending.count == 1 }
        m.draft += " "; XCTAssertFalse(m.pending); XCTAssertNil(m.result)
        d.release(try result()); try await Task.sleep(nanoseconds: 30_000_000)
        XCTAssertNil(m.result)
        m.analyze(); try await waitFor { d.pending.count == 1 }
        d.release(try result()); try await waitFor { m.result != nil }
        let previous = m.result!; m.draft += " "
        let target = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        XCTAssertFalse(m.exportValidated(to: target, expected: previous))
        XCTAssertFalse(FileManager.default.fileExists(atPath: target.path))
        XCTAssertNil(m.result)
    }
    @MainActor func testScopeConnectionCloseCancelAndLateGuards() async throws {
        for change in ["server", "session", "stream", "client", "close", "cancel"] {
            let d = Deferred(), m = SpectralMaskModel(fetch: { _, _, id, draft in try await d.fetch(id, draft) })
            let c = try client(), scope = SpectralMaskModel.Scope(server: "local", session: "s", stream: "tiny")
            m.configure(client: c, scope: scope, source: try source()); m.makeTemplate(); let draft = m.draft
            m.analyze(); try await waitFor { d.pending.count == 1 }
            if change == "close" { m.reset() }
            else if change == "cancel" { m.cancel() }
            else {
                m.configure(client: change == "client" ? try client() : c,
                    scope: .init(server: change == "server" ? "other" : "local", session: change == "session" ? "other" : "s",
                                 stream: change == "stream" ? "other" : "tiny"), source: try source())
            }
            d.release(try result()); try await Task.sleep(nanoseconds: 15_000_000)
            XCTAssertNil(m.result, change); XCTAssertFalse(m.pending); XCTAssertEqual(m.draft, draft)
        }
    }
    @MainActor func testTimeoutNoAutomaticRetryLateReplyAndManualRetry() async throws {
        let d = Deferred(), m = try model(d, timeout: 500_000_000)
        m.analyze(); try await waitFor { d.pending.count == 1 }
        try await waitFor { !m.pending }; XCTAssertNotNil(m.errorText)
        d.release(try result()); try await Task.sleep(nanoseconds: 25_000_000)
        XCTAssertNil(m.result); XCTAssertEqual(d.sources.count, 1)
        m.analyze(); try await waitFor { d.pending.count == 1 }
        d.release(try result()); try await waitFor { m.result != nil }
        XCTAssertEqual(d.sources.count, 2)
    }
    @MainActor func testWrongSourceStreamAndRecordedSettingsRejected() async throws {
        for changed in ["sweep", "stream", "settings"] {
            let d = Deferred(), m = try model(d)
            m.analyze(); try await waitFor { d.pending.count == 1 }
            d.release(try result(id: changed == "sweep" ? "other" : "w", stream: changed == "stream" ? "other" : "tiny",
                                 settings: changed == "settings" ? ["rbw_hz": 2000] : nil))
            try await waitFor { !m.pending }; XCTAssertNil(m.result, changed); XCTAssertNotNil(m.errorText)
        }
    }
    @MainActor func testNestedBooleanNumberProvenanceMismatchAndLegacyMissingSettings() async throws {
        var missing = Responses.sweep(); missing.removeValue(forKey: "settings")
        XCTAssertNil(try decode(missing, as: PlatformSweep.self).settings)
        missing["settings"] = NSNull()
        XCTAssertNil(try decode(missing, as: PlatformSweep.self).settings)
        for (recorded, echoed) in [(true as Any, 1 as Any), (1 as Any, true as Any)] {
            let d = Deferred(), m = SpectralMaskModel(fetch: { _, _, id, draft in try await d.fetch(id, draft) })
            var s = Responses.sweep(); s["settings"] = ["extension": ["enabled": recorded]]
            m.configure(client: try client(), scope: .init(server: "local", session: "s", stream: "tiny"),
                        source: try decode(s, as: PlatformSweep.self))
            m.makeTemplate(); m.analyze(); try await waitFor { d.pending.count == 1 }
            d.release(try result(settings: ["extension": ["enabled": echoed]]))
            try await waitFor { !m.pending }; XCTAssertNil(m.result); XCTAssertNotNil(m.errorText)
        }
    }
    @MainActor func testErrorsArePanelLocalAndNeverUnknownWriteNotices() async throws {
        for (status, code) in [(400, "invalid_request"), (404, "sweep_not_found"), (409, "mask_incompatible"), (413, "request_too_large")] {
            let d = Deferred(), m = try model(d)
            m.analyze(); try await waitFor { d.pending.count == 1 }
            d.fail(PlatformError.http(status: status, code: code, message: "Synthetic")); try await waitFor { !m.pending }
            XCTAssertNil(m.result); XCTAssertNotNil(m.errorText)
            XCTAssertFalse(m.errorText!.contains("неизвест")); XCTAssertEqual(d.sources.count, 1)
        }
    }
    @MainActor func testInvalidSizeAndSourceAndEqualFrequencyTemplate() throws {
        let d = Deferred(), m = try model(d)
        m.draft = String(repeating: "é", count: 32769); m.analyze()
        XCTAssertNotNil(m.errorText); XCTAssertFalse(m.pending); XCTAssertEqual(d.sources.count, 0)
        let c = try client(), scope = SpectralMaskModel.Scope(server: "local", session: "s", stream: "tiny")
        var s = Responses.sweep(); s["frequencies_hz"] = [100, 100, 100, 100, 100]
        m.configure(client: c, scope: scope, source: try decode(s, as: PlatformSweep.self))
        let draft = m.draft; XCTAssertFalse(m.canMakeTemplate); m.makeTemplate(); XCTAssertEqual(m.draft, draft)
        for field in ["level_unit", "stream_id", "settings"] {
            var value = Responses.sweep(); value[field] = field == "settings" ? [:] : "other"
            m.configure(client: c, scope: scope, source: try decode(value, as: PlatformSweep.self)); XCTAssertNil(m.source)
        }
    }
    @MainActor func testIntegrationPollingNeverAnalyzesAndDisconnectClearsPanel() async throws {
        let responses = Responses(); var maskRequests = 0
        Stub.handler = { stub in
            if stub.request.url!.path.hasSuffix("/sweep-mask-analysis") { maskRequests += 1; stub.respond([:], status: 404) }
            else { responses.handle(stub) }
        }
        let remote = RemoteDualModel(makeClient: { try self.client($0) })
        remote.connect(startPolling: false); try await waitFor { remote.spectralMask.source != nil }
        remote.spectralMask.makeTemplate(); let raw = remote.current, oldError = remote.errorText
        responses.configure(sweepID: "new"); await remote.refresh()
        XCTAssertEqual(remote.spectralMask.source?.id, "new"); XCTAssertEqual(maskRequests, 0)
        remote.spectralMask.analyze(); try await waitFor { remote.spectralMask.errorText != nil }
        XCTAssertEqual(maskRequests, 1); XCTAssertEqual(remote.current, raw); XCTAssertEqual(remote.errorText, oldError)
        XCTAssertNil(remote.sweepThreshold.result); XCTAssertNil(remote.sweepComparison)
        remote.disconnect(); XCTAssertNil(remote.spectralMask.source); XCTAssertNil(remote.spectralMask.result)
        XCTAssertFalse(remote.spectralMask.draft.isEmpty)
    }

    func testLocalFileHelpersBoundedUTF8AndCanonicalRoundTrip() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ori-mask-tests-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let input = directory.appendingPathComponent("input.json"), output = directory.appendingPathComponent("output.json")
        let duplicate = "{\"name\":\"a\",\"name\":\"b\"}"
        try Data(duplicate.utf8).write(to: input)
        XCTAssertEqual(try SpectralMaskFiles.readDraft(from: input), duplicate)
        try Data(repeating: 32, count: 65_536).write(to: input)
        XCTAssertEqual(try SpectralMaskFiles.readDraft(from: input).utf8.count, 65_536)
        try Data(repeating: 32, count: 65_537).write(to: input)
        XCTAssertThrowsError(try SpectralMaskFiles.readDraft(from: input))
        try Data([0xff, 0xfe]).write(to: input)
        XCTAssertThrowsError(try SpectralMaskFiles.readDraft(from: input))
        XCTAssertThrowsError(try SpectralMaskFiles.readDraft(from: directory))
        let mask = try result().mask
        XCTAssertFalse(try SpectralMaskFiles.save(mask, to: nil)); XCTAssertFalse(FileManager.default.fileExists(atPath: output.path))
        XCTAssertTrue(try SpectralMaskFiles.save(mask, to: output))
        let saved = try SpectralMaskFiles.readDraft(from: output)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        XCTAssertEqual(try decoder.decode(PlatformSpectralMask.self, from: Data(saved.utf8)), mask)
        XCTAssertFalse(saved.contains("sweep")); XCTAssertFalse(saved.contains("session_id"))
        // Atomic replacement failures must leave an existing destination directory and its contents intact.
        let occupied = directory.appendingPathComponent("occupied.json")
        try FileManager.default.createDirectory(at: occupied, withIntermediateDirectories: true)
        let marker = occupied.appendingPathComponent("marker"); try Data("unchanged".utf8).write(to: marker)
        XCTAssertThrowsError(try SpectralMaskFiles.save(mask, to: occupied))
        XCTAssertEqual(try String(contentsOf: marker), "unchanged")
    }
    @MainActor func testImportCancelAndFailurePreserveDraftAndValidatedExport() async throws {
        let d = Deferred(), m = try model(d)
        m.analyze(); try await waitFor { d.pending.count == 1 }; d.release(try result()); try await waitFor { m.result != nil }
        let result = m.result!, draft = m.draft
        m.importDraft(from: nil); XCTAssertEqual(m.draft, draft); XCTAssertEqual(m.result, result)
        XCTAssertFalse(m.exportValidated(to: nil, expected: result)); XCTAssertEqual(m.result, result)
        m.importDraft(from: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        XCTAssertEqual(m.draft, draft); XCTAssertEqual(m.result, result); XCTAssertNotNil(m.errorText)
    }
}
