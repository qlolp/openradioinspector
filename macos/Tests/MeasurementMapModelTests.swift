import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class MeasurementMapModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, String)] = []
        var continuations: [CheckedContinuation<PlatformMeasurementMap, Error>] = []
        func measure(_ session: String, _ json: String) async throws -> PlatformMeasurementMap {
            requests.append((session, json))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformMeasurementMap) { continuations.removeFirst().resume(returning: value) }
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
                : ["error": ["code": "measurement_map_error", "message": "synthetic"]]
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
    func resultObject(session: String = "s") -> [String: Any] {
        ["session_id": session,
         "geojson": ["type": "FeatureCollection", "features": [
            ["type": "Feature", "geometry": ["type": "Point", "coordinates": [37.62, 55.75]],
             "properties": ["kind": "measurement_point", "label": "Точка 1"]],
         ]],
         "summary": ["point_count": 1, "sector_count": 0,
                     "bbox": ["min_latitude_deg": 55.75, "max_latitude_deg": 55.75,
                              "min_longitude_deg": 37.62, "max_longitude_deg": 37.62]],
         "map": ["format": "openradioinspector-measurement-map", "format_version": 1,
                 "name": "Карта", "points": []]]
    }
    func mapJSON() -> String {
        "{\"format\":\"openradioinspector-measurement-map\",\"format_version\":1,\"name\":\"Карта\",\"points\":[{\"latitude_deg\":55.75,\"longitude_deg\":37.62,\"label\":\"Точка 1\",\"sector\":null}]}"
    }
    @MainActor private var scope: MeasurementMapModel.Scope { .init(server: "local", session: "s") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for map state")
    }

    @MainActor func testEmptyDraftDisablesAndBuildEchoesScope() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = MeasurementMapModel(
            timeoutNanoseconds: 60_000_000_000,
            measure: { _, session, json in try await deferred.measure(session, json) })
        model.configure(client: client, scope: scope)
        XCTAssertFalse(model.canBuild, "empty draft cannot build")
        model.draft = mapJSON()
        XCTAssertTrue(model.canBuild)
        model.analyze()
        XCTAssertTrue(model.pending)
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        XCTAssertEqual(deferred.requests.count, 1, "single flight")
        XCTAssertEqual(deferred.requests[0].1, mapJSON(), "raw draft is never rewritten")
        deferred.release(try decode(resultObject(), as: PlatformMeasurementMap.self))
        try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.summary.pointCount, 1)
        XCTAssertNotNil(model.infoText)
    }

    @MainActor func testFailureNeverFreezes() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = MeasurementMapModel(
            timeoutNanoseconds: 60_000_000_000,
            measure: { _, session, json in try await deferred.measure(session, json) })
        model.configure(client: client, scope: scope)
        model.draft = mapJSON()
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        deferred.release(try decode(resultObject(session: "other"), as: PlatformMeasurementMap.self))
        try await waitFor { !model.pending }
        XCTAssertNil(model.result, "a foreign-session map is never accepted")
        XCTAssertNotNil(model.errorText)
    }

    @MainActor func testDecodingGuards() throws {
        let good = try decode(resultObject(), as: PlatformMeasurementMap.self)
        XCTAssertEqual(good.summary.sectorCount, 0)
        var broken = resultObject()
        broken["summary"] = ["point_count": 1]
        XCTAssertThrowsError(try decode(broken, as: PlatformMeasurementMap.self))
    }
}
