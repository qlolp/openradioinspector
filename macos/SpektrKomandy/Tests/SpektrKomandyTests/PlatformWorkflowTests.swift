import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class PlatformWorkflowTests: XCTestCase {
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
        return try PlatformClient(
            baseURL: "http://example.test", session: URLSession(configuration: configuration)
        )
    }

    private func response(_ url: URL) -> HTTPURLResponse {
        HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
    }

    private func envelope(_ data: String) -> Data {
        Data("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(data)}".utf8)
    }

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
        let client = try client { request in
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
        let candidate = try await client.createCandidate(
            sessionID: "s/1",
            draft: PlatformCandidateDraft(
                sourceStreamId: "tiny/1", interval: interval, centerFrequencyHz: 433075000,
                peakLevel: -42.5, levelUnit: "dBm", observationIds: ["sw/1"]
            )
        )
        XCTAssertEqual(candidate.peakLevel, -42.5)
        XCTAssertEqual(candidate.levelUnit, "dBm")
        let investigation = try await client.enqueueInvestigation(
            sessionID: "s/1", candidateID: candidate.id,
            draft: PlatformInvestigationDraft(
                targetStreamId: "ar/1", targetFrequencyHz: 433075000, mode: "DMR", priority: 5
            )
        )
        XCTAssertEqual(investigation.targetFrequencyHz, 433075000)
        XCTAssertEqual(investigation.priority, 5)
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
            return (self.response(request.url!), self.envelope(session))
        }
        let snapshot = try await client.snapshot(sessionID: "s/1")
        XCTAssertEqual(snapshot.candidates.first?.levelUnit, "dBm")
        XCTAssertEqual(snapshot.nextInvestigation?.id, "i/1")
        XCTAssertEqual(snapshot.automaticDetection?.policy.enabled, true)
        XCTAssertEqual(snapshot.automaticDetection?.runtime.candidatesCreated, 1)
        XCTAssertEqual(snapshot.automaticHandoff?.policy.enabled, false)
        XCTAssertEqual(snapshot.automaticHandoff?.policyPersistent, false)
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
