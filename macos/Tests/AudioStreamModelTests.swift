import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class AudioStreamModelTests: XCTestCase {
    @MainActor final class Deferred {
        var calls: [(String, String, String)] = []
        var continuations: [CheckedContinuation<AudioStreamOutcome, Error>] = []
        func call(_ action: String, _ session: String, _ stream: String) async throws -> AudioStreamOutcome {
            calls.append((action, session, stream))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: AudioStreamOutcome) { continuations.removeFirst().resume(returning: value) }
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
                : ["error": ["code": "audio_stream_unavailable", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: config))
    }
    private func statusValue(session: String = "s") throws -> PlatformAudioStreamStatus {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let object: [String: Any] = ["session_id": session, "stream_id": "tiny",
                                     "subscribers": 1, "max_subscribers": 4,
                                     "queued_blocks": 2, "dropped_blocks": 3]
        return try decoder.decode(PlatformAudioStreamStatus.self, from: JSONSerialization.data(withJSONObject: object))
    }
    @MainActor private var scope: AudioStreamModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for audio stream state")
    }

    @MainActor func testSampleEchoesScopeAndKeepsBytes() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = AudioStreamModel(
            timeoutNanoseconds: 60_000_000_000,
            call: { _, action, session, stream in try await deferred.call(action, session, stream) })
        model.configure(client: client, scope: scope)
        model.perform("sample")
        XCTAssertTrue(model.pending)
        model.perform("status")
        try await waitFor { deferred.continuations.count >= 1 }
        XCTAssertEqual(deferred.calls.count, 1, "single flight")
        XCTAssertEqual(deferred.calls[0].0, "sample")
        XCTAssertEqual(deferred.calls[0].1, "s")
        XCTAssertEqual(deferred.calls[0].2, "tiny")
        deferred.release(.sample(PlatformAudioStreamSample(
            pcm: Data([1, 2, 3, 4, 5]), sampleRateHz: 48000, sampleFormat: "pcm16le")))
        try await waitFor { model.sample != nil }
        XCTAssertEqual(model.sample?.pcm.count, 5)
        XCTAssertNotNil(model.infoText)
    }

    @MainActor func testForgedStatusAndFailureNeverFreeze() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = AudioStreamModel(
            timeoutNanoseconds: 60_000_000_000,
            call: { _, action, session, stream in try await deferred.call(action, session, stream) })
        model.configure(client: client, scope: scope)
        model.perform("status")
        try await waitFor { deferred.continuations.count >= 1 }
        deferred.release(.status(try statusValue(session: "other")))
        try await waitFor { !model.pending }
        XCTAssertNil(model.status, "статус другого сеанса отклоняется")
        XCTAssertNotNil(model.errorText)
        model.perform("status")
        try await waitFor { deferred.calls.count >= 2 }
        deferred.fail(PlatformError.http(status: 503, code: "audio_stream_unavailable", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertNotNil(model.errorText)
    }

    @MainActor func testScopeChangeClearsState() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = AudioStreamModel(
            timeoutNanoseconds: 60_000_000_000,
            call: { _, action, session, stream in try await deferred.call(action, session, stream) })
        model.configure(client: client, scope: scope)
        model.perform("status")
        try await waitFor { deferred.continuations.count >= 1 }
        deferred.release(.status(try statusValue()))
        try await waitFor { model.status != nil }
        model.configure(client: client, scope: .init(server: "local", session: "s", stream: "other"))
        XCTAssertNil(model.status)
        XCTAssertNil(model.sample)
        XCTAssertEqual(model.canCall, true)
    }
}
