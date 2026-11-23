import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

extension NativeGuardSignalTests {
    /// Replies can be held after the node accepts a request. Every POST is counted,
    /// including a committed scan plan whose reply has not reached the wizard.
    private final class LiveProfilePeer {
        private let lock = NSLock()
        private var requests: [String] = []
        private var pauses: [String: (skip: Int, entered: XCTestExpectation)] = [:]
        private var held: [String: Stub] = [:]
        private var scanPlan: Any?
        private var armed = false
        private var scheduled = false

        var writes: [String] { lock.withLock { requests.filter { $0.hasPrefix("POST ") } } }
        func setArmed(_ value: Bool) { lock.withLock { armed = value } }
        func setScheduled() { lock.withLock { scheduled = true } }
        func pause(_ key: String, skipping: Int = 0) -> XCTestExpectation {
            let entered = XCTestExpectation(description: "Node accepted " + key)
            lock.withLock { pauses[key] = (skipping, entered) }
            return entered
        }

        func receive(_ stub: Stub) {
            let key = (stub.request.httpMethod ?? "GET") + " " + stub.request.url!.lastPathComponent
            let entered: XCTestExpectation? = lock.withLock {
                requests.append(key)
                if key == "POST scan-plan", let body = stub.submittedBody() {
                    scanPlan = try? JSONSerialization.jsonObject(with: body)
                }
                guard let pause = pauses[key] else { return nil }
                if pause.skip > 0 { pauses[key] = (pause.skip - 1, pause.entered); return nil }
                pauses[key] = nil; held[key] = stub
                return pause.entered
            }
            if let entered { entered.fulfill() } else { answer(key, stub) }
        }

        func release(_ key: String) throws {
            let stub = try XCTUnwrap(lock.withLock { held.removeValue(forKey: key) })
            answer(key, stub)
        }

        private func answer(_ key: String, _ stub: Stub) {
            let draftProject = ["id":"created", "name":"Duty", "site":"Test", "state":"draft",
                                "active_session_id":"session", "primary_stream_id":"a", "primary_source_kind":"tinysa"]
            switch key {
            case "GET capabilities":
                stub.reply(["candidate":["mode":"live"], "features":["explicit_serial_adapters":true,
                    "monitoring_backup_reserve":true, "single_source_duty":true, "scan_plan_grid_10khz":true,
                    "rf_explorer_3g_spectrum":true, "rf_explorer_10khz_survey":true,
                    "ardv1_cyclic_panorama":true, "monitoring_duty_schedule":true]])
            case "GET health": stub.reply(["last_event_id": 38])
            case "GET status":
                if stub.request.url!.pathComponents.dropLast().last == "created" {
                    stub.reply(["active":false, "project":draftProject])
                } else {
                    let active = lock.withLock { armed }
                    stub.reply(["active":active, "project":["id":"previous", "state":active ? "armed" : "draft"]])
                }
            case "GET duty": stub.reply(["enabled":lock.withLock { scheduled }])
            case "POST sessions": stub.reply(["id":"session", "name":"Duty", "state":"created", "streams":[]])
            case "POST monitoring-projects": stub.reply(draftProject)
            case "GET scan-plan", "POST scan-plan":
                stub.reply(lock.withLock { scanPlan.map { ["revision":1, "config":$0] as [String:Any] } } ?? [:])
            case "GET settings", "POST settings":
                stub.reply(["autostart":false, "archive":["minimum_free_bytes":0, "minimum_retention_seconds":3600],
                            "webhook":["enabled":false, "secret_configured":false, "url":""]])
            case "GET storage": stub.reply([:])
            case "GET alerts": stub.reply(["items":[]])
            default: XCTFail("Unexpected profile request " + key); stub.reply([:], status: 404)
            }
        }
    }

