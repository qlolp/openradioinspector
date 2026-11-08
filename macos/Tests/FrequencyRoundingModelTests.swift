import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class FrequencyRoundingModelTests: XCTestCase {
    typealias Stub = SweepThresholdModelTests.Stub
    typealias Responses = SweepThresholdModelTests.Responses
    @MainActor final class Deferred {
        var pending: [CheckedContinuation<PlatformCandidateFrequencyRounding, Error>] = []
        var ids: [String] = []; var drafts: [String] = []
        func fetch(_ id: String, _ draft: String) async throws -> PlatformCandidateFrequencyRounding {
            ids.append(id); drafts.append(draft)
            return try await withCheckedThrowingContinuation { pending.append($0) }
        }
        func release(_ value: PlatformCandidateFrequencyRounding, at index: Int = 0) { pending.remove(at: index).resume(returning: value) }
        func fail(_ error: Error) { pending.removeFirst().resume(throwing: error) }
    }
    let scope = FrequencyRoundingModel.Scope(server: "local", session: "s")
    func candidate(id: String = "c", session: String = "s", stream: String = "tiny", state: String = "new",
                   frequency: Int64 = 103_025_123, unit: String = "dBm", start: String = "2026-11-08T12:00:00.000001Z") -> [String: Any] {
        ["id": id, "session_id": session, "source_stream_id": stream,
         "interval": ["started_at": start, "ended_at": "2026-11-08T12:00:01.000002Z"],
         "center_frequency_hz": frequency, "level_unit": unit, "state": state, "peak_level": unit == "dBm" ? -65 : 37]
    }
    func decode<T: Decodable>(_ object: Any, as: T.Type) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func source(id: String = "c", state: String = "new", stream: String = "tiny") throws -> PlatformCandidate {
        try decode(candidate(id: id, stream: stream, state: state), as: PlatformCandidate.self)
    }
    @MainActor func result(id: String = "c", session: String = "s", stream: String = "tiny", name: String? = nil,
                          frequency: Int64 = 103_025_123, unit: String = "dBm",
                          start: String = "2026-11-08T12:00:00.000001Z") throws -> PlatformCandidateFrequencyRounding {
        var profile = try JSONSerialization.jsonObject(with: Data(FrequencyRoundingModel.example.utf8)) as! [String: Any]
        if let name { profile["name"] = name }
        var raw = candidate(id: id, session: session, stream: stream, frequency: frequency, unit: unit, start: start)
        for key in ["session_id", "state", "peak_level"] { raw.removeValue(forKey: key) }
        return try decode(["session_id": session, "candidate": raw, "profile": profile, "frequency_unit": "Hz",
                           "method": "nearest_grid", "tie_break": "lower", "result": ["status": "rounded", "rule_index": 0,
                            "nearest_frequency_hz": 103000000, "offset_hz": 103000000 - frequency,
                            "rounded_frequency_hz": 103000000]], as: PlatformCandidateFrequencyRounding.self)
    }
    func client(_ base: String = "https://example.test") throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: base, session: URLSession(configuration: config))
    }
    @MainActor func waitFor(_ predicate: @escaping () -> Bool) async throws {
        for _ in 0..<600 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for rounding state")
    }
    @MainActor func model(_ d: Deferred, timeout: UInt64 = 6_000_000_000) throws -> FrequencyRoundingModel {
        let model = FrequencyRoundingModel(timeoutNanoseconds: timeout, fetch: { _, _, id, draft in try await d.fetch(id, draft) })
        model.configure(client: try client(), scope: scope, candidates: [try source(), try source(id: "receiver", stream: "receiver")])
        return model
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }

    @MainActor func testExplicitOnlySingleFlightRawDraftAndRetainedSelection() async throws {
        let d = Deferred(), m = try model(d), c = try client()
        m.configure(client: c, scope: scope, candidates: [try source(), try source(id: "receiver", stream: "receiver")])
        XCTAssertEqual(d.ids, []); XCTAssertTrue(m.canAnalyze)
        m.analyze(); m.analyze(); try await waitFor { d.pending.count == 1 }
        m.configure(client: c, scope: scope, candidates: [try source(id: "receiver", stream: "receiver")])
        XCTAssertEqual(m.source?.id, "c"); XCTAssertEqual(m.menuChoices.count, 2)
        d.release(try result()); try await waitFor { m.result != nil }
        XCTAssertEqual(m.frozenSource?.id, "c"); XCTAssertEqual(d.ids, ["c"])
        m.draft = "{\"name\":\"a\",\"name\":\"b\"}"; let raw = m.draft
        m.analyze(); try await waitFor { d.pending.count == 1 }
        XCTAssertEqual(d.drafts.last, raw)
        d.fail(PlatformError.http(status: 400, code: "invalid_request", message: "Duplicate"))
        try await waitFor { !m.pending }; XCTAssertNil(m.result); XCTAssertNotNil(m.errorText)
    }
    @MainActor func testMutableStatePollingDoesNotChangeFrozenFrequencyProvenance() async throws {
        let d = Deferred(), m = try model(d), c = try client()
        m.configure(client: c, scope: scope, candidates: [try source()]); m.analyze()
        try await waitFor { d.pending.count == 1 }
        m.configure(client: c, scope: scope, candidates: [try source(state: "dismissed")])
        XCTAssertEqual(m.source?.state, "new"); XCTAssertEqual(m.choices.first?.state, "dismissed")
        d.release(try result()); try await waitFor { m.result != nil }
        XCTAssertEqual(m.frozenSource?.state, "new"); XCTAssertEqual(m.result?.result.offsetHz, -25123)
    }
    @MainActor func testEditSelectClearScopeDisconnectCloseReadinessCancelAndLateReplies() async throws {
        for change in ["edit", "select", "clear", "server", "session", "client", "disconnect", "close", "snapshot", "candidate-error"] {
            let d = Deferred(), m = try model(d), c = try client()
            let values = [try source(), try source(id: "receiver", stream: "receiver")]
            m.configure(client: c, scope: scope, candidates: values); m.analyze()
            try await waitFor { d.pending.count == 1 }
            switch change {
            case "edit": m.draft += " "
            case "select": m.selectSource(id: "receiver")
            case "clear": m.cancel()
            case "disconnect", "close": m.reset()
            case "snapshot": m.unavailable()
            case "candidate-error": m.configure(client: c, scope: scope, candidates: [], ready: false)
            default: m.configure(client: change == "client" ? try client() : c,
                scope: .init(server: change == "server" ? "different" : "local", session: change == "session" ? "different" : "s"), candidates: values)
            }
            XCTAssertFalse(m.pending, change); XCTAssertNil(m.result, change)
            d.release(try result()); try await Task.sleep(nanoseconds: 15_000_000)
            XCTAssertNil(m.result, change)
        }
    }
    @MainActor func testUnicodeScopePickerAndDraftUseExactUTF8() async throws {
        let d = Deferred(), m = try model(d), c = try client()
        let nfc = "é", nfd = "e\u{301}"
        m.configure(client: c, scope: scope, candidates: [try source(id: nfc), try source(id: nfd)])
        XCTAssertEqual(m.choices.count, 2)
        m.selectSource(id: nfd); XCTAssertEqual(Array(m.source!.id.utf8), Array(nfd.utf8))
        m.analyze(); try await waitFor { d.pending.count == 1 }
        d.release(try result(id: nfd)); try await waitFor { m.result != nil }
        m.draft = m.draft.replacingOccurrences(of: "пример", with: nfc)
        m.analyze(); try await waitFor { d.pending.count == 1 }
        m.draft = m.draft.replacingOccurrences(of: nfc, with: nfd)
        d.release(try result(id: nfd)); try await Task.sleep(nanoseconds: 15_000_000); XCTAssertNil(m.result)
        m.configure(client: c, scope: .init(server: nfc, session: "s"), candidates: [try source()]); m.makeTemplate(); m.analyze()
        try await waitFor { d.pending.count == 1 }
        m.configure(client: c, scope: .init(server: nfd, session: "s"), candidates: [try source()])
        d.release(try result()); try await Task.sleep(nanoseconds: 15_000_000); XCTAssertNil(m.result)
    }
    @MainActor func testCoreWhitespaceCandidateScopeAndOriginalUnitReadiness() async throws {
        let d = Deferred(), m = try model(d), c = try client()
        for exact in ["\u{200B}", "\u{FEFF}"] {
            let candidate = try decode(candidate(id: exact, session: exact, stream: exact, unit: exact), as: PlatformCandidate.self)
            let exactScope = FrequencyRoundingModel.Scope(server: "server/" + exact, session: exact)
            m.configure(client: c, scope: exactScope, candidates: [candidate])
            XCTAssertTrue(m.canAnalyze); XCTAssertEqual(m.choices.count, 1)
            for field in [m.source!.id, m.source!.sessionId, m.source!.sourceStreamId, m.source!.levelUnit] {
                XCTAssertEqual(Array(field.utf8), Array(exact.utf8))
            }
            m.analyze(); try await waitFor { d.pending.count == 1 }
            XCTAssertEqual(Array(d.ids.last!.utf8), Array(exact.utf8))
            d.release(try result(id: exact, session: exact, stream: exact, unit: exact))
            try await waitFor { !m.pending }; XCTAssertNotNil(m.result); XCTAssertNil(m.errorText)
            XCTAssertEqual(Array(m.frozenScope!.session.utf8), Array(exact.utf8))
            m.reset()
        }
        for scalar in [0x001C, 0x001D, 0x001E, 0x001F] {
            let blank = String(UnicodeScalar(scalar)!)
            for field in ["id", "session_id", "source_stream_id", "level_unit"] {
                var object = candidate(); object[field] = blank
                m.reset()
                m.configure(client: c, scope: .init(server: "local", session: field == "session_id" ? blank : "s"),
                    candidates: [try decode(object, as: PlatformCandidate.self)])
                XCTAssertFalse(m.canAnalyze, field); XCTAssertNil(m.source, field)
            }
        }
    }

    @MainActor func testFullFrozenProvenanceAndProfileMismatchRejected() async throws {
        for field in ["id", "session", "stream", "frequency", "unit", "microseconds", "profile"] {
            let d = Deferred(), m = try model(d); m.analyze(); try await waitFor { d.pending.count == 1 }
            d.release(try result(id: field == "id" ? "other" : "c", session: field == "session" ? "other" : "s",
                stream: field == "stream" ? "other" : "tiny", name: field == "profile" ? "Other" : nil,
                frequency: field == "frequency" ? 103025124 : 103025123,
                unit: field == "unit" ? "s_meter_count" : "dBm",
                start: field == "microseconds" ? "2026-11-08T12:00:00.000002Z" : "2026-11-08T12:00:00.000001Z"))
            try await waitFor { !m.pending }; XCTAssertNil(m.result, field); XCTAssertNotNil(m.errorText)
        }
    }
    @MainActor func testTimeoutNoRetryLateReplyAndExplicitRetry() async throws {
        let d = Deferred(), m = try model(d, timeout: 500_000_000)
        m.analyze(); try await waitFor { d.pending.count == 1 }; try await waitFor { !m.pending }
        XCTAssertTrue(m.errorText?.contains("истекло") == true)
        d.release(try result()); try await Task.sleep(nanoseconds: 15_000_000); XCTAssertNil(m.result)
        XCTAssertEqual(d.ids.count, 1)
        m.analyze(); try await waitFor { d.pending.count == 1 }; d.release(try result()); try await waitFor { m.result != nil }
        XCTAssertEqual(d.ids.count, 2)
    }
    @MainActor func testBadSizeUnsupportedSourcesAndErrorsRemainLocal() async throws {
        let d = Deferred(), m = try model(d), c = try client()
        m.draft = String(repeating: "é", count: 32769); m.analyze()
        XCTAssertNotNil(m.errorText); XCTAssertFalse(m.pending); XCTAssertEqual(d.ids.count, 0)
        for object in [candidate(id: String(repeating: "a", count: 129)), candidate(session: "other"),
                       candidate(frequency: 9007199254740992), candidate(unit: " "), candidate(start: "bad")] {
            m.reset(); m.configure(client: c, scope: scope, candidates: [try decode(object, as: PlatformCandidate.self)])
            XCTAssertFalse(m.canAnalyze); XCTAssertNil(m.source)
        }
        for status in [400, 404, 409, 413] {
            m.configure(client: c, scope: scope, candidates: [try source()]); m.makeTemplate(); m.analyze()
            try await waitFor { d.pending.count == 1 }
            d.fail(PlatformError.http(status: status, code: "failure", message: "Synthetic"))
            try await waitFor { !m.pending }; XCTAssertNotNil(m.errorText); XCTAssertNil(m.result)
            XCTAssertFalse(m.errorText!.contains("неизвест"))
        }
    }
    @MainActor func testFileLimitsUTF8AtomicFailureAndCanonicalExport() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rounding-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let input = dir.appendingPathComponent("in.json"), output = dir.appendingPathComponent("out.json")
        for (data, valid) in [(Data(repeating: 32, count: 65536), true), (Data(repeating: 32, count: 65537), false), (Data([255]), false)] {
            try data.write(to: input)
            if valid { XCTAssertEqual(try FrequencyRoundingFiles.readDraft(from: input).utf8.count, 65536) }
            else { XCTAssertThrowsError(try FrequencyRoundingFiles.readDraft(from: input)) }
        }
        XCTAssertThrowsError(try FrequencyRoundingFiles.readDraft(from: dir))
        let d = Deferred(), m = try model(d); m.analyze(); try await waitFor { d.pending.count == 1 }
        d.release(try result()); try await waitFor { m.result != nil }; let expected = m.result!
        XCTAssertTrue(m.exportValidated(to: output, expected: expected, token: m.beginFileOperation()))
        let text = try String(contentsOf: output)
        XCTAssertEqual(try PlatformFrequencyRoundingProfile.submitted(text), expected.profile)
        XCTAssertFalse(text.contains("candidate")); XCTAssertFalse(text.contains("session_id"))
        XCTAssertFalse(m.exportValidated(to: nil, expected: expected, token: m.beginFileOperation()))
        let occupied = dir.appendingPathComponent("occupied.json")
        try FileManager.default.createDirectory(at: occupied, withIntermediateDirectories: true)
        let marker = occupied.appendingPathComponent("marker"); try Data("kept".utf8).write(to: marker)
        XCTAssertFalse(m.exportValidated(to: occupied, expected: expected, token: m.beginFileOperation()))
        XCTAssertEqual(try String(contentsOf: marker), "kept")
        let draft = m.draft; m.importDraft(from: nil, token: m.beginFileOperation())
        XCTAssertEqual(m.draft, draft); XCTAssertEqual(m.result, expected)
    }
    @MainActor func testStaleImportAndExportTokensNeverTouchNewScopeOrFiles() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("rounding-race-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let input = dir.appendingPathComponent("in.json"), output = dir.appendingPathComponent("out.json")
        try Data("{}".utf8).write(to: input)
        let d = Deferred(), m = try model(d)
        for change in ["edit", "template", "select", "clear", "reset", "unavailable", "new-dialog"] {
            m.configure(client: try client(), scope: scope, candidates: [try source(), try source(id: "receiver")]); m.makeTemplate()
            m.analyze(); try await waitFor { d.pending.count == 1 }; d.release(try result()); try await waitFor { m.result != nil }
            let expected = m.result!, token = m.beginFileOperation()
            switch change {
            case "edit": m.draft += " "
            case "template": m.makeTemplate()
            case "select": m.selectSource(id: "receiver")
            case "clear": m.cancel()
            case "reset": m.reset()
            case "unavailable": m.unavailable()
            default: _ = m.beginFileOperation()
            }
            let draft = m.draft
            m.importDraft(from: input, token: token); XCTAssertEqual(m.draft, draft, change)
            XCTAssertFalse(m.exportValidated(to: output, expected: expected, token: token), change)
            XCTAssertFalse(FileManager.default.fileExists(atPath: output.path), change)
        }
        m.configure(client: try client(), scope: scope, candidates: [try source()]); m.makeTemplate()
        m.analyze(); try await waitFor { d.pending.count == 1 }
        m.importDraft(from: input, token: m.beginFileOperation()); XCTAssertEqual(m.draft, "{}"); XCTAssertFalse(m.pending)
        d.release(try result()); try await Task.sleep(nanoseconds: 15_000_000); XCTAssertNil(m.result)
    }
    @MainActor func testIntegrationSnapshotAndCandidateReadFailuresDisableReadiness() async throws {
        let responses = Responses(); var candidateFailure = false, snapshotFailure = false, roundingRequests = 0
        Stub.handler = { stub in
            let path = stub.request.url!.path
            if path.hasSuffix("/candidate-frequency-rounding") { roundingRequests += 1; stub.respond([:], status: 404) }
            else if path.hasSuffix("/candidates") { stub.respond(candidateFailure ? [] : [self.candidate()], status: candidateFailure ? 404 : 200) }
            else if path == "/api/v1/sessions/s" && snapshotFailure { stub.respond([:], status: 404) }
            else { responses.handle(stub) }
        }
        let remote = RemoteDualModel(makeClient: { try self.client($0) }); remote.connect(startPolling: false)
        defer { remote.disconnect() }
        try await waitFor { remote.frequencyRounding.canAnalyze }; XCTAssertEqual(roundingRequests, 0)
        let selected = remote.selectedCandidateID, raw = remote.current
        remote.frequencyRounding.analyze(); try await waitFor { !remote.frequencyRounding.pending }
        XCTAssertEqual(roundingRequests, 1); XCTAssertEqual(remote.selectedCandidateID, selected); XCTAssertEqual(remote.current, raw)
        candidateFailure = true; await remote.refresh(); XCTAssertFalse(remote.frequencyRounding.canAnalyze)
        candidateFailure = false; await remote.refresh(); XCTAssertTrue(remote.frequencyRounding.canAnalyze)
        snapshotFailure = true; await remote.refresh(); XCTAssertFalse(remote.frequencyRounding.canAnalyze)
        snapshotFailure = false; await remote.refresh(); XCTAssertTrue(remote.frequencyRounding.canAnalyze)
        remote.serverURL = "https://different.test"; XCTAssertNil(remote.frequencyRounding.source)
        XCTAssertFalse(remote.frequencyRounding.canAnalyze)
    }
    @MainActor func testActualHTTPNativeRoundingAllStatusesAndSessionSwitch() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_ROUNDING_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_ROUNDING_SMOKE_BASE_URL to synthetic fixture") }
        let remote = RemoteDualModel(); remote.serverURL = base; remote.connect(startPolling: false)
        defer { remote.disconnect() }
        try await waitFor { remote.frequencyRounding.ready }
        remote.selectSession("rounding-one"); try await waitFor { remote.frequencyRounding.choices.contains { $0.id == "one-rounded" } }
        let m = remote.frequencyRounding, raw = remote.current, selected = remote.selectedCandidateID
        for (id, status, nearest, offset) in [
            ("rounded", "rounded", 103000000 as Int64?, -25123 as Int64?),
            ("unchanged", "unchanged", 103000000, 0), ("tolerance", "outside_tolerance", 103000000, -50000),
            ("outside", "outside_ranges", nil, nil), ("receiver", "rounded", 145500000, -6250),
            ("upper", "rounded", 103100000, 25000)] {
            m.selectSource(id: "one-" + id); m.analyze(); try await waitFor { !m.pending }
            XCTAssertNil(m.errorText, id); XCTAssertEqual(m.result?.result.status, status, id)
            XCTAssertEqual(m.result?.result.nearestFrequencyHz, nearest, id); XCTAssertEqual(m.result?.result.offsetHz, offset, id)
            XCTAssertEqual(m.result?.candidate.interval.startedAt, "2026-11-08T12:00:00.000001Z")
            XCTAssertEqual(m.result?.candidate.levelUnit, id == "receiver" ? "s_meter_count" : "dBm")
        }
        let frozen = m.result; await remote.refresh(); XCTAssertEqual(m.result, frozen)
        XCTAssertEqual(remote.current, raw); XCTAssertEqual(remote.selectedCandidateID, selected)
        remote.selectSession("rounding-two"); XCTAssertNil(m.result)
        try await waitFor { m.choices.contains { $0.id == "two-rounded" } }
        m.selectSource(id: "two-rounded"); m.analyze(); try await waitFor { !m.pending }
        XCTAssertEqual(m.result?.sessionId, "rounding-two"); XCTAssertEqual(m.result?.result.offsetHz, -25123)
        remote.disconnect(); XCTAssertNil(m.result); XCTAssertNil(m.source); XCTAssertFalse(m.canAnalyze)
    }
}
