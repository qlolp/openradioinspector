import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class FrequencyCounterTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let envelope: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "counter_incompatible", "message": "synthetic"]]
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
    func resultObject(session: String = "s", sweep: String = "sweep-0",
                      start: Int = 100, stop: Int = 400, resolution: Int = 10) -> [String: Any] {
        ["session_id": session, "stream_id": "tiny",
         "sweep": ["id": sweep, "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
                   "level_unit": "dBm", "settings": ["rbw_hz": 1000]],
         "window": ["start_hz": start, "stop_hz": stop], "resolution_hz": resolution, "raster_hz": 100,
         "method": "parabolic", "peak_index": 2, "peak_frequency_hz": 300, "peak_level_dbm": -50.0,
         "measured_frequency_hz": 343.75, "uncertainty_hz": 50.0,
         "measurement_methodology": "frequency_counter",
         "values": ["measured_frequency_hz": 343.75, "resolution_hz": 10.0, "uncertainty_hz": 50.0]]
    }
    func request(_ sweep: String = "sweep-0", resolution: Int64 = 10,
                 start: Int64 = 100, stop: Int64 = 400) -> PlatformFrequencyCounterRequest {
        PlatformFrequencyCounterRequest(sweepId: sweep, resolutionHz: resolution, startHz: start, stopHz: stop)
    }

    func testResultDecodingGuards() throws {
        XCTAssertEqual(try decode(resultObject(), as: PlatformFrequencyCounterResult.self).method, "parabolic")
        for changes in [["method": "guess"], ["uncertainty_hz": -1.0],
                        ["measured_frequency_hz": 999.0], ["resolution_hz": -1]] as [[String: Any]] {
            var object = resultObject()
            object.merge(changes) { _, new in new }
            XCTAssertThrowsError(try decode(object, as: PlatformFrequencyCounterResult.self), "\(changes)")
        }
        var wrongValues = resultObject()
        wrongValues["values"] = ["measured_frequency_hz": 1.0]
        XCTAssertThrowsError(try decode(wrongValues, as: PlatformFrequencyCounterResult.self))
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
            stub.respond(self.resultObject())
        }
        let value = try await api().frequencyCounter(sessionID: "s", request())
        XCTAssertEqual(value.measuredFrequencyHz, 343.75, accuracy: 1e-9)
        XCTAssertEqual(sent.count, 1)
        XCTAssertTrue(sent[0].0.hasSuffix("/sessions/s/frequency-counter"))
        let body = try JSONSerialization.jsonObject(with: sent[0].1) as! [String: Any]
        XCTAssertEqual(Set(body.keys), ["sweep_id", "resolution_hz", "start_hz", "stop_hz"])
        let client = try api()
        for bad in [request(resolution: 0), request(start: 400, stop: 400), request(stop: -1),
                    request("")] {
            do {
                _ = try await client.frequencyCounter(sessionID: "s", bad)
                XCTFail("invalid request must be refused")
            } catch {}
        }
        do {
            _ = try await client.frequencyCounter(sessionID: "", request())
            XCTFail("empty session must be refused")
        } catch {}
    }
}
