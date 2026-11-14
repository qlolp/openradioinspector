import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class PhonogramModelTests: XCTestCase {
    @MainActor final class Deferred {
        var calls: [(String, String, String)] = []
        var continuations: [CheckedContinuation<PlatformPhonogramStatus, Error>] = []
        func call(_ action: String, _ session: String, _ stream: String) async throws -> PlatformPhonogramStatus {
            calls.append((action, session, stream))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformPhonogramStatus) { continuations.removeFirst().resume(returning: value) }
        func fail(_ error: Error) { continuations.removeFirst().resume(throwing: error) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "phonogram_error", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func statusObject(recording: Bool = true, session: String = "s", stream: String = "tiny") -> [String: Any] {
        ["session_id": session, "stream_id": stream, "sample_rate_hz": 48000, "recording": recording,
         "pre_history_seconds": 5.0, "buffered_seconds": 2.5,
         "recorded_seconds": recording ? 1.25 : 0.0,
         "started_at": recording ? "2026-11-08T12:00:00Z" : NSNull(),
         "last": NSNull()]
    }
    func lastObject() -> [String: Any] {
        ["sample_rate_hz": 48000, "pre_history_samples": 240000, "recorded_samples": 48000,
         "pre_history_seconds": 5.0, "recorded_seconds": 1.0, "duration_seconds": 6.0,
         "started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:06Z"]
    }
    @MainActor private var scope: PhonogramModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for phonogram state")
    }

    @MainActor func testReceiverOnlySessionCanSelectItsRecorder() throws {
        let receiver: [String: Any] = ["id": "rx", "device": "AR-DV1", "role": "receiver",
                                       "level_unit": "s_meter_count", "state": "disconnected", "capabilities": []]
        let session = try decode(["id": "s", "name": "Receiver", "state": "draft", "streams": [receiver]], as: PlatformSession.self)
        let remote = RemoteDualModel()
        remote.session = session; remote.selectedSessionID = "s"
        remote.configureAuxiliaryStreams(client: try api(), session: session)
        XCTAssertEqual(remote.auxiliaryStreamID, "rx")
        XCTAssertTrue(remote.phonogram.canCall)
        remote.auxiliaryStreamID = "unknown"
        XCTAssertFalse(remote.phonogram.canCall)
        remote.configureAuxiliaryStreams(client: nil, session: nil)
        XCTAssertFalse(remote.phonogram.canCall)
    }

    @MainActor func testStartEchoesScopeAndKeepsStatus() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = PhonogramModel(
            timeoutNanoseconds: 60_000_000_000,
            call: { _, action, session, stream in try await deferred.call(action, session, stream) })
        model.configure(client: client, scope: scope)
        model.perform("start")
        XCTAssertTrue(model.pending)
        model.perform("stop")
        try await waitFor { deferred.continuations.count >= 1 }
        XCTAssertEqual(deferred.calls.count, 1, "single flight")
        XCTAssertEqual(deferred.calls[0].0, "start")
        XCTAssertEqual(deferred.calls[0].1, "s")
        XCTAssertEqual(deferred.calls[0].2, "tiny")
        deferred.release(try decode(statusObject(), as: PlatformPhonogramStatus.self))
        try await waitFor { model.status != nil }
        XCTAssertEqual(model.status?.recording, true)
        XCTAssertNotNil(model.infoText)
        model.reset()
        XCTAssertNil(model.status)
        XCTAssertNil(model.infoText)
        XCTAssertEqual(deferred.calls.map { $0.0 }, ["start"], "Reset must not stop the Core recorder")
    }

    @MainActor func testForgedStatusAndFailureNeverFreeze() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = PhonogramModel(
            timeoutNanoseconds: 60_000_000_000,
            call: { _, action, session, stream in try await deferred.call(action, session, stream) })
        model.configure(client: client, scope: scope)
        model.perform("status")
        try await waitFor { deferred.continuations.count >= 1 }
        let forged = try decode(statusObject(session: "other"), as: PlatformPhonogramStatus.self)
        XCTAssertEqual(forged.sessionId, "other")
        deferred.fail(PlatformError.http(status: 409, code: "phonogram_error", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertNil(model.status)
        XCTAssertNotNil(model.errorText)
    }

    @MainActor func testStatusDecodingGuards() throws {
        XCTAssertEqual(PlatformPhonogramStatus.preHistorySeconds, 5.0)
        let good = try decode(statusObject(), as: PlatformPhonogramStatus.self)
        XCTAssertEqual(good.preHistorySecondsField, 5.0)
        var wrongHistory = statusObject()
        wrongHistory["pre_history_seconds"] = 4.0
        XCTAssertThrowsError(try decode(wrongHistory, as: PlatformPhonogramStatus.self))
        var withLast = statusObject(recording: false)
        withLast["last"] = lastObject()
        let decoded = try decode(withLast, as: PlatformPhonogramStatus.self)
        XCTAssertEqual(decoded.last?.recordedSamples, 48000)
        var brokenSplit = lastObject()
        brokenSplit["duration_seconds"] = 99.0
        XCTAssertThrowsError(try decode(brokenSplit, as: PlatformPhonogram.self))
    }
}
