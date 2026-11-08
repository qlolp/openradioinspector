import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class FrequencyRasterTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: Any, status: Int = 200) {
            let envelope: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "invalid_request", "message": "synthetic"]]
            bytes(try! JSONSerialization.data(withJSONObject: envelope), status: status)
        }
        func bytes(_ data: Data, status: Int = 200) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func table() -> [String: Any] {
        ["format": "openradioinspector-frequency-raster", "format_version": 1, "name": "Synthetic raster",
         "frequency_unit": "Hz",
         "entries": [["name": "band", "start_hz": 100_000_000, "stop_hz": 110_000_000, "grid_step_hz": 100_000]]]
    }
    func items() -> [[String: Any]] {
        [["candidate_id": "c1", "frequency_hz": 101_234_567, "matched": true,
          "rounded_frequency_hz": 101_200_000, "delta_hz": -34_567, "grid_step_hz": 100_000, "entry_name": "band"],
         ["candidate_id": "c2", "frequency_hz": 50_000_000, "matched": false,
          "rounded_frequency_hz": NSNull(), "delta_hz": NSNull(), "grid_step_hz": NSNull(), "entry_name": NSNull()]]
    }
    func result() -> [String: Any] {
        ["session_id": "s", "order": "request", "table": table(), "count": 2,
         "matched_count": 1, "unmatched_count": 1, "items": items()]
    }

    func testNegativeBoundsThrowAndDraftNamesNormalize() throws {
        var invalid = table()
        invalid["entries"] = [["name": "band", "start_hz": 0, "stop_hz": -1, "grid_step_hz": 1]]
        let text = String(data: try JSONSerialization.data(withJSONObject: invalid), encoding: .utf8)!
        XCTAssertThrowsError(try PlatformFrequencyRaster.submitted(text))
        XCTAssertThrowsError(try decode(invalid, as: PlatformFrequencyRaster.self))
        var draft = table()
        draft["entries"] = [["name": "  cafe\u{301}  ", "start_hz": 100_000_000, "stop_hz": 110_000_000, "grid_step_hz": 100_000]]
        let accepted = try PlatformFrequencyRaster.submitted(String(data: try JSONSerialization.data(withJSONObject: draft), encoding: .utf8)!)
        XCTAssertEqual(accepted.entries[0].name, "café")
    }

    func testArtifactStrictnessAndCanonicalIdentity() throws {
        let raster = try decode(table(), as: PlatformFrequencyRaster.self)
        XCTAssertEqual(raster.entries.first?.gridStepHz, 100_000)
        let loose: [String: Any] = ["format": "openradioinspector-frequency-raster", "format_version": 1,
            "name": "  Synthetic raster  ", "frequency_unit": "Hz",
            "entries": [["name": "band", "start_hz": 100_000_000, "stop_hz": 110_000_000, "grid_step_hz": 100_000]]]
        let raw = try JSONSerialization.data(withJSONObject: loose)
        let accepted = try PlatformFrequencyRaster.submitted(String(data: raw, encoding: .utf8)!)
        XCTAssertEqual(accepted, raster)
        for changes in [["extra": 1], ["format_version": true], ["frequency_unit": "kHz"], ["entries": []]] {
            var bad = table(); bad.merge(changes) { _, new in new }
            XCTAssertThrowsError(try decode(bad, as: PlatformFrequencyRaster.self), "\(changes)")
        }
        for changes in [["start_hz": 110_000_000], ["stop_hz": 100_000_000], ["grid_step_hz": 0],
                        ["grid_step_hz": 10_000_001], ["name": ""], ["name": " x "]] as [[String: Any]] {
            var bad = table(); var entries = bad["entries"] as! [[String: Any]]
            entries[0].merge(changes) { _, new in new }
            bad["entries"] = entries
            XCTAssertThrowsError(try decode(bad, as: PlatformFrequencyRaster.self), "\(changes)")
        }
        // Overlapping and unsorted entries are refused.
        var overlapping = table()
        overlapping["entries"] = [(table()["entries"] as! [[String: Any]])[0],
                                  ["name": "b", "start_hz": 105_000_000, "stop_hz": 120_000_000, "grid_step_hz": 100_000]]
        XCTAssertThrowsError(try decode(overlapping, as: PlatformFrequencyRaster.self))
    }

    func testRoundingResultGuards() throws {
        let good = try decode(result(), as: PlatformFrequencyRounding.self)
        XCTAssertEqual(good.matchedCount, 1)
        try good.validate(table: String(data: try JSONSerialization.data(withJSONObject: table()), encoding: .utf8)!,
                          candidateIDs: ["c1", "c2"])
        for changes in [["order": "sorted"], ["count": 3], ["matched_count": 0], ["unmatched_count": 0]] {
            var bad = result(); bad.merge(changes) { _, new in new }
            XCTAssertThrowsError(try decode(bad, as: PlatformFrequencyRounding.self), "\(changes)")
        }
        // Off-grid or out-of-range rounded values are refused.
        for changes in [["rounded_frequency_hz": 101_234_500, "delta_hz": -67],
                        ["rounded_frequency_hz": 99_000_000, "delta_hz": -2_234_567],
                        ["rounded_frequency_hz": 101_350_000, "delta_hz": 115_433]] as [[String: Any]] {
            var bad = result(); var rows = bad["items"] as! [[String: Any]]
            rows[0].merge(changes) { _, new in new }
            bad["items"] = rows
            XCTAssertThrowsError(try decode(bad, as: PlatformFrequencyRounding.self), "\(changes)")
        }
        // Unmatched rows never carry derived values.
        var leaky = result(); var rows = leaky["items"] as! [[String: Any]]
        rows[1]["rounded_frequency_hz"] = 1
        leaky["items"] = rows
        XCTAssertThrowsError(try decode(leaky, as: PlatformFrequencyRounding.self))
        // A forged table echo is refused by validate().
        var renamed = result()
        var forgedTable = table(); forgedTable["name"] = "other"
        renamed["table"] = forgedTable
        let forged = try decode(renamed, as: PlatformFrequencyRounding.self)
        XCTAssertThrowsError(try forged.validate(
            table: String(data: try JSONSerialization.data(withJSONObject: table()), encoding: .utf8)!,
            candidateIDs: ["c1", "c2"]))
    }

    func testClientRoundTripAndInputGuards() async throws {
        var sent: [(String, Data)] = []
        Stub.handler = { stub in
            let request = try! bufferedTestRequest(stub.request)
            sent.append((request.url!.path, request.httpBody ?? Data()))
            stub.respond(self.result())
        }
        let value = try await api().frequencyRounding(
            sessionID: "s", candidateIDs: ["c1", "c2"],
            tableJSON: String(data: try JSONSerialization.data(withJSONObject: table()), encoding: .utf8)!)
        XCTAssertEqual(value.count, 2)
        XCTAssertEqual(sent.count, 1)
        XCTAssertTrue(sent[0].0.hasSuffix("/sessions/s/frequency-rounding"))
        let body = try JSONSerialization.jsonObject(with: sent[0].1) as! [String: Any]
        XCTAssertEqual(Set(body.keys), ["table_json", "candidate_ids"])
        XCTAssertEqual(body["candidate_ids"] as! [String], ["c1", "c2"])
        let client = try api()
        do {
            _ = try await client.frequencyRounding(sessionID: "s", candidateIDs: [], tableJSON: "{}")
            XCTFail("empty candidate list must be refused")
        } catch {}
        do {
            _ = try await client.frequencyRounding(sessionID: "s", candidateIDs: ["c1", "c1"], tableJSON: "{}")
            XCTFail("duplicate candidate IDs must be refused")
        } catch {}
        do {
            _ = try await client.frequencyRounding(sessionID: "s", candidateIDs: ["c1"], tableJSON: "{bad")
            XCTFail("malformed draft must be refused before any request")
        } catch {}
    }
}
