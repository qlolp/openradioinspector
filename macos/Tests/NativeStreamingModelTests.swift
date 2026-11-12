import Foundation
import Combine
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeStreamingModelTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handle: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handle?(self) }
        override func stopLoading() {}
        func json(_ value: Any, status: Int = 200) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": value]
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: envelope)); client?.urlProtocolDidFinishLoading(self)
        }
        func events() {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "text/event-stream"])!, cacheStoragePolicy: .notAllowed)
        }
        func sweep(sequence: Int, id: String, levels: [Double]) {
            var sample = Responses.sweep(id: id, levels: levels)
            sample["interval"] = ["started_at": String(format: "2026-11-11T00:00:00.%06dZ", sequence), "ended_at": "2026-11-11T00:00:01Z"]
            let payload: [String: Any] = ["sequence": sequence, "event_type": "sweep.received", "occurred_at": "2026-11-11T00:00:02Z", "session_id": "s", "payload": ["sweep": sample]]
            let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": payload]
            let body = String(decoding: try! JSONSerialization.data(withJSONObject: envelope), as: UTF8.self)
            client?.urlProtocol(self, didLoad: Data("id: \(sequence)\nevent: sweep.received\ndata: \(body)\n\n".utf8))
        }
    }
    final class Responses: @unchecked Sendable {
        let lock = NSLock()
        var stream: Stub?
        var reads = 0
        var healthReads = 0
        var eventRequests: [Int64] = []
        private var holdMetadata = false
        private var heldMetadata: (Stub, [String: Any])?
        private var restoredWatermark: Int64?
        private var snapshotID = "initial"
        private var snapshotLevels: [Double] = [-100, -90, -80]
        private var healthStatus = 200
        private var holdHealth = false
        private var heldHealth: (Stub, Int64, Int)?
        var mutations = 0
        static func sweep(id: String = "initial", levels: [Double] = [-100, -90, -80]) -> [String: Any] {
            ["id": id, "session_id": "s", "stream_id": "a", "interval": ["started_at": "2026-11-11T00:00:00Z", "ended_at": "2026-11-11T00:00:01Z"], "frequencies_hz": [100, 200, 300], "levels": levels, "level_unit": "dBm"]
        }
        var sweepReads: Int { lock.withLock { reads } }
        var eventConnection: Stub? { lock.withLock { stream } }
        var resumeCursors: [Int64] { lock.withLock { eventRequests } }
        var watermarkReads: Int { lock.withLock { healthReads } }
        var metadataIsHeld: Bool { lock.withLock { heldMetadata != nil } }
        var healthIsHeld: Bool { lock.withLock { heldHealth != nil } }
        func holdNextHealth() { lock.withLock { holdHealth = true } }
        func releaseHealth() {
            let held = lock.withLock { let held = heldHealth; heldHealth = nil; return held }
            if let held { held.0.json(["last_event_id": held.1], status: held.2) }
        }
        func holdNextMetadata() { lock.withLock { holdMetadata = true } }
        func restoreArchive(watermark: Int64, id: String = "restored", levels: [Double] = [-98.125, -87.5, -76.25]) {
            lock.withLock { restoredWatermark = watermark; snapshotID = id; snapshotLevels = levels }
        }
        func failHealth(_ status: Int) { lock.withLock { healthStatus = status } }
        func releaseMetadata() {
            let held = lock.withLock { let held = heldMetadata; heldMetadata = nil; return held }
            if let held { held.0.json(held.1) }
        }
        func handle(_ stub: Stub) {
            lock.withLock {
                if stub.request.httpMethod != "GET" { mutations += 1 }
                let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": "a", "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
                switch stub.request.url!.path {
                case "/api/v1/capabilities": stub.json(["features": [:]])
                case "/api/v1/health":
                    healthReads += 1
                    let value = restoredWatermark ?? (healthReads == 1 ? 0 : 10000)
                    if holdHealth { holdHealth = false; heldHealth = (stub, value, healthStatus) }
                    else { stub.json(["last_event_id": value], status: healthStatus) }
                case "/api/v1/events":
                    eventRequests.append(Int64(stub.request.value(forHTTPHeaderField: "Last-Event-ID") ?? "-1")!)
                    stream = stub; stub.events()
                case "/api/v1/sessions": stub.json([session])
                case "/api/v1/sessions/s":
                    if holdMetadata { holdMetadata = false; heldMetadata = (stub, session) }
                    else { stub.json(session) }
                case "/api/v1/sessions/s/sweeps": reads += 1; stub.json([Self.sweep(id: snapshotID, levels: snapshotLevels)])
                case "/api/v1/sessions/s/audio/status":
                    stub.json(["session_id": "s", "stream_id": "a", "subscribers": 1, "max_subscribers": 4, "queued_blocks": 2, "dropped_blocks": 0])
                case "/api/v1/sessions/s/phonogram/status":
                    stub.json(["session_id": "s", "stream_id": "a", "sample_rate_hz": 48000, "recording": true,
                               "pre_history_seconds": 5.0, "buffered_seconds": 2.5, "recorded_seconds": 1.25,
                               "started_at": "2026-11-11T00:00:00Z", "last": NSNull()])
                case "/api/v1/monitoring-projects/p/status":
                    stub.json(["active": true, "project": ["id": "p", "state": "armed", "coverage": "wide"]])
                case "/api/v1/monitoring-projects/p/history":
                    stub.json(["items": [NativeArchiveContextTests.Responses.row], "next_cursor": "old-page"])
                case "/api/v1/sessions/s/workers", "/api/v1/sessions/s/observations": stub.json([])
                default: stub.json([:], status: 404)
                }
            }
        }
    }
    override func tearDown() { Stub.handle = nil; super.tearDown() }
    @MainActor private func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Synthetic streaming state was not published")
    }
    @MainActor private func connected(_ responses: Responses, onArchiveContextReset: @escaping @MainActor (Int64?) -> Void = { _ in }) async throws -> RemoteDualModel {
        Stub.handle = responses.handle
        let model = RemoteDualModel(makeClient: { url in
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: url, session: URLSession(configuration: configuration))
        }, onArchiveContextReset: onArchiveContextReset)
        model.connect()
        try await wait { responses.eventConnection != nil && !model.current.isEmpty }
        return model
    }
    @MainActor func testCommittedFramesUpdateWithoutRefetchingSnapshotAndDisconnectNeverMutatesCore() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        let reads = responses.sweepReads, connection = try XCTUnwrap(responses.eventConnection)
        connection.sweep(sequence: 1, id: "initial", levels: [-100, -90, -80])
        connection.sweep(sequence: 2, id: "committed", levels: [-60, -50, -40])
        try await wait { model.current.map(\.y) == [-60, -50, -40] }
        XCTAssertEqual(responses.sweepReads, reads)
        XCTAssertEqual(model.spectrumPresentation.sweeps.map(\.id), ["initial", "committed"])
        model.disconnect()
        connection.sweep(sequence: 3, id: "late", levels: [-30, -20, -10])
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertTrue(model.current.isEmpty)
        XCTAssertEqual(responses.mutations, 0)
    }
    @MainActor func testAnalysisSourcesArePublishedAfterTheirDisplayFrameIsReady() async throws {
        let responses = Responses()
        Stub.handle = responses.handle
        let model = RemoteDualModel(makeClient: { url in
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: url, session: URLSession(configuration: configuration))
        })
        var observed = false
        let subscription = model.$comparisonSweeps.sink { sweeps in
            guard !sweeps.isEmpty else { return }
            observed = true
            XCTAssertEqual(model.current.map(\.y), [-100, -90, -80])
            XCTAssertEqual(model.waterfall.rows.count, 1)
        }
        defer { subscription.cancel(); model.disconnect() }
        model.connect(startPolling: false)
        try await wait { model.sweepAverage.target != nil }
        XCTAssertTrue(observed)
        let revision = model.waterfallRevision
        await model.refresh()
        XCTAssertEqual(model.waterfallRevision, revision, "An identical refresh must not add a waterfall frame")
    }
    @MainActor func testPauseFreezesOnlyDisplayWhileCommittedFramesContinueAndResumeUsesNewest() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        model.toggleSpectrumPause()
        let connection = try XCTUnwrap(responses.eventConnection)
        connection.sweep(sequence: 1, id: "while-paused", levels: [-60, -50, -40])
        try await wait { model.eventStatus.contains("принято событие 1") }
        XCTAssertEqual(model.current.map(\.y), [-100, -90, -80])
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "initial")
        XCTAssertEqual(responses.mutations, 0)
        model.toggleSpectrumPause()
        XCTAssertEqual(model.current.map(\.y), [-60, -50, -40])
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "while-paused")
    }
    @MainActor func testMetadataRefreshUsesFramesAppliedWhileItsRequestWasInFlight() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { responses.releaseMetadata(); model.disconnect() }
        responses.holdNextMetadata()
        let refresh = Task { await model.refresh() }
        try await wait { responses.metadataIsHeld }
        try XCTUnwrap(responses.eventConnection).sweep(sequence: 1, id: "during-metadata", levels: [-60, -50, -40])
        try await wait { model.current.map(\.y) == [-60, -50, -40] }
        responses.releaseMetadata()
        await refresh.value
        XCTAssertEqual(model.current.map(\.y), [-60, -50, -40])
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "during-metadata")
        XCTAssertEqual(responses.resumeCursors, [0])
        XCTAssertEqual(responses.mutations, 0)
    }
    @MainActor func testSequenceGapChecksCurrentHealthButReplaysLastAppliedCursor() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        let reads = responses.sweepReads, connection = try XCTUnwrap(responses.eventConnection)
        connection.sweep(sequence: 1, id: "first", levels: [-70, -60, -50])
        try await wait { model.current.map(\.y) == [-70, -60, -50] }
        let watermarks = responses.watermarkReads
        connection.sweep(sequence: 3, id: "after-gap", levels: [-60, -50, -40])
        try await wait { responses.resumeCursors.count == 2 }
        XCTAssertEqual(responses.resumeCursors, [0, 1])
        XCTAssertGreaterThan(responses.watermarkReads, watermarks)
        XCTAssertEqual(responses.sweepReads, reads)
        XCTAssertEqual(model.current.map(\.y), [-70, -60, -50])
        let resumed = try XCTUnwrap(responses.eventConnection)
        resumed.sweep(sequence: 2, id: "missing", levels: [-65, -55, -45])
        resumed.sweep(sequence: 3, id: "after-gap", levels: [-60, -50, -40])
        try await wait { model.current.map(\.y) == [-60, -50, -40] }
        XCTAssertEqual(model.spectrumPresentation.sweeps.map(\.id), ["initial", "first", "missing", "after-gap"])
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testSameIDArchiveRollbackLoadsFreshSpectrumAndClearsPinnedAndAudioState() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        let oldStream = try XCTUnwrap(responses.eventConnection)
        oldStream.sweep(sequence: 1, id: "old-committed", levels: [-60, -50, -40])
        try await wait { model.comparisonSweeps.last?.id == "old-committed" }
        model.comparisonSelectionID = "old-committed"; model.pinComparisonReference()
        model.phonogram.perform("status"); model.audioStream.perform("status")
        try await wait { model.phonogram.status != nil && model.audioStream.status != nil }
        let generation = model.generation
        responses.restoreArchive(watermark: 0)
        await model.refresh()
        XCTAssertGreaterThan(model.generation, generation)
        XCTAssertNil(model.comparisonReference)
        XCTAssertNil(model.sweepComparison)
        XCTAssertNil(model.phonogram.status)
        XCTAssertNil(model.phonogram.infoText)
        XCTAssertNil(model.audioStream.status)
        XCTAssertNil(model.audioStream.sample)
        XCTAssertNil(model.audioStream.infoText)
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["restored"])
        XCTAssertEqual(model.current.map(\.y), [-98.125, -87.5, -76.25])
        XCTAssertEqual(model.spectrumPresentation.sweeps.map(\.id), ["restored"])
        XCTAssertEqual(model.selectedSessionID, "s")
        XCTAssertEqual(model.analyzer?.id, "a")
        try await wait { responses.resumeCursors.count == 2 }
        XCTAssertEqual(responses.resumeCursors, [0, 0])
        oldStream.sweep(sequence: 2, id: "late-old-archive", levels: [-1, -2, -3])
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertEqual(model.current.map(\.y), [-98.125, -87.5, -76.25])
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testRollbackWhileMetadataIsHeldRejectsOldRefreshCompletion() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { responses.releaseMetadata(); model.disconnect() }
        try XCTUnwrap(responses.eventConnection).sweep(sequence: 1, id: "old", levels: [-60, -50, -40])
        try await wait { model.comparisonSweeps.last?.id == "old" }
        responses.holdNextMetadata()
        let oldRefresh = Task { await model.refresh() }
        try await wait { responses.metadataIsHeld }
        responses.restoreArchive(watermark: 0)
        await model.refresh()
        responses.releaseMetadata(); await oldRefresh.value
        XCTAssertEqual(model.current.map(\.y), [-98.125, -87.5, -76.25])
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["restored"])
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testFrameAppliedWhileHealthIsHeldDoesNotCauseFalseRollback() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { responses.releaseHealth(); model.disconnect() }
        responses.restoreArchive(watermark: 0, id: "initial", levels: [-100, -90, -80])
        responses.holdNextHealth()
        let generation = model.generation
        let refresh = Task { await model.refresh() }
        try await wait { responses.healthIsHeld }
        try XCTUnwrap(responses.eventConnection).sweep(sequence: 1, id: "during-health", levels: [-60, -50, -40])
        try await wait { model.comparisonSweeps.last?.id == "during-health" }
        responses.restoreArchive(watermark: 1, id: "initial", levels: [-100, -90, -80])
        responses.releaseHealth(); await refresh.value
        XCTAssertEqual(model.generation, generation)
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["initial", "during-health"])
        XCTAssertEqual(responses.resumeCursors, [0])
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testLiveRollbackSynchronouslyInvalidatesWorkspaceBeforeItsNextPoll() async throws {
        let responses = Responses(), workspace = MonitoringWorkspaceModel()
        let model = try await connected(responses, onArchiveContextReset: { [weak workspace] watermark in
            workspace?.resetArchiveContext(watermark: watermark)
        })
        defer { model.disconnect(); workspace.stopAudio(); workspace.stopOperations() }
        workspace.configure(model.serverURL, client: model.client); workspace.selected = "p"
        await workspace.refreshStatus()
        await workspace.loadHistory(start: "", end: "", minHz: "", maxHz: "", eventID: nil)
        XCTAssertFalse(workspace.history.isEmpty)
        try XCTUnwrap(responses.eventConnection).sweep(sequence: 1, id: "old", levels: [-60, -50, -40])
        try await wait { model.comparisonSweeps.last?.id == "old" }
        responses.restoreArchive(watermark: 0)
        await model.refresh() // Do not call workspace.refreshStatus after the rollback.
        XCTAssertTrue(workspace.history.isEmpty)
        XCTAssertEqual(workspace.selectedSweepID, "")
        XCTAssertEqual(workspace.status, .null)
        XCTAssertEqual(workspace.selected, "p")
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["restored"])
        XCTAssertEqual(responses.mutations, 0)
    }

    @MainActor func testUnavailableHealthSuspendsLateFramesAndHealthyResumeKeepsAppliedCursor() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        let oldStream = try XCTUnwrap(responses.eventConnection)
        oldStream.sweep(sequence: 1, id: "applied", levels: [-70, -60, -50])
        try await wait { model.comparisonSweeps.last?.id == "applied" }
        responses.failHealth(503)
        let published = model.waterfallRevision
        await model.refresh()
        XCTAssertNotNil(model.errorText)
        XCTAssertTrue(model.current.isEmpty)
        XCTAssertGreaterThan(model.waterfallRevision, published)
        oldStream.sweep(sequence: 2, id: "unverified", levels: [-2, -3, -4])
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertTrue(model.current.isEmpty)
        model.toggleSpectrumPause(); model.toggleSpectrumPause()
        XCTAssertTrue(model.current.isEmpty, "Pause/resume cannot publish an unverified cache")
        responses.failHealth(200)
        await model.refresh()
        try await wait { responses.resumeCursors.count == 2 }
        XCTAssertEqual(responses.resumeCursors, [0, 1])
        XCTAssertEqual(model.comparisonSweeps.map(\.id), ["initial", "applied"])
        try XCTUnwrap(responses.eventConnection).sweep(sequence: 2, id: "replayed", levels: [-65.25, -55.125, -45.5])
        try await wait { model.comparisonSweeps.last?.id == "replayed" }
        XCTAssertEqual(model.current.map(\.y), [-65.25, -55.125, -45.5])
        XCTAssertEqual(responses.mutations, 0)
    }
}
