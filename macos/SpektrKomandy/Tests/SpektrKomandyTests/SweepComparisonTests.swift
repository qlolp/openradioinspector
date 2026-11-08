import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class SweepComparisonTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: [String: Any], status: Int = 200, contract: String = "openradioinspector-platform") {
            let payload: [String: Any] = status == 200
                ? ["contract": contract, "contract_version": 1, "data": object]
                : ["error": object]
            let data = try! JSONSerialization.data(withJSONObject: payload)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    private func client() throws -> PlatformClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: configuration))
    }

    override func tearDown() {
        Stub.handler = nil
        Stub.onStop = nil
        super.tearDown()
    }

    private func fixture(session: String = "s", reference: String = "r", target: String = "t") -> [String: Any] {
        let interval: [String: Any] = ["started_at": "2026-11-08T01:00:00.123456+00:00", "ended_at": "2026-11-08T01:00:01Z"]
        let recorded: [String: Any] = ["rbw_hz": 1000, "extension": ["gain": 2.5, "enabled": false, "modes": ["scan"]]]
        return ["session_id": session, "stream_id": "tiny", "reference": ["id": reference, "interval": interval, "levels": [-90, -60, -50], "level_unit": "dBm", "settings": recorded],
                "target": ["id": target, "interval": interval, "levels": [-80, -80, -40], "level_unit": "dBm", "settings": recorded],
                "frequencies_hz": [100, 200, 900], "delta_levels": [10, -20, 10], "delta_unit": "dB", "points": 3,
                "max_increase": ["frequency_hz": 100, "delta_db": 10], "min_change": ["frequency_hz": 200, "delta_db": -20]]
    }

    private func decode(_ fixture: [String: Any]) throws -> PlatformSweepComparison {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweepComparison.self, from: JSONSerialization.data(withJSONObject: fixture))
    }

    func testExactIDsAndPrefixAreEncodedAsReadOnlyBoundedRequest() async throws {
        let body = fixture(session: "s/a?", reference: "r &+", target: "t/é")
        Stub.handler = { stub in
            XCTAssertEqual(stub.request.httpMethod, "GET")
            XCTAssertEqual(stub.request.timeoutInterval, 5)
            XCTAssertNil(stub.request.httpBody)
            XCTAssertEqual(stub.request.url?.absoluteString,
                "https://example.test/root/api/v1/sessions/s%2Fa%3F/sweep-comparison?reference_id=r%20%26%2B&target_id=t%2F%C3%A9")
            stub.respond(body)
        }
        let result = try await client().sweepComparison(sessionID: "s/a?", referenceID: "r &+", targetID: "t/é")
        XCTAssertEqual(result.deltaLevels, [10, -20, 10])
        XCTAssertEqual(result.reference.levels, [-90, -60, -50])
        XCTAssertEqual(result.deltaUnit, "dB")
        XCTAssertEqual(result.target.levelUnit, "dBm")
        XCTAssertEqual(result.reference.settings["rbw_hz"], .number(1000))
        XCTAssertEqual(result.maxIncrease.frequencyHz, 100) // first tied bin, not last
    }

    func testSameIDZeroAndSignedNegativeMaximumAreAccepted() throws {
        var body = fixture(reference: "same", target: "same")
        body["delta_levels"] = [0, 0, 0]
        body["max_increase"] = ["frequency_hz": 100, "delta_db": 0]
        body["min_change"] = ["frequency_hz": 100, "delta_db": 0]
        XCTAssertEqual(try decode(body).deltaLevels, [0, 0, 0])
        body["delta_levels"] = [-3, -20, -3]
        body["max_increase"] = ["frequency_hz": 100, "delta_db": -3]
        body["min_change"] = ["frequency_hz": 200, "delta_db": -20]
        XCTAssertEqual(try decode(body).maxIncrease.deltaDb, -3)
    }

    func testDuplicateAndZeroFrequencyBinsRemainValid() throws {
        var body = fixture()
        body["frequencies_hz"] = [0, 0, 0]
        body["max_increase"] = ["frequency_hz": 0, "delta_db": 10]
        body["min_change"] = ["frequency_hz": 0, "delta_db": -20]
        XCTAssertEqual(try decode(body).frequenciesHz, [0, 0, 0])
    }

    func testMalformedComparisonValuesAreRejectedWithoutCoercion() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["session_id"] = " " }, { $0["stream_id"] = "" },
            { $0["points"] = 0 }, { $0["points"] = 16_385 }, { $0["points"] = true },
            { $0["delta_unit"] = "dBm" }, { $0["delta_levels"] = [10, -20] },
            { $0["delta_levels"] = [10, "bad", 10] }, { $0["delta_levels"] = [true, -20, 10] },
            { $0["frequencies_hz"] = [100, 90, 900] }, { $0["frequencies_hz"] = [100.5, 200, 900] },
            { $0["max_increase"] = ["frequency_hz": 900, "delta_db": 10] },
            { $0["min_change"] = ["frequency_hz": 200, "delta_db": 10] },
            { var r = $0["reference"] as! [String: Any]; r["level_unit"] = "s_meter_count"; $0["reference"] = r },
            { var r = $0["reference"] as! [String: Any]; r["levels"] = [-90]; $0["reference"] = r },
            { var r = $0["reference"] as! [String: Any]; r["settings"] = ["sequence": 1]; $0["reference"] = r },
            { var r = $0["reference"] as! [String: Any]; r["settings"] = ["rbw_hz": NSNull()]; $0["reference"] = r },
            { var r = $0["reference"] as! [String: Any]; r["interval"] = ["started_at": "2026-11-08", "ended_at": "bad"]; $0["reference"] = r },
            { var r = $0["reference"] as! [String: Any]; r["interval"] = ["started_at": "2026-11-08T02:00:00Z", "ended_at": "2026-11-08T01:00:00Z"]; $0["reference"] = r }
        ]
        for (index, mutation) in mutations.enumerated() {
            var body = fixture(); mutation(&body)
            XCTAssertThrowsError(try decode(body), "Mutation \(index)")
        }
    }

    func testHTTP404And409RemainStructuredErrors() async throws {
        for (status, code) in [(404, "sweep_not_found"), (409, "comparison_incompatible")] {
            Stub.handler = { $0.respond(["code": code, "message": "Unavailable"], status: status) }
            do {
                _ = try await client().sweepComparison(sessionID: "s", referenceID: "r", targetID: "t")
                XCTFail("Expected HTTP error")
            } catch let error as PlatformError {
                XCTAssertEqual(error, .http(status: status, code: code, message: "Unavailable"))
            }
        }
    }

    func testMalformedEnvelopeAndWrongIdentitiesAreRejected() async throws {
        for field in ["contract", "session", "reference", "target", "payload"] {
            var body = fixture(session: field == "session" ? "other" : "s",
                reference: field == "reference" ? "other" : "r", target: field == "target" ? "other" : "t")
            if field == "payload" { body["delta_levels"] = [] }
            Stub.handler = { $0.respond(body, contract: field == "contract" ? "other" : "openradioinspector-platform") }
            do {
                _ = try await client().sweepComparison(sessionID: "s", referenceID: "r", targetID: "t")
                XCTFail("Expected invalid response for \(field)")
            } catch PlatformError.invalidResponse {} catch { XCTFail("Unexpected error \(error)") }
        }
    }

    func testBlankIdentityIsRejectedWithoutNetwork() async throws {
        Stub.handler = { _ in XCTFail("No network for blank IDs") }
        do {
            _ = try await client().sweepComparison(sessionID: "s", referenceID: " \n", targetID: "t")
            XCTFail("Expected invalid command")
        } catch PlatformError.invalidCommand {} catch { XCTFail("Unexpected error \(error)") }
    }

    func testCancellationStopsTransportAndPreservesCancellationIdentity() async throws {
        let started = expectation(description: "Request started")
        let stopped = expectation(description: "Transport stopped")
        Stub.handler = { _ in started.fulfill() }
        Stub.onStop = { stopped.fulfill() }
        let api = try client()
        let task = Task { try await api.sweepComparison(sessionID: "s", referenceID: "r", targetID: "t") }
        await fulfillment(of: [started], timeout: 2)
        task.cancel()
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch is CancellationError {} catch { XCTFail("Unexpected error \(error)") }
        await fulfillment(of: [stopped], timeout: 2)
    }
}
