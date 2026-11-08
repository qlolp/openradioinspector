import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SpectrumPresentationModelTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200 ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object] : ["error": ["code": "not_found", "message": "synthetic unavailable"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    final class Responses {
        let lock = NSLock()
        var sweeps: [[String: Any]]
        var mutations = 0
        var reads = 0
        var fail = false
        var holdNext = false
        var held: [(Stub, [[String: Any]])] = []
        var heldCount: Int { lock.withLock { held.count } }
        func hold() { lock.withLock { holdNext = true } }
        func release(status: Int) {
            let item = lock.withLock { held.removeFirst() }
            item.0.respond(item.1, status: status)
        }
        init(_ sweeps: [[String: Any]]) { self.sweeps = sweeps }
        func change(_ value: [[String: Any]], fail: Bool = false) { lock.withLock { sweeps = value; self.fail = fail } }
        func handle(_ stub: Stub) {
            lock.withLock {
                if stub.request.httpMethod != "GET" { mutations += 1 }; reads += 1
                let session: [String: Any] = ["id": "session-A", "name": "Synthetic", "state": "running", "streams": [
                    ["id": "stream-A", "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]],
                    ["id": "receiver", "device": "AR-DV1", "role": "receiver", "level_unit": "s_meter_count", "state": "connected", "capabilities": ["tune"]]]]
                switch stub.request.url!.path {
                case "/api/v1/sessions": stub.respond([session])
                case "/api/v1/sessions/session-A": stub.respond(session, status: fail ? 503 : 200)
                case "/api/v1/sessions/session-A/sweeps":
                    if holdNext { holdNext = false; held.append((stub, sweeps)) } else { stub.respond(sweeps) }
                case "/api/v1/sessions/session-A/workers", "/api/v1/sessions/session-A/observations": stub.respond([])
                default: stub.respond([:], status: 404)
                }
            }
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func sources() throws -> [[String: Any]] {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let fixture = try JSONSerialization.jsonObject(with: Data(contentsOf: root.appendingPathComponent("tests/fixtures/spectrum-presentation-v1.json"))) as! [String: Any]
        return (fixture["frame"] as! [String: Any])["sweeps"] as! [[String: Any]]
    }
    @MainActor private func model(_ responses: Responses) async throws -> RemoteDualModel {
        Stub.handler = responses.handle
        let model = RemoteDualModel(makeClient: { base in
            let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: base, session: URLSession(configuration: config))
        })
        model.connect(startPolling: false)
        for _ in 0..<400 {
            if !model.spectrumPresentation.sweeps.isEmpty { return model }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Synthetic snapshot not loaded"); return model
    }
    @MainActor func testRealPollingModelPauseFitResumeAndNoMutations() async throws {
        let source = try sources(); let responses = Responses(source); let model = try await model(responses)
        defer { model.disconnect() }
        let scale = model.spectrumPresentation.scale, current = model.current, rows = model.waterfall.rows
        model.spectrumLower = "-90"; model.spectrumUpper = "-70"
        model.toggleSpectrumPause()
        var newer = source[1]; newer["id"] = "newer"; newer["levels"] = [-20, -10, 0]
        responses.change([newer]); let reads = responses.reads
        await model.refresh()
        XCTAssertGreaterThan(responses.reads, reads)
        XCTAssertEqual(model.current, current); XCTAssertEqual(model.waterfall.rows, rows)
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "sweep-2")
        XCTAssertEqual(model.spectrumLower, "-90")
        model.applySpectrumBounds(); XCTAssertEqual(model.spectrumPresentation.clippedCount, 3)
        model.fitSpectrum(); XCTAssertEqual(model.spectrumPresentation.scale, scale)
        model.tuneLatestPeak(); model.createCandidateFromLatestPeak()
        try await Task.sleep(nanoseconds: 20_000_000)
        XCTAssertEqual(responses.mutations, 0)
        model.toggleSpectrumPause(); XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "newer")
        XCTAssertEqual(model.current.map(\.y), [-20, -10, 0]); XCTAssertEqual(model.spectrumPresentation.scale, scale)
        XCTAssertEqual(model.spectrumPresentation.clippedCount, 3)
    }
    @MainActor func testNativeGridForeignEmptyFailureAndScopeReset() async throws {
        let source = try sources(); let responses = Responses(source); let model = try await model(responses)
        defer { model.disconnect() }
        model.toggleSpectrumPause(); var grid = source[1]; grid["frequencies_hz"] = [1, 2, 9]
        responses.change([source[0], grid]); await model.refresh()
        XCTAssertFalse(model.spectrumPresentation.paused); XCTAssertEqual(model.waterfall.grid, [1, 2, 9]); XCTAssertEqual(model.waterfall.rows.count, 1)
        var foreign = source[1]; foreign["stream_id"] = "foreign"
        responses.change([source[0], foreign]); await model.refresh(); XCTAssertTrue(model.current.isEmpty)
        responses.change(source); await model.refresh(); model.toggleSpectrumPause()
        responses.change([], fail: true); await model.refresh()
        XCTAssertTrue(model.current.isEmpty); XCTAssertFalse(model.spectrumPresentation.paused); XCTAssertNil(model.spectrumPresentation.scale)
        responses.change(source); await model.refresh(); model.toggleSpectrumPause()
        model.serverURL += "/other"; XCTAssertTrue(model.current.isEmpty); XCTAssertFalse(model.spectrumPresentation.paused)
        model.disconnect(); XCTAssertTrue(model.waterfall.rows.isEmpty)
    }
    @MainActor func testLegacyRendererDefaultsAndExplicitScaleBitmap() throws {
        let chart = TraceChart(current: [], minimum: [], maximum: [], threshold: nil, yTitle: "raw")
        XCTAssertNil(chart.displayScale)
        let waterfall = WaterfallView(history: WaterfallHistory(), revision: 0, unitTitle: "raw")
        XCTAssertNil(waterfall.displayScale)
        let rows = [[-100.0, -50.0, 0.0]], legacy = WaterfallScale(low: -100, high: 0)
        let implicit = try XCTUnwrap(WaterfallBitmap.makeImage(rows: rows, scale: legacy))
        let explicit = try XCTUnwrap(WaterfallBitmap.makeImage(rows: rows, scale: legacy, displayScale: nil))
        XCTAssertEqual(implicit.dataProvider?.data as Data?, explicit.dataProvider?.data as Data?)
        let extreme = try XCTUnwrap(WaterfallBitmap.makeImage(rows: [[-Double.greatestFiniteMagnitude, 0, Double.greatestFiniteMagnitude]], scale: legacy,
            displayScale: PlatformSpectrumScale(lower: -Double.greatestFiniteMagnitude, upper: Double.greatestFiniteMagnitude)))
        XCTAssertEqual(extreme.width, 3)
    }
    @MainActor func testOlderRefreshSuccessAndFailureCannotReplaceNewerFrame() async throws {
        let source = try sources(); let responses = Responses(source); let model = try await model(responses)
        defer { model.disconnect() }
        for status in [200, 503] {
            responses.change(source); responses.hold()
            let older = Task { await model.refresh() }
            for _ in 0..<400 {
                if responses.heldCount == 1 { break }
                try await Task.sleep(nanoseconds: 5_000_000)
            }
            XCTAssertEqual(responses.heldCount, 1)
            var newest = source[1]; newest["id"] = "new-\(status)"; newest["levels"] = [-30, -20, -10]
            responses.change([newest]); await model.refresh()
            XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "new-\(status)")
            responses.release(status: status); await older.value
            XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "new-\(status)")
            XCTAssertEqual(model.current.map(\.y), [-30, -20, -10])
        }
    }

    @MainActor func testIdenticalPollingAndMetadataChangesDoNotInvalidateBitmap() async throws {
        let source = try sources(); let responses = Responses(source); let model = try await model(responses)
        defer { model.disconnect() }
        let revision = model.waterfallRevision, history = model.waterfall
        await model.refresh(); await model.refresh()
        XCTAssertEqual(model.waterfallRevision, revision)
        XCTAssertEqual(model.waterfall, history)

        var metadataOnly = source
        metadataOnly[1]["id"] = "metadata-new"
        metadataOnly[1]["interval"] = ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"]
        responses.change(metadataOnly); await model.refresh()
        XCTAssertEqual(model.waterfallRevision, revision)
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.id, "metadata-new")
        XCTAssertEqual(model.spectrumPresentation.sweeps.last?.interval.endedAt, "2026-11-08T01:00:01Z")
        model.toggleSpectrumPause(); model.toggleSpectrumPause()
        XCTAssertEqual(model.waterfallRevision, revision)

        metadataOnly[1]["levels"] = [-30, -20, -10]
        responses.change(metadataOnly); await model.refresh()
        XCTAssertEqual(model.waterfallRevision, revision + 1)
        XCTAssertEqual(model.waterfall.rows.last, [-30, -20, -10])
        await model.refresh()
        XCTAssertEqual(model.waterfallRevision, revision + 1)
    }

}
