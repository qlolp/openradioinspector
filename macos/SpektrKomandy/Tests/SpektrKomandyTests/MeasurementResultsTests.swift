import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class MeasurementResultsTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let envelope: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "invalid_request", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: envelope)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func recordObject(code: String = "norm", session: String = "s", stream: String = "tiny") -> [String: Any] {
        let values: [String: Any] = code == "not_measured" ? [:]
            : ["bandwidth_hz": 1.0, "lower_edge_hz": 2.0, "upper_edge_hz": 3.0, "center_hz": 4.0]
        return [
            "id": "result-1", "session_id": session, "stream_id": stream,
            "methodology": "beta2_bandwidth", "result_code": code, "frequency_hz": 101_800_000,
            "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
            "values": values, "created_at": "2026-11-08T12:00:02Z",
        ]
    }
    func record(code: String = "norm", session: String = "s", stream: String = "tiny") throws -> PlatformMeasurementResult {
        try decode(recordObject(code: code, session: session, stream: stream), as: PlatformMeasurementResult.self)
    }
    func draft(code: String = "norm") -> PlatformMeasurementResultDraft {
        let values: [String: Double] = code == "not_measured" ? [:]
            : ["bandwidth_hz": 1, "lower_edge_hz": 2, "upper_edge_hz": 3, "center_hz": 4]
        return PlatformMeasurementResultDraft(
            methodology: "beta2_bandwidth", resultCode: code, frequencyHz: 101_800_000,
            interval: PlatformInterval(startedAt: "2026-11-08T12:00:00Z", endedAt: "2026-11-08T12:00:01Z"),
            values: values, streamId: "tiny")
    }

    func testRecordDecodingGuards() throws {
        XCTAssertEqual(try record().values.count, 4)
        XCTAssertEqual(try record(code: "not_measured").values, [:])
        for changes in [["result_code": "ok"], ["result_code": 1],
                        ["session_id": ""], ["frequency_hz": -1]] as [[String: Any]] {
            var object: [String: Any] = [
                "id": "result-1", "session_id": "s", "stream_id": "tiny",
                "methodology": "beta2_bandwidth", "result_code": "norm", "frequency_hz": 101_800_000,
                "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                "values": ["bandwidth_hz": 1.0], "created_at": "2026-11-08T12:00:02Z",
            ]
            object.merge(changes) { _, new in new }
            XCTAssertThrowsError(try decode(object, as: PlatformMeasurementResult.self), "\(changes)")
        }
    }

    func testClientRoundTripAndInputGuards() async throws {
        var sent: [(String, Data)] = []
        Stub.handler = { stub in
            var request = stub.request
            if request.httpBody == nil, let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
                while true {
                    let count = stream.read(&buffer, maxLength: buffer.count)
                    if count <= 0 { break }
                    data.append(contentsOf: buffer.prefix(count))
                }
                request.httpBody = data
            }
            sent.append((request.url!.path, request.httpBody ?? Data()))
            stub.respond(self.recordObject(), status: 201)
        }
        let value = try await api().saveMeasurementResult(sessionID: "s", draft())
        XCTAssertEqual(value.id, "result-1")
        XCTAssertEqual(sent.count, 1)
        XCTAssertTrue(sent[0].0.hasSuffix("/sessions/s/measurement-results"))
        let body = try JSONSerialization.jsonObject(with: sent[0].1) as! [String: Any]
        XCTAssertEqual(Set(body.keys), ["methodology", "result_code", "frequency_hz", "interval", "values", "stream_id"])
        XCTAssertEqual(body["stream_id"] as? String, "tiny")
        let client = try api()
        for bad in [draft(code: "ok"),
                    PlatformMeasurementResultDraft(
                        methodology: "beta2_bandwidth", resultCode: "not_measured", frequencyHz: 1,
                        interval: PlatformInterval(startedAt: "x", endedAt: "y"),
                        values: ["bandwidth_hz": 1], streamId: "tiny")] {
            do {
                _ = try await client.saveMeasurementResult(sessionID: "s", bad)
                XCTFail("invalid draft must be refused")
            } catch {}
        }
        do {
            _ = try await client.saveMeasurementResult(sessionID: "", draft())
            XCTFail("empty session must be refused")
        } catch {}
    }
}
