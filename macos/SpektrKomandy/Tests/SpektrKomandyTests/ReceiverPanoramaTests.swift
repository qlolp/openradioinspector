import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import SpektrKomandy

@Suite(.serialized)
struct ReceiverPanoramaTests {
    private final class Core: URLProtocol {
        nonisolated(unsafe) static var records: [URLRequest] = []
        nonisolated(unsafe) static var replies: [Data] = []
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func stopLoading() {}
        override func startLoading() {
            do {
                Self.records.append(try bufferedTestRequest(request))
                guard !Self.replies.isEmpty, let url = request.url,
                      let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)
                else { throw URLError(.badServerResponse) }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: Self.replies.removeFirst())
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
    }
    private func peer(_ replies: [[String: Any]]) throws -> PlatformClient {
        Core.records = []
        Core.replies = try replies.map {
            try JSONSerialization.data(withJSONObject: ["contract_version": 1, "contract": "openradioinspector-platform", "data": $0])
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Core.self]
        return try PlatformClient(baseURL: "https://node.test", session: URLSession(configuration: configuration))
    }
    private let first = "2026-01-01T00:00:00Z", end = "2026-01-01T00:00:04Z"
    private func partial() -> [String: Any] {
        // 5 points: measured indices 0, 2, 4; the final step is only 5 kHz.
        ["pass": 2, "total_points": 5, "measured_points": 3, "complete": false,
         "start_hz": 100_000, "stop_hz": 135_000, "step_hz": 10_000, "level_unit": "s_meter_count",
         "lower_is_stronger": true, "started_at": first, "ended_at": end, "coverage_hex": "15",
         "bins": [["start_index": 0,"end_index": 2,"measured_points": 2,"frequency_hz": 120_000,
                   "level": 9,"observation_id": "b","started_at": first,"ended_at": end],
                  ["start_index": 3,"end_index": 4,"measured_points": 1,"frequency_hz": 135_000,
                   "level": 6,"observation_id": "a","started_at": first,"ended_at": end]]]
    }
    private func original() -> [String: Any] {
        ["id": "a", "session_id":"session one","stream_id":"r","interval":["started_at":first,"ended_at":end],
         "frequency_hz":135_000,"mode":"FM","level":6,"level_unit":"s_meter_count",
         "metadata":["scan_pass":2,"frequency_readback":true,"frequency_source":"receiver_RX","raw_receiver_status":"RX fixture"]]
    }

    @Test func fullGridUsesOneCyclicStreamAndLegacySegmentsStayBounded() async throws {
        let grid = try PlatformARDV1ScopeConfiguration(startHz: 100_000, stopHz: 1_300_000_000, stepHz: 10_000, cyclic: true)
        #expect(grid.pointCount == 129_991)
        #expect(try PlatformARDV1ScopeConfiguration(startHz: 100_000_000, stopHz: 400_000_000, stepHz: 10_000, cyclic: true).pointCount == 30_001)
        #expect(throws: PlatformError.self) { try PlatformARDV1ScopeConfiguration(startHz: grid.startHz, stopHz: grid.stopHz, stepHz: 10_000) }
        let path = "/dev/cu.AR-DV1"
        let api = try peer([["features":["ardv1_cyclic_panorama":true]],
            ["id":"s","name":"AOR","state":"draft","streams":[["id":"r","device":"AR-DV1","port":path,"role":"receiver","state":"idle",
             "level_unit":"s_meter_count","capabilities":["receiver","ardv1_cyclic_panorama"],"settings":["fast_scan":["cyclic":true]]]]]])
        let session = try await api.createARDV1Scope(port: path, configuration: grid, name: "Дежурство")
        #expect(session.streams.count == 1 && session.streams[0].isCyclicARDV1)
        #expect(Core.records.map { $0.url?.path } == ["/api/v1/capabilities", "/api/v1/sessions"])
        let request = try #require(Core.records.last?.httpBody)
        let draft = try JSONDecoder().decode(MonitoringValue.self, from: request)
        let settings = try #require(draft["streams"].items.first)["settings"]
        #expect(settings["fast_scan"]["cyclic"].bool == true && settings["fast_scan"]["strategy"].text == "auto")
        #expect(settings["fast_scan"]["settle_s"].double == 0.015 && settings["mode"].text == "FM")
        #expect(settings["adapter"].text == "ardv1-serial" && draft["streams"].items.count == 1)
    }

