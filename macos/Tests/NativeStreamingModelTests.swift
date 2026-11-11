import Foundation
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
            let body = String(decoding: try! JSONSerialization.data(withJSONObject: payload), as: UTF8.self)
            client?.urlProtocol(self, didLoad: Data("id: \(sequence)\nevent: sweep.received\ndata: \(body)\n\n".utf8))
        }
    }
    final class Responses: @unchecked Sendable {
        let lock = NSLock()
        var stream: Stub?
        var reads = 0
        var mutations = 0
        static func sweep(id: String = "initial", levels: [Double] = [-100, -90, -80]) -> [String: Any] {
            ["id": id, "session_id": "s", "stream_id": "a", "interval": ["started_at": "2026-11-11T00:00:00Z", "ended_at": "2026-11-11T00:00:01Z"], "frequencies_hz": [100, 200, 300], "levels": levels, "level_unit": "dBm"]
        }
        var sweepReads: Int { lock.withLock { reads } }
        var eventConnection: Stub? { lock.withLock { stream } }
        func handle(_ stub: Stub) {
            lock.withLock {
                if stub.request.httpMethod != "GET" { mutations += 1 }
                let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [["id": "a", "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]]]]
                switch stub.request.url!.path {
                case "/api/v1/health": stub.json(["last_event_id": 0])
                case "/api/v1/events": stream = stub; stub.events()
                case "/api/v1/sessions": stub.json([session])
                case "/api/v1/sessions/s": stub.json(session)
                case "/api/v1/sessions/s/sweeps": reads += 1; stub.json([Self.sweep()])
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
    @MainActor private func connected(_ responses: Responses) async throws -> RemoteDualModel {
        Stub.handle = responses.handle
        let model = RemoteDualModel(makeClient: { url in
            let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: url, session: URLSession(configuration: configuration))
        })
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
    @MainActor func testSequenceGapRecoversSnapshotBeforeApplyingAnyLaterFrame() async throws {
        let responses = Responses(), model = try await connected(responses)
        defer { model.disconnect() }
        let reads = responses.sweepReads, connection = try XCTUnwrap(responses.eventConnection)
        connection.sweep(sequence: 3, id: "after-gap", levels: [-60, -50, -40])
        try await wait { responses.sweepReads > reads }
        XCTAssertEqual(model.current.map(\.y), [-100, -90, -80])
        XCTAssertEqual(responses.mutations, 0)
    }
}
