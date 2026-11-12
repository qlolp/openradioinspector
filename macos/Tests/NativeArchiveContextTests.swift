import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeArchiveContextTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handle: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handle?(self) }
        override func stopLoading() {}
        func reply(_ value: Any) {
            let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": value]
            bytes(try! JSONSerialization.data(withJSONObject: envelope), type: "application/json")
        }
        func bytes(_ data: Data, type: String) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200,
                httpVersion: nil, headerFields: ["Content-Type": type])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
    }

    final class Responses: @unchecked Sendable {
        private let lock = NSLock()
        private var watermark: Int64 = 100
        private var hold: String?
        private var held: [(Stub, Any)] = []
        private var requests: [String] = []
        private var writes = 0
        var pendingCount: Int { lock.withLock { held.count } }
        var mutations: Int { lock.withLock { writes } }
        func restore(_ value: Int64) { lock.withLock { watermark = value } }
        func holdNext(_ resource: String) { lock.withLock { hold = resource } }
        func release() {
            let values = lock.withLock { let values = held; held = []; return values }
            for (stub, value) in values {
                if let data = value as? Data { stub.bytes(data, type: "audio/wav") }
                else { stub.reply(value) }
            }
        }
        static var row: [String: Any] {
            ["id": "sweep-old", "session_id": "s", "stream_id": "a", "level_unit": "dBm",
             "interval": ["started_at": "2026-11-11T00:00:00Z", "ended_at": "2026-11-11T00:00:01Z"],
             "frequencies_hz": [100, 200], "levels": [-98.125, -87.5], "display_only": true,
             "original_point_count": 2, "settings": ["rbw_hz": 1000]]
        }
        func handle(_ stub: Stub) {
            let path = stub.request.url!.path
            let value: Any = lock.withLock {
                requests.append(path)
                if stub.request.httpMethod != "GET" { writes += 1 }
                switch path {
                case "/api/v1/health": return ["last_event_id": watermark]
                case "/api/v1/monitoring-projects/p/status":
                    return ["active": true, "project": ["id": "p", "state": "armed", "coverage": "wide", "active_session_id": "s"]]
                case "/api/v1/monitoring-projects/p/history": return ["items": [Self.row], "next_cursor": "old-page"]
                case "/api/v1/monitoring-projects/p/history/sweep-old": return Self.row
                case "/api/v1/monitoring-projects/p/history/sweep-old/comparison":
                    return ["sweep_id": "sweep-old", "compatible": true, "display_only": true, "items": []]
                case "/api/v1/monitoring-projects/p/events": return ["items": [["id": "event-old"]]]
                case "/api/v1/monitoring-projects/p/events/event-old":
                    return ["id": "event-old", "analysis": ["audio": ["filename": "old-evidence.wav"]]]
                case "/api/v1/monitoring-projects/p/events/event-old/sources": return ["items": [["sweep_id": "sweep-old"]]]
                case "/api/v1/monitoring-projects/p/events/event-old/audio": return Data("RIFF-old-evidence".utf8)
                default: return [:]
                }
            }
            let retained = lock.withLock {
                guard let hold, path.hasSuffix(hold) else { return false }
                self.hold = nil; held.append((stub, value)); return true
            }
            if !retained {
                if let data = value as? Data { stub.bytes(data, type: "audio/wav") }
                else { stub.reply(value) }
            }
        }
    }

    override func tearDown() { Stub.handle = nil; super.tearDown() }
    @MainActor private func workspace(_ responses: Responses) async throws -> MonitoringWorkspaceModel {
        Stub.handle = responses.handle
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: configuration))
        let model = MonitoringWorkspaceModel()
        model.configure("https://example.test", client: client); model.selected = "p"
        await model.refreshStatus()
        return model
    }
    @MainActor private func seed(_ model: MonitoringWorkspaceModel) async {
        await model.loadHistory(start: "", end: "", minHz: "", maxHz: "", eventID: nil)
        await model.loadSelectedSweep(sweepID: "sweep-old", comparison: false)
        await model.loadSelectedSweep(sweepID: "sweep-old", comparison: true)
        await model.loadEvents(); await model.showEvent("event-old")
        await model.loadSources(eventID: "event-old", next: false)
    }
    @MainActor private func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Controlled archive request did not reach the fixture")
    }
    @MainActor private func assertCleared(_ model: MonitoringWorkspaceModel, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(model.selected, "p", file: file, line: line)
        XCTAssertTrue(model.history.isEmpty, file: file, line: line)
        XCTAssertEqual(model.historyNext, "", file: file, line: line)
        XCTAssertEqual(model.selectedSweepID, "", file: file, line: line)
        XCTAssertNil(model.rawSweep, file: file, line: line)
        XCTAssertEqual(model.rawDetail, .null, file: file, line: line)
        XCTAssertEqual(model.baselineComparison, .null, file: file, line: line)
        XCTAssertTrue(model.events.isEmpty, file: file, line: line)
        XCTAssertEqual(model.selectedEvent, "", file: file, line: line)
        XCTAssertEqual(model.event, .null, file: file, line: line)
        XCTAssertTrue(model.sources.isEmpty, file: file, line: line)
        XCTAssertFalse(model.hasPlayback, file: file, line: line)
        XCTAssertFalse(model.audioLoading, file: file, line: line)
        XCTAssertEqual(model.audioNotice, "", file: file, line: line)
        XCTAssertFalse(model.baselineConfirmed, file: file, line: line)
    }

    @MainActor func testSameProjectArchiveRollbackClearsAllOldEvidenceWithoutDisarmingCore() async throws {
        let responses = Responses(), model = try await workspace(responses)
        defer { responses.release(); model.stopAudio(); model.stopOperations() }
        await seed(model)
        XCTAssertEqual(model.history.map(\.id), ["sweep-old"])
        XCTAssertNotNil(model.rawSweep)
        XCTAssertNotEqual(model.baselineComparison, .null)
        XCTAssertTrue(model.canPlay)
        model.audioNotice = "old-evidence.wav"
        model.baselineConfirmed = true
        responses.restore(7)
        await model.refreshStatus()
        assertCleared(model)
        XCTAssertTrue(model.isArmed, "UI invalidation must query, never disarm Core")
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testRollbackRejectsLateHistoryRawComparisonAndWAVCompletions() async throws {
        for resource in ["/history", "/history/sweep-old", "/history/sweep-old/comparison", "/audio"] {
            let responses = Responses(), model = try await workspace(responses)
            defer { responses.release(); model.stopAudio(); model.stopOperations() }
            await seed(model)
            responses.holdNext(resource)
            let request = Task {
                switch resource {
                case "/history": await model.loadHistory(start: "", end: "", minHz: "", maxHz: "", eventID: nil)
                case "/audio": await model.playAudio()
                default: await model.loadSelectedSweep(sweepID: "sweep-old", comparison: resource.hasSuffix("comparison"))
                }
            }
            try await wait { responses.pendingCount == 1 }
            responses.restore(7); await model.refreshStatus()
            assertCleared(model)
            responses.release(); await request.value
            assertCleared(model)
            XCTAssertTrue(model.error.isEmpty, "An old WAV must be discarded before decoding: \(resource)")
            XCTAssertTrue(model.researchError.isEmpty, resource)
            XCTAssertEqual(responses.mutations, 0)
        }
    }

    @MainActor func testNondecreasingArchiveWatermarkKeepsExplicitHistoryAndSelection() async throws {
        let responses = Responses(), model = try await workspace(responses)
        defer { model.stopAudio(); model.stopOperations() }
        await seed(model)
        model.baselineConfirmed = true
        responses.restore(101); await model.refreshStatus()
        XCTAssertEqual(model.history.map(\.id), ["sweep-old"])
        XCTAssertEqual(model.rawSweep?.levels, [-98.125, -87.5])
        XCTAssertEqual(model.selectedSweepID, "sweep-old")
        XCTAssertEqual(model.selectedEvent, "event-old")
        XCTAssertNotEqual(model.baselineComparison, .null)
        XCTAssertTrue(model.baselineConfirmed, "Ordinary refresh does not invalidate a current operator confirmation")
        model.selected = "another-project"
        XCTAssertFalse(model.baselineConfirmed)
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testOlderHeldHealthCannotRollBackANewerSuccessfulStatusRefresh() async throws {
        let responses = Responses(), model = try await workspace(responses)
        defer { responses.release(); model.stopAudio(); model.stopOperations() }
        await seed(model)
        responses.holdNext("/health")
        let oldRefresh = Task { await model.refreshStatus() }
        try await wait { responses.pendingCount == 1 }
        responses.restore(101); await model.refreshStatus()
        responses.release(); await oldRefresh.value
        responses.restore(102); await model.refreshStatus()
        XCTAssertEqual(model.history.map(\.id), ["sweep-old"])
        XCTAssertEqual(model.selectedEvent, "event-old")
        XCTAssertNotEqual(model.baselineComparison, .null)
        XCTAssertEqual(responses.mutations, 0)
    }
}