    @MainActor private func liveProfile(_ source: NativeDutySource) throws
        -> (LiveProfilePeer, MonitoringWorkspaceModel, NativeRCPreparationModel) {
        let peer = LiveProfilePeer(), workspace = MonitoringWorkspaceModel(), flow = NativeRCPreparationModel()
        Stub.handle = peer.receive
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: "https://node.test", session: URLSession(configuration: configuration))
        flow.configure(server: "https://node.test", client: client, workspace: workspace, sessionID: "")
        workspace.selected = "previous"
        flow.draft.source = source; flow.draft.tinySAPort = "COM1"; flow.draft.receiverPort = "COM2"
        flow.draft.explorer.port = "COM3"
        return (peer, workspace, flow)
    }

    @MainActor func testNativeConsoleLiveProfileRechecksStateAfterBothCapabilityReads() async throws {
        for source in NativeDutySource.allCases {
            let (peer, workspace, flow) = try liveProfile(source)
            await workspace.loadMonitoringCapabilities()
            let entered = peer.pause("GET capabilities", skipping: source == .tinySA ? 0 : 1)
            let creating = Task { await flow.create(workspace: workspace) { _ in XCTFail("Adopted an interrupted profile") } }
            await fulfillment(of: [entered], timeout: 5)
            switch source {
            case .tinySA: peer.setArmed(true); await workspace.refreshStatus()
            case .rfExplorer: peer.setScheduled(); await workspace.refreshDuty()
            case .ardv1:
                // The other write even finishes before the capability reply arrives.
                await workspace.saveOperationsSettings(body: ["autostart":.boolean(false)])
            }
            try peer.release("GET capabilities")
            await creating.value
            XCTAssertEqual(peer.writes, source == .ardv1 ? ["POST settings"] : [])
            XCTAssertTrue(flow.error.contains(source == .ardv1 ? "команда проекта" : "дежурство"), flow.error)
            XCTAssertEqual(workspace.selected, "previous"); XCTAssertTrue(flow.projectID.isEmpty)
        }
    }

    @MainActor func testNativeConsoleResumesEachAcknowledgedLiveProfileWriteAfterInterruption() async throws {
        let (peer, workspace, flow) = try liveProfile(.tinySA)
        let session = "POST sessions", project = "POST monitoring-projects", plan = "POST scan-plan"
        let stages = [(session, [session]), (project, [session, project]),
                      ("GET scan-plan", [session, project]), (plan, [session, project, plan])]
        for (key, expectedWrites) in stages {
            let entered = peer.pause(key)
            let creating = Task { await flow.create(workspace: workspace) { _ in XCTFail("Adopted before all checks") } }
            await fulfillment(of: [entered], timeout: 5)
            peer.setArmed(true); await workspace.refreshStatus()
            try peer.release(key)
            await creating.value
            XCTAssertEqual(peer.writes, expectedWrites); XCTAssertTrue(flow.error.contains("дежурство"), flow.error)
            XCTAssertEqual(workspace.selected, "previous"); XCTAssertTrue(flow.projectID.isEmpty)
            peer.setArmed(false); await workspace.refreshStatus()
        }
        let poll = peer.pause("GET alerts")
        let alerts = Task { await workspace.pollAlerts() }
        await fulfillment(of: [poll], timeout: 5)
        var selected: [String] = []
        await flow.create(workspace: workspace) { selected.append($0) }
        try peer.release("GET alerts"); await alerts.value
        XCTAssertTrue(flow.error.isEmpty, flow.error); XCTAssertEqual(selected, ["session"])
        XCTAssertEqual(flow.projectID, "created"); XCTAssertEqual(workspace.selected, "created")
        XCTAssertEqual(peer.writes, [session, project, plan])
    }

    @MainActor func testNativeConsoleLiveProfileStopsIfOperatorReselectsProjectDuringCapabilityRead() async throws {
        let (peer, workspace, flow) = try liveProfile(.tinySA)
        let entered = peer.pause("GET capabilities")
        let creating = Task { await flow.create(workspace: workspace) { _ in XCTFail("Unexpected selection") } }
        await fulfillment(of: [entered], timeout: 5)
        workspace.selected = "another"
        workspace.selected = "previous"
        try peer.release("GET capabilities"); await creating.value
        XCTAssertTrue(flow.error.contains("проект или узел")); XCTAssertTrue(peer.writes.isEmpty)
        XCTAssertEqual(workspace.selected, "previous")
    }

    @MainActor func testNativeConsoleLiveProfileStopsWhileAnotherProjectWriteIsInFlight() async throws {
        let (peer, workspace, flow) = try liveProfile(.tinySA)
        let capability = peer.pause("GET capabilities"), settings = peer.pause("POST settings")
        let creating = Task { await flow.create(workspace: workspace) { _ in XCTFail("Unexpected selection") } }
        await fulfillment(of: [capability], timeout: 5)
        let saving = Task { await workspace.saveOperationsSettings(body: ["autostart": .boolean(false)]) }
        await fulfillment(of: [settings], timeout: 5)
        try peer.release("GET capabilities"); await creating.value
        XCTAssertTrue(flow.error.contains("команда проекта"), flow.error)
        XCTAssertEqual(workspace.selected, "previous"); XCTAssertTrue(flow.projectID.isEmpty)
        XCTAssertEqual(peer.writes, ["POST settings"])
        try peer.release("POST settings"); await saving.value
    }

    @MainActor func testNativeConsoleCreatesLiveProfileOnceWhilePreviousProjectPollsAreHeld() async throws {
        let (peer, workspace, flow) = try liveProfile(.tinySA)
        let status = peer.pause("GET status"), alerts = peer.pause("GET alerts")
        let statusPoll = Task { await workspace.refreshStatus() }
        let alertsPoll = Task { await workspace.pollAlerts() }
        await fulfillment(of: [status, alerts], timeout: 5)
        var selected: [String] = []
        XCTAssertTrue(flow.canSubmitProfile(in: workspace))
        await flow.create(workspace: workspace) { selected.append($0) }
        try peer.release("GET status"); try peer.release("GET alerts")
        await statusPoll.value; await alertsPoll.value
        XCTAssertEqual(selected, ["session"]); XCTAssertTrue(flow.error.isEmpty, flow.error)
        XCTAssertEqual(workspace.selected, "created")
        XCTAssertEqual(workspace.project["id"].text, "created")
        XCTAssertEqual(peer.writes, ["POST sessions", "POST monitoring-projects", "POST scan-plan"])
    }
}
