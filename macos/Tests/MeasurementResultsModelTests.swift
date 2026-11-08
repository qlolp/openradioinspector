import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class MeasurementResultsModelTests: XCTestCase {
    @MainActor final class Deferred {
        var saves: [(String, PlatformMeasurementResultDraft)] = []
        var continuations: [CheckedContinuation<PlatformMeasurementResult, Error>] = []
        var lists: [String] = []
        var listContinuations: [CheckedContinuation<[PlatformMeasurementResult], Error>] = []
        func save(_ session: String, _ draft: PlatformMeasurementResultDraft) async throws -> PlatformMeasurementResult {
            saves.append((session, draft))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func list(_ session: String) async throws -> [PlatformMeasurementResult] {
            lists.append(session)
            return try await withCheckedThrowingContinuation { listContinuations.append($0) }
        }
        func release(_ value: PlatformMeasurementResult) { continuations.removeFirst().resume(returning: value) }
        func releaseList(_ value: [PlatformMeasurementResult]) { listContinuations.removeFirst().resume(returning: value) }
        func fail(_ error: Error) { continuations.removeFirst().resume(throwing: error) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200 || status == 201
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "invalid_request", "message": "synthetic"]]
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
    private func entry(_ id: String = "beta2_bandwidth") -> PlatformMethodologyEntry {
        try! decode(["methodology": id, "title": "Полоса", "original_table": "MEAS_B2_RESULT",
                     "unit": "Hz", "fields": ["bandwidth_hz", "lower_edge_hz", "upper_edge_hz", "center_hz"],
                     "result_codes": ["norm", "not_norm", "not_measured"]], as: PlatformMethodologyEntry.self)
    }
    private func record(id: String = "result-1", code: String = "norm") throws -> PlatformMeasurementResult {
        let values: [String: Any] = code == "not_measured" ? [:]
            : ["bandwidth_hz": 1.0, "lower_edge_hz": 2.0, "upper_edge_hz": 3.0, "center_hz": 4.0]
        return try decode([
            "id": id, "session_id": "s", "stream_id": "tiny",
            "methodology": "beta2_bandwidth", "result_code": code, "frequency_hz": 101_800_000,
            "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
            "values": values, "created_at": "2026-11-08T12:00:02Z",
        ], as: PlatformMeasurementResult.self)
    }
    @MainActor private var scope: MeasurementResultsModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for measurement state")
    }
    /// The model starts its request in a detached task: wait until save is registered.
    @MainActor private func waitForSave(_ deferred: Deferred) async throws {
        try await waitFor { deferred.continuations.count >= 1 }
    }
    @MainActor private func model(_ deferred: Deferred) throws -> MeasurementResultsModel {
        let model = MeasurementResultsModel(
            timeoutNanoseconds: 60_000_000_000,
            save: { _, session, draft in try await deferred.save(session, draft) },
            list: { _, session in try await deferred.list(session) },
            loadCatalog: { _ in [self.entry()] })
        model.configure(client: try api(), scope: scope)
        return model
    }

    @MainActor func testCatalogLoadsAndValuesMustMatchMethodology() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        try await waitFor { !model.catalog.isEmpty }
        XCTAssertEqual(model.draft.methodology, "beta2_bandwidth")
        model.draft.values = "{\"bandwidth_hz\": 1}"
        XCTAssertNil(model.chosenValues, "missing methodology fields are refused")
        model.draft.values = "{\"bandwidth_hz\": 1, \"lower_edge_hz\": 2, \"upper_edge_hz\": 3, \"center_hz\": 4}"
        XCTAssertNotNil(model.chosenValues)
        model.draft.values = "{\"bandwidth_hz\": true, \"lower_edge_hz\": 2, \"upper_edge_hz\": 3, \"center_hz\": 4}"
        XCTAssertNil(model.chosenValues, "JSON booleans must never become measurement values")
        model.draft.values = "{bad"
        XCTAssertNil(model.chosenValues, "malformed JSON is refused")
        model.draft.resultCode = "not_measured"
        model.draft.values = "{\"bandwidth_hz\": 1}"
        XCTAssertNil(model.chosenValues, "not_measured never carries values")
        model.draft.values = "{}"
        XCTAssertEqual(model.chosenValues, [:])
    }

    @MainActor func testFractionalSecondIntervalsReachTheCoreWithoutPrecisionLoss() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        try await waitFor { !model.catalog.isEmpty }
        model.draft.values = "{\"bandwidth_hz\": 1, \"lower_edge_hz\": 2, \"upper_edge_hz\": 3, \"center_hz\": 4}"
        model.draft.startedText = "2026-11-08T12:00:00.123456Z"
        model.draft.endedText = "2026-11-08T12:00:01.123456Z"
        model.analyze(); try await waitForSave(deferred)
        XCTAssertEqual(deferred.saves[0].1.interval.startedAt, model.draft.startedText)
        deferred.fail(PlatformError.http(status: 400, code: "synthetic", message: "cleanup"))
        try await waitFor { !model.pending }
    }

    @MainActor func testSaveSubmitsDraftAndKeepsItems() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        try await waitFor { !model.catalog.isEmpty }
        model.draft.values = "{\"bandwidth_hz\": 1, \"lower_edge_hz\": 2, \"upper_edge_hz\": 3, \"center_hz\": 4}"
        model.analyze()
        XCTAssertTrue(model.pending)
        model.analyze()
        try await waitForSave(deferred)
        XCTAssertEqual(deferred.saves.count, 1, "single flight")
        XCTAssertEqual(deferred.saves[0].1.methodology, "beta2_bandwidth")
        XCTAssertEqual(deferred.saves[0].1.resultCode, "norm")
        XCTAssertEqual(deferred.saves[0].1.streamId, "tiny")
        deferred.release(try record())
        try await waitFor { !model.pending }
        XCTAssertEqual(model.items.map(\.id), ["result-1"])
        XCTAssertNotNil(model.infoText)
    }

    @MainActor func testForgedRecordAndFailureNeverFreeze() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        try await waitFor { !model.catalog.isEmpty }
        model.draft.values = "{\"bandwidth_hz\": 1, \"lower_edge_hz\": 2, \"upper_edge_hz\": 3, \"center_hz\": 4}"
        model.analyze()
        try await waitForSave(deferred)
        let forged = try decode([
            "id": "result-x", "session_id": "other", "stream_id": "tiny",
            "methodology": "beta2_bandwidth", "result_code": "norm", "frequency_hz": 101_800_000,
            "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
            "values": ["bandwidth_hz": 1.0], "created_at": "2026-11-08T12:00:02Z",
        ], as: PlatformMeasurementResult.self)
        deferred.release(forged)
        try await waitFor { !model.pending }
        XCTAssertTrue(model.items.isEmpty, "a foreign-session record is never accepted")
        XCTAssertNotNil(model.errorText)
        model.analyze()
        try await waitFor { deferred.continuations.count == 1 }
        deferred.fail(PlatformError.http(status: 400, code: "invalid_request", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertTrue(model.items.isEmpty)
    }
}
