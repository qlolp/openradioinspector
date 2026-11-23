import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeFirstDutyTests: XCTestCase {
    func testIndependentInstrumentsAndOptionalSecondary() throws {
        var draft = NativeRCPreparationDraft()
        draft.tinySAPort = "/dev/ttyACM0"
        guard case let .tinySA(_, _, secondary) = try draft.validated() else { return XCTFail("Expected tinySA") }
        XCTAssertNil(secondary)
        draft.source = .rfExplorer
        draft.explorer.port = "COM7"
        draft.explorer.applySurveyRange(100_000_000...400_000_000)
        guard case let .rfExplorer(plan, receiver) = try draft.validated() else { return XCTFail("Expected RF Explorer") }
        XCTAssertNil(receiver)
        XCTAssertEqual(plan.plannedSurveyTiles, 268)
        draft.receiverPort = "COM7"
        XCTAssertThrowsError(try draft.validated())
        draft.receiverPort = "COM9"
        guard case let .rfExplorer(_, attached) = try draft.validated() else { return XCTFail("Expected pair") }
        XCTAssertEqual(attached, "COM9")
        draft.source = .ardv1
        draft.startHz = "100000"; draft.stopHz = "1300000000"
        guard case let .ardv1(port, grid) = try draft.validated() else { return XCTFail("Expected single primary receiver") }
        XCTAssertEqual(port, "COM9")
        XCTAssertEqual(grid.pointCount, 129_991)
        XCTAssertTrue(grid.cyclic)
    }

    func testReceiverStepEditsAndRestoresKilohertzWhileThePlanUsesIntegralHertz() throws {
        var draft = NativeRCPreparationDraft()
        draft.source = .ardv1; draft.receiverPort = "COM9"
        XCTAssertEqual(draft.receiverStepKHz, "10")
        XCTAssertEqual(try draft.receiverGrid().stepHz, 10_000)
        draft.restoreReceiverGrid(.object(["start_hz": .integer(100_000), "stop_hz": .integer(100_100), "step_hz": .integer(10)]))
        XCTAssertEqual(draft.receiverStepKHz, "0,01")
        for text in ["0,01", "0.01"] {
            draft.receiverStepKHz = text
            guard case let .ardv1(_, grid) = try draft.validated() else { return XCTFail("Expected AR-DV1") }
            XCTAssertEqual(grid.stepHz, 10); XCTAssertEqual(grid.pointCount, 11)
        }
        for text in ["0,", "0.001", "0.0001", "1,2.3", "0", "1300000,01"] {
            draft.receiverStepKHz = text
            XCTAssertThrowsError(try draft.validated())
            XCTAssertEqual(draft.receiverStepKHz, text)
        }
    }

    func testBaselineRequiresFullPerPointOrWindowEvidence() {
        func check(countKey: String, readyKey: String, ready: Int64) -> NativeBaselineProgress {
            NativeBaselineProgress(preflight: .object(["baseline_collection": .object([
                "ready":.boolean(true),"passes":.integer(20),"minimum_passes":.integer(20),"elapsed_seconds":.integer(90),
                countKey:.integer(3),readyKey:.integer(ready)])]))
        }
        XCTAssertFalse(check(countKey: "windows", readyKey: "ready_windows", ready: 2).ready)
        XCTAssertFalse(check(countKey: "points", readyKey: "ready_points", ready: 2).ready)
        XCTAssertTrue(check(countKey: "points", readyKey: "ready_points", ready: 3).ready)
    }

    func testTinyGridPersistsTheWholeBandAndSeedsOnlyTheFirstPhysicalWindow() throws {
        var draft = NativeRCPreparationDraft()
        draft.tinySAPort = "COM4"; draft.startHz = "100000"; draft.stopHz = "1300000000"
        let plan = try XCTUnwrap(draft.scanPlan())
        let band = try XCTUnwrap(plan["bands"].items.first)
        XCTAssertEqual(band["id"].text, "duty"); XCTAssertEqual(band["step_hz"].integer, 10_000)
        XCTAssertEqual(band["points"].integer, 129_991); XCTAssertEqual(band["stop_hz"].integer, 1_300_000_000)
        guard case let .tinySA(_, first, _) = try draft.validated() else { return XCTFail("Expected tinySA grid") }
        XCTAssertEqual(first.points, 290); XCTAssertEqual(first.stopHz, 2_990_000)
        draft.startHz = "900000000"; draft.stopHz = "902900000"
        guard case let .tinySA(_, tail, _) = try draft.validated() else { return XCTFail("Expected extended grid") }
        XCTAssertEqual(tail.points, 289); XCTAssertEqual(tail.stopHz, 902_880_000)
        draft.stopHz = "902900001"
        XCTAssertThrowsError(try draft.validated())
        draft.tinyUsesGrid = false
        XCTAssertNil(try draft.scanPlan()); XCTAssertThrowsError(try draft.validated())
        draft.receiverPort = "com4"
        XCTAssertThrowsError(try draft.validatedPorts())
    }

    func testTiledBandRequiresEveryEligibleWindowToHaveTwentySweepsAndSixtySeconds() {
        func state(_ ready: Int64, _ count: Int64, _ elapsed: Int64) -> NativeBaselineProgress {
            .init(preflight: .object(["baseline_collection": .object(["ready": .boolean(true), "bands": .array([.object([
                "band_id": .string("duty"), "ready": .boolean(true), "eligible_windows": .integer(3),
                "window_count": .integer(4), "ready_windows": .integer(ready), "sweeps": .integer(count),
                "elapsed_seconds": .integer(elapsed)])])])]))
        }
        XCTAssertFalse(state(2, 20, 65).ready)
        XCTAssertFalse(state(3, 19, 65).ready)
        XCTAssertFalse(state(3, 20, 59).ready)
        let complete = state(3, 20, 60)
        XCTAssertTrue(complete.ready)
        XCTAssertEqual(complete.coverage?.total, 3)
        XCTAssertEqual(complete.sweeps, 20)
    }

    private final class ArchivePeer: URLProtocol {
        static var answer: [String: [String: Any]] = [:]
        static var paths: [String] = []
        static var respond: ((URLRequest) -> [String: Any]?)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            guard let url = request.url else { return }
            let answer = Self.respond.map { $0(request) } ?? Self.answer[url.lastPathComponent]
            Self.paths.append((request.httpMethod ?? "GET") + " " + url.path)
            guard let answer,
                  let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil),
                  let bytes = try? JSONSerialization.data(withJSONObject: [
                    "contract_version": 1, "contract": "openradioinspector-platform", "data": answer]) else {
                client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse)); return
            }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: bytes)
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    func testFirstOriginalFollowsCommittedReceiverIDAndRejectsForeignOrSyntheticData() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArchivePeer.self]
        let client = try PlatformClient(baseURL: "https://fixture.test", session: URLSession(configuration: configuration))
        let run = try XCTUnwrap(NativeDutyRun(.object(["id": .string("p"), "active_session_id": .string("new-run"),
            "primary_stream_id": .string("rx"), "primary_source_kind": .string("ardv1")])))
        let start = "2026-01-01T00:00:00Z", end = "2026-01-01T00:00:01Z"
        let metadata: [String: Any] = ["simulated": false, "adapter":"ardv1-serial", "scan_pass": 1,
            "frequency_readback": true, "frequency_source":"receiver_RX"]
        let original: [String: Any] = ["id": "exact-id", "session_id":"new-run", "stream_id":"rx",
            "frequency_hz":100000, "mode":"FM", "level":2, "level_unit":"s_meter_count",
            "interval":["started_at": start, "ended_at": end], "metadata":metadata]
        ArchivePeer.answer = ["receiver-panorama":["pass":1, "total_points": 2, "measured_points":1, "complete":false,
            "start_hz":100000,"stop_hz":110000,"step_hz":10000,"level_unit":"s_meter_count","lower_is_stronger":true,
            "started_at":start, "ended_at":end, "coverage_hex":"01",
            "bins":[["start_index":0,"end_index":0,"measured_points":1,"frequency_hz":100000,"level":2,
                     "observation_id":"exact-id","started_at":start,"ended_at":end]]], "exact-id":original]
        ArchivePeer.paths = []
        let result = try await NativeDutyOriginal.read(run: run, client: client)
        XCTAssertEqual(result?.id, "exact-id"); XCTAssertEqual(result?.started, start); XCTAssertEqual(result?.ended, end)
        XCTAssertTrue(ArchivePeer.paths.last?.hasSuffix("/sessions/new-run/observations/exact-id") == true)
        for mutation: [String: Any] in [["id":"replaced"], ["session_id":"template"], ["stream_id":"secondary"],
            ["interval":["started_at": start]], ["mode":"AUTO"], ["metadata":metadata.merging(["simulated":true]) { _, new in new }]] {
            ArchivePeer.answer["exact-id"] = original.merging(mutation) { _, new in new }
            do { _ = try await NativeDutyOriginal.read(run: run, client: client); XCTFail("Unexpected original for \(mutation.keys)") }
            catch {}
        }
    }

    @MainActor func testLostScanPlanReplyCanResumeWithoutDuplicateSessionsProjectsOrRevisions() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArchivePeer.self]
        let client = try PlatformClient(baseURL: "https://fixture.test", session: URLSession(configuration: configuration))
        let workspace = MonitoringWorkspaceModel()
        let flow = NativeRCPreparationModel()
        flow.configure(server: "https://fixture.test", client: client, workspace: workspace, sessionID: "")
        flow.draft.tinySAPort = "COM7"
        let encoded = try JSONEncoder().encode(XCTUnwrap(flow.draft.scanPlan()))
        let fullPlan = try XCTUnwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        let project: [String: Any] = ["id":"p", "name":"Test", "site":"Test", "active_session_id":"s",
            "primary_stream_id":"a", "primary_source_kind":"tinysa", "state":"draft"]
        ArchivePeer.paths = []
        ArchivePeer.respond = { request in
            switch (request.httpMethod ?? "GET", request.url?.lastPathComponent) {
            case ("GET", "capabilities"):
                return ["candidate":["mode":"live"], "features":["single_source_duty":true, "scan_plan_grid_10khz":true,
                    "explicit_serial_adapters":true, "monitoring_backup_reserve":true]]
            case ("POST", "sessions"): return ["id":"s", "name":"Test", "state":"created", "streams":[]]
            case ("POST", "monitoring-projects"): return project
            case ("POST", "scan-plan"): return nil // Committed by the node, response lost.
            case ("GET", "scan-plan"): return ["config":fullPlan, "revision":1]
            case ("GET", "health"): return ["last_event_id":0]
            case ("GET", "status"): return ["project":project, "active":false]
            default: return [:]
            }
        }
        defer { ArchivePeer.respond = nil }
        var selected: [String] = []
        await flow.create(workspace: workspace) { selected.append($0) }
        XCTAssertTrue(selected.isEmpty)
        XCTAssertTrue(flow.projectID.isEmpty)
        XCTAssertFalse(flow.error.isEmpty)
        await flow.create(workspace: workspace) { selected.append($0) }
        XCTAssertEqual(selected, ["s"]); XCTAssertEqual(flow.projectID, "p")
        let writes = ArchivePeer.paths.filter { $0.hasPrefix("POST ") }
        XCTAssertEqual(writes, ["POST /api/v1/sessions","POST /api/v1/monitoring-projects","POST /api/v1/monitoring-projects/p/scan-plan"])
        XCTAssertFalse(ArchivePeer.paths.contains { $0.hasSuffix("/preparation/start") })
    }

    func testWeeklySchedulePreservesLocalOvernightWindowAndSortedISODays() throws {
        var schedule = NativeDutyScheduleDraft()
        XCTAssertFalse(schedule.resume)
        schedule.mode = "weekly"; schedule.weekdays = [7, 1]; schedule.timezone = "Europe/Moscow"
        schedule.start = "22:30"; schedule.end = "06:00"
        let saved = try schedule.value()
        XCTAssertEqual(saved["weekdays"].items.map(\.integer), [1,7])
        XCTAssertEqual(saved["timezone"].text, "Europe/Moscow")
        XCTAssertEqual(saved["end_local"].text, "06:00")
        schedule.start = "25:01"
        XCTAssertThrowsError(try schedule.value())
        schedule.mode = "until"; schedule.until = Date(timeIntervalSince1970: 0)
        XCTAssertThrowsError(try schedule.value())
    }

    func testReceiverEventUsesCountsAndImmutableObservationReference() {
        let source = MonitoringValue.object(["session_id":.string("s"),"stream_id":.string("r"),
            "observation_id":.string("o"),"scan_pass":.integer(2),"frequency_hz":.integer(145_000_000)])
        let event = NativeGuardSignal(value: .object(["level_unit":.string("s_meter_count"),"peak_level":.integer(11),
            "excess_counts":.integer(7),"run_session_id":.string("s"),"peak_source":source]))
        XCTAssertTrue(event.peakReading.hasSuffix("s_meter_count"))
        XCTAssertEqual(event.observationSource?.observation, "o")
        XCTAssertEqual(event.observationSource?.archivedEvent, true)
        XCTAssertNil(event.source)
        let foreign = NativeGuardSignal(value: .object(["level_unit":.string("s_meter_count"),"run_session_id":.string("other"),"peak_source":source]))
        XCTAssertNil(foreign.observationSource)
    }
}
