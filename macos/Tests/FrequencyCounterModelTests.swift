import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class FrequencyCounterModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, PlatformFrequencyCounterRequest)] = []
        var continuations: [CheckedContinuation<PlatformFrequencyCounterResult, Error>] = []
        func measure(_ session: String, _ request: PlatformFrequencyCounterRequest) async throws -> PlatformFrequencyCounterResult {
            requests.append((session, request))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformFrequencyCounterResult) { continuations.removeFirst().resume(returning: value) }
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
                : ["error": ["code": "counter_incompatible", "message": "synthetic"]]
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
    private func sweep(id: String = "sweep-0") throws -> PlatformSweep {
        try decode(["id": id, "session_id": "s", "stream_id": "tiny",
                    "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                    "settings": ["rbw_hz": 1000], "frequencies_hz": [100, 200, 300, 400],
                    "levels": [-90.0, -80.0, -50.0, -52.0], "level_unit": "dBm"],
                   as: PlatformSweep.self)
    }
    func resultObject(method: String = "parabolic") -> [String: Any] {
        ["session_id": "s", "stream_id": "tiny",
         "sweep": ["id": "sweep-0", "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                   "level_unit": "dBm", "settings": ["rbw_hz": 1000]],
         "window": ["start_hz": 100, "stop_hz": 400], "resolution_hz": 10, "raster_hz": 100,
         "method": method, "peak_index": 2, "peak_frequency_hz": 300, "peak_level_dbm": -50.0,
         "measured_frequency_hz": 343.75, "uncertainty_hz": 50.0,
         "measurement_methodology": "frequency_counter",
         "values": ["measured_frequency_hz": 343.75, "resolution_hz": 10.0, "uncertainty_hz": 50.0]]
    }
    @MainActor private var scope: FrequencyCounterModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for counter state")
    }
    @MainActor private func model(_ deferred: Deferred) throws -> FrequencyCounterModel {
        let model = FrequencyCounterModel(
            timeoutNanoseconds: 60_000_000_000,
            measure: { _, session, request in try await deferred.measure(session, request) })
        model.configure(client: try api(), scope: scope, sweeps: [try sweep()])
        return model
    }

    @MainActor func testOnlyDbmSweepsOfTheScopeAreOffered() throws {
        let foreign = try decode(["id": "foreign", "session_id": "other", "stream_id": "tiny",
                                  "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                                  "settings": ["rbw_hz": 1], "frequencies_hz": [100], "levels": [-50.0],
                                  "level_unit": "dBm"], as: PlatformSweep.self)
        let notDbm = try decode(["id": "rx", "session_id": "s", "stream_id": "tiny",
                                 "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                                 "settings": ["rbw_hz": 1], "frequencies_hz": [100], "levels": [5.0],
                                 "level_unit": "s_meter_count"], as: PlatformSweep.self)
        let model = FrequencyCounterModel()
        model.configure(client: try api(), scope: scope, sweeps: [try sweep(), foreign, notDbm])
        XCTAssertEqual(model.sweeps.map(\.id), ["sweep-0"])
        XCTAssertEqual(model.draft.sweepID, "sweep-0")
        XCTAssertTrue(model.canMeasure)
    }

    @MainActor func testBadWindowsAreRefusedBeforeAnyRequest() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        for (resolution, start, stop) in [("0", "100", "400"), ("abc", "100", "400"),
                                          ("10", "400", "400"), ("10", "500", "400"), ("10", "100", "")] {
            model.draft.resolutionText = resolution
            model.draft.startText = start
            model.draft.stopText = stop
            model.analyze()
            XCTAssertFalse(model.pending, "\(resolution) \(start) \(stop)")
            XCTAssertNotNil(model.errorText)
        }
        XCTAssertTrue(deferred.requests.isEmpty)
    }

    @MainActor func testMeasureEchoesRequestAndKeepsResult() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = FrequencyCounterModel(
            timeoutNanoseconds: 60_000_000_000,
            measure: { _, session, request in try await deferred.measure(session, request) })
        model.configure(client: client, scope: scope, sweeps: [try sweep()])
        model.analyze()
        XCTAssertTrue(model.pending)
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        XCTAssertEqual(deferred.requests.count, 1, "single flight")
        XCTAssertEqual(deferred.requests[0].1.sweepId, "sweep-0")
        XCTAssertEqual(deferred.requests[0].1.resolutionHz, 10)
        deferred.release(try decode(resultObject(), as: PlatformFrequencyCounterResult.self))
        try await waitFor { model.result != nil }
        XCTAssertFalse(model.pending)
        XCTAssertEqual(model.result?.method, "parabolic")
        XCTAssertNotNil(model.infoText)
        // Polling a new sweep list on the same client never re-measures automatically.
        model.configure(client: client, scope: scope, sweeps: [try sweep(), try sweep(id: "sweep-1")])
        XCTAssertNotNil(model.result)
    }

    @MainActor func testForgedResultAndFailureNeverFreeze() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        let forged = try decode(resultObject(), as: PlatformFrequencyCounterResult.self)
        // A different session in the echo is refused by the client contract.
        XCTAssertEqual(forged.sessionId, "s")
        deferred.fail(PlatformError.http(status: 409, code: "counter_incompatible", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertNil(model.result)
        XCTAssertTrue(model.errorText != nil)
    }
}
