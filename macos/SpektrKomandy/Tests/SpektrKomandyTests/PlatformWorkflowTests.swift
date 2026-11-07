import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class PlatformWorkflowTests: XCTestCase {
    private var sessions: [URLSession] = []

    override func tearDown() {
        for session in sessions { session.finishTasksAndInvalidate() }
        sessions.removeAll()
        Stub.handler = nil
        super.tearDown()
    }

    final class Stub: URLProtocol {
        static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do {
                guard let (response, data) = try Self.handler?(request) else {
                    throw PlatformError.connection("Нет заглушки")
                }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
        override func stopLoading() {}
    }

    private func client(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> PlatformClient {
        Stub.handler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        let session = URLSession(configuration: configuration)
        sessions.append(session)
        return try PlatformClient(
            baseURL: "https://example.test", session: session
        )
    }

    private func response(_ url: URL) -> HTTPURLResponse {
        HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
    }

    private func envelope(_ data: String) -> Data {
        Data("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(data)}".utf8)
    }

    private let completionStatusJSON = """
    {"policy":{"enabled":true,"interval_seconds":0.5,"minimum_listening_seconds":10.0,"minimum_observations":2,"receiver_stream_id":"ar/1"},"runtime":{"last_result":"completed","last_error":null,"last_investigation_id":"i/1","completed_investigations":1,"skipped_investigations":0},"scheduler_alive":true,"policy_persistent":false,"restart_requires_opt_in":true,"automatic_fail_or_cancel":false}
    """

    private let taskSchedulerJSON = """
    {"enabled":true,"scheduler_alive":true,"policy_persistent":false,"restart_requires_opt_in":true,"runtime":{"last_result":"completed","last_error":null,"last_task_name":"task/1","started_tasks":1,"completed_tasks":1,"aborted_tasks":0},"tasks":{"total":1,"running":0,"completed":1}}
    """

    private let taskListJSON = """
    {"items":[{"name":"macos-monitor","status":"completed","start_at":"2026-11-07T12:00:00Z","outcome":"OUTCOME_SUCCESS","equipment":"tinySA Ultra + AR-DV1"}],"count":1}
    """

    private let candidateJSON = """
    {"id":"c/1","session_id":"s/1","source_stream_id":"tiny/1","interval":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"},"center_frequency_hz":433075000,"bandwidth_hz":null,"peak_level":-42.5,"level_unit":"dBm","score":null,"observation_ids":["sw/1"],"state":"new"}
    """

    private let investigationJSON = """
    {"id":"i/1","candidate_id":"c/1","target_stream_id":"ar/1","target_frequency_hz":433075000,"state":"queued","priority":5,"mode":"DMR","created_at":"2026-01-01T00:00:02Z","started_at":null,"ended_at":null,"result_observation_ids":[],"error":null}
    """

    private func statusJSON(enabled: Bool) -> String {
        """
        {"policy":{"enabled":\(enabled),"interval_seconds":1.0,"stream_id":"tiny/1","threshold_dbm":-70.0,"dedup_bandwidth_hz":100000,"cooldown_seconds":60.0,"max_candidates_per_sweep":10},"runtime":{"last_result":"idle","last_error":null,"sweeps_processed":2,"candidates_created":1,"duplicates_suppressed":1},"scheduler_alive":\(enabled),"policy_persistent":false,"restart_requires_opt_in":true,"processes_existing_archive":false,"automatically_enqueues_investigations":false}
        """
    }

    func testCandidateAndQueueKeepRawUnitsAndIntegerHertz() async throws {
        let workflowClient = try client { request in
            let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            if request.url!.absoluteString.hasSuffix("/candidates") {
                XCTAssertEqual(body["source_stream_id"] as? String, "tiny/1")
                XCTAssertEqual(body["center_frequency_hz"] as? Int, 433075000)
                XCTAssertEqual(body["level_unit"] as? String, "dBm")
                XCTAssertEqual(body["peak_level"] as? Double, -42.5)
                XCTAssertEqual(body["observation_ids"] as? [String], ["sw/1"])
                XCTAssertNil(body["bandwidth_hz"])
                return (self.response(request.url!), self.envelope(self.candidateJSON))
            }
            XCTAssertTrue(request.url!.absoluteString.hasSuffix("/candidates/c%2F1/investigations"))
            XCTAssertEqual(body["target_stream_id"] as? String, "ar/1")
            XCTAssertEqual(body["target_frequency_hz"] as? Int, 433075000)
            XCTAssertEqual(body["priority"] as? Int, 5)
            return (self.response(request.url!), self.envelope(self.investigationJSON))
        }
        let interval = PlatformInterval(
            startedAt: "2026-01-01T00:00:00Z", endedAt: "2026-01-01T00:00:01Z"
        )
        let candidate = try await workflowClient.createCandidate(
            sessionID: "s/1",
            draft: PlatformCandidateDraft(
                sourceStreamId: "tiny/1", interval: interval, centerFrequencyHz: 433075000,
                peakLevel: -42.5, levelUnit: "dBm", observationIds: ["sw/1"]
            )
        )
        XCTAssertEqual(candidate.peakLevel, -42.5)
        XCTAssertEqual(candidate.levelUnit, "dBm")
        let investigation = try await workflowClient.enqueueInvestigation(
            sessionID: "s/1", candidateID: candidate.id,
            draft: PlatformInvestigationDraft(
                targetStreamId: "ar/1", targetFrequencyHz: 433075000, mode: "DMR", priority: 5
            )
        )
        XCTAssertEqual(investigation.targetFrequencyHz, 433075000)
        XCTAssertEqual(investigation.priority, 5)

        let serialClient = try client { request in
            let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            let streams = body["streams"] as! [[String: Any]]
            XCTAssertEqual(streams[0]["port"] as? String, "/dev/cu.usbmodem1")
            XCTAssertEqual(streams[1]["port"] as? String, "/dev/cu.serial2")
            let analyzer = streams[0]["settings"] as! [String: Any]
            let receiver = streams[1]["settings"] as! [String: Any]
            XCTAssertEqual(analyzer["adapter"] as? String, "tinysa-serial")
            XCTAssertEqual(analyzer["start_hz"] as? Int, 88_000_000)
            XCTAssertEqual(analyzer["stop_hz"] as? Int, 108_000_000)
            XCTAssertEqual(analyzer["points"] as? Int, 201)
            XCTAssertEqual(receiver["adapter"] as? String, "ardv1-serial")
            XCTAssertEqual(receiver["frequency_hz"] as? Int, 145_500_000)
            let session = """
            {"id":"serial/session","name":"Двойной обзор · serial","state":"draft","streams":[]}
            """
            return (self.response(request.url!), self.envelope(session))
        }
        let serialSession = try await serialClient.createSerialSession(
            name: " Двойной обзор · serial ",
            tinySAPort: "/dev/cu.usbmodem1", ardv1Port: "/dev/cu.serial2"
        )
        XCTAssertEqual(serialSession.id, "serial/session")
        await assertPlatformError(try await serialClient.createSerialSession(
            name: "bad", tinySAPort: "/dev/same", ardv1Port: "/dev/same"
        ))
        await assertPlatformError(try await serialClient.createSerialSession(
            name: "bad", tinySAPort: "sim://tinysa", ardv1Port: "/dev/other"
        ))
    }

    func testStartNextConfirmationAndMinimalIndependentDisable() async throws {
        let client = try client { request in
            let object = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            if request.url!.absoluteString.hasSuffix("/start-next") {
                XCTAssertTrue(request.url!.absoluteString.contains("/investigations/start-next"))
                XCTAssertEqual(object["investigation_id"] as? String, "i/1")
                XCTAssertEqual(object["confirm"] as? Bool, true)
                return (self.response(request.url!), self.envelope(self.investigationJSON))
            }
            XCTAssertEqual(object.count, 1)
            XCTAssertEqual(object["enabled"] as? Bool, false)
            return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false)))
        }
        await assertPlatformError(
            try await client.startNextInvestigation(sessionID: "s/1", investigationID: "i/1", confirm: false)
        )
        let started = try await client.startNextInvestigation(
            sessionID: "s/1", investigationID: "i/1", confirm: true
        )
        XCTAssertEqual(started.id, "i/1")
        let detection = try await client.disableAutomaticDetection(sessionID: "s/1")
        XCTAssertFalse(detection.policy.enabled)
        let handoff = try await client.disableAutomaticHandoff(sessionID: "s/1")
        XCTAssertFalse(handoff.policy.enabled)
    }

    func testAutomaticCompletionPolicyPayloadAndSnapshot() async throws {
        let session = """
        {"id":"s/1","name":"Обзор","state":"running","streams":[{"id":"tiny/1","device":"tinySA","role":"spectrum_analyzer","level_unit":"dBm","state":"connected","capabilities":["sweep"]},{"id":"ar/1","device":"AR-DV1","role":"receiver","level_unit":"s_meter_count","state":"connected","capabilities":["tune"]}]}
        """
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/workers") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/sweeps") || path.hasSuffix("/observations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/candidates") || path.hasSuffix("/investigations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/investigations/next") { return (self.response(request.url!), self.envelope("null")) }
            if path.hasSuffix("/automatic-handoff") || path.hasSuffix("/automatic-detection") {
                return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false)))
            }
            if path.hasSuffix("/automatic-completion") {
                if request.httpMethod == "POST" {
                    let object = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                    if object["enabled"] as? Bool == true {
                        XCTAssertEqual(object["interval_seconds"] as? Double, 0.5)
                        XCTAssertEqual(object["minimum_listening_seconds"] as? Double, 10.0)
                        XCTAssertEqual(object["minimum_observations"] as? Int, 2)
                        XCTAssertEqual(object["receiver_stream_id"] as? String, "ar/1")
                        return (self.response(request.url!), self.envelope(self.completionStatusJSON))
                    }
                    return (self.response(request.url!), self.envelope(
                        self.completionStatusJSON.replacingOccurrences(of: "\"enabled\":true", with: "\"enabled\":false")
                    ))
                }
                return (self.response(request.url!), self.envelope(self.completionStatusJSON))
            }
            return (self.response(request.url!), self.envelope(session))
        }
        let configured = try await client.configureAutomaticCompletion(
            sessionID: "s/1",
            draft: PlatformCompletionPolicyDraft(
                enabled: true, intervalSeconds: 0.5, minimumListeningSeconds: 10,
                minimumObservations: 2, receiverStreamId: "ar/1"
            )
        )
        XCTAssertTrue(configured.policy.enabled)
        XCTAssertEqual(configured.policy.minimumObservations, 2)
        XCTAssertEqual(configured.runtime.completedInvestigations, 1)
        let snapshot = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(snapshot.automaticCompletion?.policy.enabled, true)
        XCTAssertEqual(snapshot.automaticCompletion?.runtime.lastInvestigationId, "i/1")
        XCTAssertEqual(snapshot.automaticCompletion?.automaticFailOrCancel, false)
        let disabled = try await client.disableAutomaticCompletion(sessionID: "s/1")
        XCTAssertFalse(disabled.policy.enabled)
        _ = try await client.configureAutomaticCompletion(
            sessionID: "s/1",
            draft: PlatformCompletionPolicyDraft(
                enabled: nil, intervalSeconds: 1, minimumListeningSeconds: 0,
                minimumObservations: 1, receiverStreamId: nil
            )
        )
    }

    func testTaskSchedulerPayloadSnapshotAndErrorIsolation() async throws {
        let session = """
        {"id":"s/1","name":"Обзор","state":"running","streams":[{"id":"tiny/1","device":"tinySA","role":"spectrum_analyzer","level_unit":"dBm","state":"connected","capabilities":["sweep"]},{"id":"ar/1","device":"AR-DV1","role":"receiver","level_unit":"s_meter_count","state":"connected","capabilities":["tune"]}]}
        """
        var schedulerAvailable = true
        var tasksAvailable = true
        let client = try client { request in
            let path = request.url!.path
            if path == "/api/v1/task-scheduler/tasks" {
                if !tasksAvailable {
                    let response = HTTPURLResponse(
                        url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil
                    )!
                    let data = Data("{\"error\":{\"code\":\"not_found\",\"message\":\"old task list\"}}".utf8)
                    return (response, data)
                }
                return (self.response(request.url!), self.envelope(self.taskListJSON))
            }
            if path == "/api/v1/task-scheduler" {
                if !schedulerAvailable {
                    let response = HTTPURLResponse(
                        url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil
                    )!
                    let data = Data("{\"error\":{\"code\":\"not_found\",\"message\":\"old server\"}}".utf8)
                    return (response, data)
                }
                if request.httpMethod == "POST" {
                    let object = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                    XCTAssertEqual(object.count, 1)
                    XCTAssertEqual(object["enabled"] as? Bool, true)
                }
                return (self.response(request.url!), self.envelope(self.taskSchedulerJSON))
            }
            if path.hasSuffix("/workers") || path.hasSuffix("/sweeps") || path.hasSuffix("/observations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/candidates") || path.hasSuffix("/investigations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/investigations/next") { return (self.response(request.url!), self.envelope("null")) }
            if path.hasSuffix("/automatic-handoff") || path.hasSuffix("/automatic-detection")
                || path.hasSuffix("/automatic-completion") {
                return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false)))
            }
            if path.hasSuffix("/analytics/summary") {
                let summary = """
                {"session_id":"s/1","stream_id":null,"archive_wide":true,"sweeps":{"count":0,"first_started_at":null,"last_started_at":null,"level_units":[],"latest":null,"latest_peak":null},"observations":{"count":0,"first_started_at":null,"last_started_at":null,"level_units":[],"latest":null,"squelch_known":0,"squelch_open":0,"squelch_open_ratio":null}}
                """
                return (self.response(request.url!), self.envelope(summary))
            }
            return (self.response(request.url!), self.envelope(session))
        }
        let configured = try await client.configureTaskScheduler(enabled: true)
        XCTAssertTrue(configured.enabled)
        XCTAssertEqual(configured.runtime.completedTasks, 1)
        XCTAssertEqual(configured.tasks.completed, 1)
        let snapshot = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(snapshot.taskScheduler?.enabled, true)
        XCTAssertEqual(snapshot.taskScheduler?.tasks.completed, 1)
        XCTAssertNil(snapshot.taskSchedulerError)
        XCTAssertEqual(snapshot.taskSchedulerTasks.count, 1)
        XCTAssertEqual(snapshot.taskSchedulerTasks[0].name, "macos-monitor")
        XCTAssertEqual(snapshot.taskSchedulerTasks[0].startAt, "2026-11-07T12:00:00Z")
        XCTAssertEqual(snapshot.taskSchedulerTasks[0].outcome, "OUTCOME_SUCCESS")
        XCTAssertEqual(snapshot.taskSchedulerTasks[0].equipment, "tinySA Ultra + AR-DV1")
        XCTAssertNil(snapshot.taskSchedulerTasksError)
        schedulerAvailable = false
        let isolated = try await client.snapshot(sessionID: "s/1")
        XCTAssertNil(isolated.taskScheduler)
        XCTAssertEqual(isolated.taskSchedulerError, "old server")
        XCTAssertEqual(isolated.taskSchedulerTasks.count, 1)
        XCTAssertNil(isolated.taskSchedulerTasksError)
        XCTAssertEqual(isolated.session.id, "s/1")
        XCTAssertEqual(isolated.workflowErrors, [])
        schedulerAvailable = true
        tasksAvailable = false
        let listIsolated = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(listIsolated.taskScheduler?.enabled, true)
        XCTAssertNil(listIsolated.taskSchedulerError)
        XCTAssertEqual(listIsolated.taskSchedulerTasks, [])
        XCTAssertEqual(listIsolated.taskSchedulerTasksError, "old task list")
    }

    func testTaskListRejectsInconsistentCount() async throws {
        let client = try client { request in
            XCTAssertEqual(request.url!.path, "/api/v1/task-scheduler/tasks")
            return (
                self.response(request.url!),
                self.envelope("{\"items\":[],\"count\":1}")
            )
        }
        do {
            _ = try await client.taskSchedulerTasks()
            XCTFail("Несогласованный count должен быть отклонён")
        } catch PlatformError.invalidResponse(let message) {
            XCTAssertTrue(message.contains("count"))
        }
    }

    func testSnapshotDecodesWorkflowAndIndependentPolicies() async throws {
        let session = """
        {"id":"s/1","name":"Обзор","state":"running","streams":[{"id":"tiny/1","device":"tinySA","role":"spectrum_analyzer","level_unit":"dBm","state":"connected","capabilities":["sweep"]},{"id":"ar/1","device":"AR-DV1","role":"receiver","level_unit":"s_meter_count","state":"connected","capabilities":["tune"]}]}
        """
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/workers") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/sweeps") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/observations") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/candidates") { return (self.response(request.url!), self.envelope("[\(self.candidateJSON)]")) }
            if path.hasSuffix("/investigations/next") { return (self.response(request.url!), self.envelope(self.investigationJSON)) }
            if path.hasSuffix("/investigations") { return (self.response(request.url!), self.envelope("[\(self.investigationJSON)]")) }
            if path.hasSuffix("/automatic-handoff") { return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false))) }
            if path.hasSuffix("/automatic-detection") { return (self.response(request.url!), self.envelope(self.statusJSON(enabled: true))) }
            if path.hasSuffix("/analytics/summary") {
                let summary = """
                {"session_id":"s/1","stream_id":null,"archive_wide":true,"sweeps":{"count":1,"first_started_at":"2026-01-01T00:00:00Z","last_started_at":"2026-01-01T00:00:01Z","level_units":["dBm"],"latest":null,"latest_peak":{"sweep_id":"sw/1","frequency_hz":433075000,"level":-42.5,"level_unit":"dBm"}},"observations":{"count":2,"first_started_at":null,"last_started_at":null,"level_units":["s_meter_count"],"latest":null,"squelch_known":2,"squelch_open":1,"squelch_open_ratio":0.5}}
                """
                return (self.response(request.url!), self.envelope(summary))
            }
            return (self.response(request.url!), self.envelope(session))
        }
        let snapshot = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(snapshot.candidates.first?.levelUnit, "dBm")
        XCTAssertEqual(snapshot.nextInvestigation?.id, "i/1")
        XCTAssertEqual(snapshot.automaticDetection?.policy.enabled, true)
        XCTAssertEqual(snapshot.automaticDetection?.runtime.candidatesCreated, 1)
        XCTAssertEqual(snapshot.automaticHandoff?.policy.enabled, false)
        XCTAssertEqual(snapshot.automaticHandoff?.policyPersistent, false)
        XCTAssertEqual(snapshot.analytics?.sweeps.count, 1)
        XCTAssertEqual(snapshot.analytics?.sweeps.latestPeak?.levelUnit, "dBm")
        XCTAssertEqual(snapshot.analytics?.observations.levelUnits, ["s_meter_count"])
        XCTAssertEqual(snapshot.analytics?.observations.squelchOpenRatio, 0.5)
        XCTAssertNil(snapshot.analyticsError)
    }

    func testOptionalPolicyStreamsEncodeExplicitNull() async throws {
        let client = try client { request in
            let object = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            if request.url!.absoluteString.hasSuffix("/automatic-handoff") {
                XCTAssertFalse(object.keys.contains("enabled"))
                XCTAssertTrue(object.keys.contains("receiver_stream_id"))
                XCTAssertTrue(object["receiver_stream_id"] is NSNull)
            } else if request.url!.absoluteString.hasSuffix("/automatic-completion") {
                XCTAssertFalse(object.keys.contains("enabled"))
                XCTAssertTrue(object["receiver_stream_id"] is NSNull)
            } else {
                XCTAssertFalse(object.keys.contains("enabled"))
                XCTAssertTrue(object.keys.contains("stream_id"))
                XCTAssertTrue(object["stream_id"] is NSNull)
            }
            return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false)))
        }
        _ = try await client.configureAutomaticHandoff(
            sessionID: "s/1",
            draft: PlatformHandoffPolicyDraft(
                enabled: nil, intervalSeconds: 1, minimumPriority: 0,
                receiverStreamId: nil, maxConsecutiveFailures: 3, retryDelaySeconds: 30
            )
        )
        _ = try await client.configureAutomaticDetection(
            sessionID: "s/1",
            draft: PlatformDetectionPolicyDraft(
                enabled: nil, intervalSeconds: 1, streamId: nil, thresholdDbm: -70,
                dedupBandwidthHz: 100000, cooldownSeconds: 60, maxCandidatesPerSweep: 10
            )
        )
        _ = try await client.configureAutomaticCompletion(
            sessionID: "s/1",
            draft: PlatformCompletionPolicyDraft(
                enabled: nil, intervalSeconds: 1, minimumListeningSeconds: 0,
                minimumObservations: 1, receiverStreamId: nil
            )
        )
    }

    func testAWorkflowOverflowDoesNotFreezeCoreSnapshot() async throws {
        let session = """
        {"id":"s/1","name":"Обзор","state":"running","streams":[{"id":"tiny/1","device":"tinySA","role":"spectrum_analyzer","level_unit":"dBm","state":"connected","capabilities":["sweep"]},{"id":"ar/1","device":"AR-DV1","role":"receiver","level_unit":"s_meter_count","state":"connected","capabilities":["tune"]}]}
        """
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/workers") {
                let workers = "[{\"stream_id\":\"tiny/1\",\"state\":\"running\",\"alive\":true,\"last_error\":null}]"
                return (self.response(request.url!), self.envelope(workers))
            }
            if path.hasSuffix("/sweeps") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/observations") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/candidates") {
                return (self.response(request.url!), Data(repeating: 65, count: 2_000_001))
            }
            if path.hasSuffix("/investigations/next") { return (self.response(request.url!), self.envelope("null")) }
            if path.hasSuffix("/investigations") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/automatic-handoff") || path.hasSuffix("/automatic-detection") {
                return (self.response(request.url!), self.envelope(self.statusJSON(enabled: false)))
            }
            return (self.response(request.url!), self.envelope(session))
        }
        let snapshot = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(snapshot.workers.first?.streamId, "tiny/1")
        XCTAssertEqual(snapshot.session.id, "s/1")
        XCTAssertTrue(snapshot.candidates.isEmpty)
        XCTAssertEqual(snapshot.workflowErrors.count, 2)
        XCTAssertTrue(snapshot.workflowErrors[0].contains("candidates"))
        XCTAssertTrue(snapshot.workflowErrors[1].contains("automatic completion"))
    }

    func testReceiverModesFollowDeclaredProfileCapabilities() {
        let serial = PlatformStream(
            id: "ar/1", device: "AR-DV1", role: "receiver",
            levelUnit: "s_meter_count", state: "connected", capabilities: ["tune", "audio"]
        )
        let hardwareDigital = PlatformStream(
            id: "ar/2", device: "AR-DV1", role: "receiver",
            levelUnit: "s_meter_count", state: "connected",
            capabilities: ["tune", "audio", "hardware_decode"]
        )
        XCTAssertEqual(serial.tuneModes, ["FM", "AM"])
        XCTAssertEqual(hardwareDigital.tuneModes, ["FM", "AM", "DMR", "TETRA"])
    }

    func testRejectsInvalidWorkflowValues() async throws {
        XCTAssertThrowsError(try PlatformCandidateDraft(
            sourceStreamId: "tiny", interval: PlatformInterval(startedAt: "a", endedAt: "b"),
            centerFrequencyHz: 0, levelUnit: "dBm"
        ))
        XCTAssertThrowsError(try PlatformDetectionPolicyDraft(
            enabled: true, intervalSeconds: .nan, streamId: "tiny", thresholdDbm: -70,
            dedupBandwidthHz: 1000, cooldownSeconds: 0, maxCandidatesPerSweep: 1
        ))
        XCTAssertThrowsError(try PlatformHandoffPolicyDraft(
            enabled: true, intervalSeconds: 1, minimumPriority: -1,
            receiverStreamId: nil, maxConsecutiveFailures: 3, retryDelaySeconds: 30
        ))
        XCTAssertThrowsError(try PlatformCompletionPolicyDraft(
            enabled: true, intervalSeconds: 0.01, minimumListeningSeconds: -1,
            minimumObservations: 0, receiverStreamId: nil
        ))
        let client = try client { request in
            let malformed = self.candidateJSON.replacingOccurrences(of: "433075000", with: "0")
            return (self.response(request.url!), self.envelope("[\(malformed)]"))
        }
        await assertPlatformError(try await client.listCandidates(sessionID: "s/1"))
    }

    private func assertPlatformError<T>(
        _ expression: @autoclosure () async throws -> T,
        file: StaticString = #filePath, line: UInt = #line
    ) async {
        do {
            _ = try await expression()
            XCTFail("Ожидалась PlatformError", file: file, line: line)
        } catch is PlatformError {
        } catch {
            XCTFail("Другая ошибка: \(error)", file: file, line: line)
        }
    }
}