    @Test func holesAndShortFinalStepKeepExactOriginalAndRevisitEvidence() async throws {
        var previous = partial()
        previous["pass"] = 1
        previous["started_at"] = "2025-12-31T23:59:00Z"
        let api = try peer([partial(), previous, original(), original()])
        let current = try await api.receiverPanorama(sessionID: "session one", streamID: "r", columns: 2)
        let prior = try await api.receiverPanorama(sessionID: "session one", streamID: "r", pass: 1, columns: 2)
        #expect(current.measuredRanges() == [0...0,2...2,4...4])
        #expect(current.revisitSeconds(after: prior)?.seconds == 60)
        #expect(current.revisitSeconds(after: prior)?.estimated == false)
        #expect(current.revisitSeconds(after: nil)?.estimated == true)
        let row = try await api.receiverOriginal(sessionID: "session one", streamID: "r", pass: 2, frequencyHz: 135_000, observationID: "a")
        #expect(row["metadata"]["raw_receiver_status"].text == "RX fixture")
        #expect(Core.records.last?.url?.path == "/api/v1/sessions/session one/streams/r/receiver-panorama/point")
        _ = try await api.receiverOriginal(sessionID: "session one", streamID: "r", pass: 2, frequencyHz: 135_000, observationID: "a", fromEvent: true)
        #expect(Core.records.last?.url?.path == "/api/v1/sessions/session one/observations/a")
    }

    @Test func foreignOrIncompatibleEvidenceCannotOpen() async throws {
        for change: [String: Any] in [["coverage_hex":"13"],["complete":true],["lower_is_stronger":false],
                                      ["level_unit":"dBm"],["pass":1],["total_points":6]] {
            let api = try peer([partial().merging(change) { _, new in new }])
            await #expect(throws: PlatformError.self) { try await api.receiverPanorama(sessionID: "s", streamID: "r", pass: 2, columns: 2) }
        }
        for change: [String: Any] in [["id":"other"],["session_id":"other"],["level_unit":"dBm"],
                                       ["metadata":["scan_pass":2, "frequency_source":"last_command", "frequency_readback":false]]] {
            let api = try peer([original().merging(change) { _, new in new }])
            await #expect(throws: PlatformError.self) { try await api.receiverOriginal(sessionID: "session one", streamID: "r", pass: 2, frequencyHz: 135_000, observationID: "a") }
        }
    }

    @Test func notificationReadbackKeepsOnlyPublicConfiguration() async throws {
        let settings: [String: Any] = ["autostart":false,"archive":["minimum_free_bytes":1_048_576,"minimum_retention_seconds":60],
            "webhook":["enabled":false,"url":"","secret_configured":false],
            "notifications":["telegram":["enabled":true,"configured":true,"bot_token":"private-token","chat_id":"private-chat"]]]
        let api = try peer([settings,["enabled":true,"status":"waiting"],["notification_id":"n","state":"pending"]])
        let readback = try await api.monitoringSettings(projectID: "p")
        #expect(readback["notifications"]["telegram"] == .object(["enabled":.boolean(true),"configured":.boolean(true)]))
        #expect(!readback.pretty.contains("private-"))
        _ = try await api.monitoringDuty(projectID: "p", body: ["schedule":.object(["mode":.string("continuous")]),"resume_after_restart":.boolean(false)])
        _ = try await api.testDutyNotification(projectID: "p", channel: "telegram")
        #expect(Core.records.map { $0.url?.path } == ["/api/v1/monitoring-projects/p/settings","/api/v1/monitoring-projects/p/duty","/api/v1/monitoring-projects/p/notifications/test"])
    }
}
