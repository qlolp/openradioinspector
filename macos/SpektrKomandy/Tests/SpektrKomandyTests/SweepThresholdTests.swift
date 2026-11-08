import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class SweepThresholdTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: [String: Any], status: Int = 200, contract: String = "openradioinspector-platform") {
            let payload: [String: Any] = (200...299).contains(status)
                ? ["contract": contract, "contract_version": 1, "data": object] : ["error": object]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
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

    private func fixture(session: String = "s", sweep: String = "w", window: Int = 21, margin: Double = 6) -> [String: Any] {
        ["session_id": session, "stream_id": "tiny",
         "sweep": ["id": sweep, "interval": ["started_at": "2026-11-08T01:00:00.123456+00:00", "ended_at": "2026-11-08T01:00:01Z"],
                   "levels": [-90, -90, -60, -90, -90], "level_unit": "dBm",
                   "settings": ["rbw_hz": 1000, "sequence": NSNull(), "extension": ["gain": 2.5, "enabled": false, "modes": ["scan"]]]],
         "frequencies_hz": [0, 100, 100, 300, 1000],
         "parameters": ["method": "rolling_median", "window_points": window, "margin_db": margin, "edge_policy": "clip", "comparison": "strictly_greater"],
         "baseline_levels": Array(repeating: -90, count: 5), "threshold_levels": Array(repeating: -90 + margin, count: 5),
         "level_unit": "dBm", "excess_levels": [-margin, -margin, 30 - margin, -margin, -margin], "delta_unit": "dB",
         "regions": margin < 30 ? [["start_index": 2, "end_index": 2, "start_frequency_hz": 100, "end_frequency_hz": 100,
                    "peak_index": 2, "peak_frequency_hz": 100, "peak_excess_db": 30 - margin, "points": 1]] : [],
         "points": 5, "exceedance_points": margin < 30 ? 1 : 0, "region_count": margin < 30 ? 1 : 0]
    }

    private func decode(_ fixture: [String: Any]) throws -> PlatformSweepThreshold {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweepThreshold.self, from: JSONSerialization.data(withJSONObject: fixture))
    }

    func testExactEncodedReadOnlyQueryAndFrozenUnitsProvenance() async throws {
        let body = fixture(session: "s/a?", sweep: "w &+/é")
        Stub.handler = { stub in
            XCTAssertEqual(stub.request.httpMethod, "GET")
            XCTAssertEqual(stub.request.timeoutInterval, 5)
            XCTAssertNil(stub.request.httpBody)
            XCTAssertEqual(stub.request.url?.absoluteString,
                "https://example.test/root/api/v1/sessions/s%2Fa%3F/sweep-threshold?sweep_id=w%20%26%2B%2F%C3%A9&window_points=21&margin_db=6.0")
            let items = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            XCTAssertEqual(items.count, 3)
            XCTAssertEqual(Set(items.map(\.name)).count, 3)
            stub.respond(body)
        }
        let result = try await client().sweepThreshold(sessionID: "s/a?", sweepID: "w &+/é")
        XCTAssertEqual(result.sweep.levels, [-90, -90, -60, -90, -90])
        XCTAssertEqual(result.thresholdLevels, [-84, -84, -84, -84, -84])
        XCTAssertEqual(result.excessLevels, [-6, -6, 24, -6, -6])
        XCTAssertEqual(result.sweep.settings["rbw_hz"], .number(1000))
        XCTAssertEqual(result.regions.first?.startIndex, 2)
    }

    func testLargeWindowAndEqualityDoNotExceed() throws {
        let value = try decode(fixture(window: 101, margin: 30))
        XCTAssertEqual(value.exceedancePoints, 0)
        XCTAssertEqual(value.regions, [])
        XCTAssertEqual(value.excessLevels[2], 0)
        XCTAssertEqual(try decode(fixture(margin: 120)).regionCount, 0)
        XCTAssertEqual(try decode(fixture(margin: 0)).exceedancePoints, 1)
    }

    func testInvalidRequestsNeverReachNetwork() async throws {
        Stub.handler = { _ in XCTFail("Invalid input must not make a request") }
        let api = try client()
        for (session, sweep, window, margin) in [("", "w", 21, 6.0), ("s", " \n", 21, 6.0),
                ("s", "w", 2, 6), ("s", "w", 4, 6), ("s", "w", 103, 6),
                ("s", "w", 21, -1), ("s", "w", 21, 121), ("s", "w", 21, Double.nan),
                ("s", "w", 21, Double.infinity), ("s", "w", 21, -Double.infinity)] {
            do { _ = try await api.sweepThreshold(sessionID: session, sweepID: sweep, windowPoints: window, marginDb: margin)
                XCTFail("Expected invalid request")
            } catch PlatformError.invalidCommand {} catch { XCTFail("Unexpected \(error)") }
        }
    }

    func testOnlyHTTP200SucceedsAndErrorsStayStructured() async throws {
        let api = try client()
        for status in [201, 202, 204, 206, 299] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await api.sweepThreshold(sessionID: "s", sweepID: "w"); XCTFail("Accepted \(status)") }
            catch PlatformError.invalidResponse {} catch { XCTFail("Unexpected \(error)") }
        }
        for (status, code) in [(400, "invalid_request"), (404, "session_not_found"), (404, "sweep_not_found"), (409, "threshold_incompatible")] {
            Stub.handler = { $0.respond(["code": code, "message": "Unavailable"], status: status) }
            do { _ = try await api.sweepThreshold(sessionID: "s", sweepID: "w"); XCTFail("Accepted \(status)") }
            catch let error as PlatformError { XCTAssertEqual(error, .http(status: status, code: code, message: "Unavailable")) }
        }
    }

    func testWrongEnvelopeIdentityAndEchoedParametersRejected() async throws {
        let api = try client()
        for field in ["contract", "session", "sweep", "window", "margin", "malformed"] {
            var body = fixture(session: field == "session" ? "other" : "s", sweep: field == "sweep" ? "other" : "w",
                               window: field == "window" ? 3 : 21, margin: field == "margin" ? 7 : 6)
            if field == "malformed" { body["excess_levels"] = [] }
            Stub.handler = { $0.respond(body, contract: field == "contract" ? "other" : "openradioinspector-platform") }
            do { _ = try await api.sweepThreshold(sessionID: "s", sweepID: "w"); XCTFail("Accepted \(field)") }
            catch PlatformError.invalidResponse {} catch { XCTFail("Unexpected \(error)") }
        }
    }

    func testMalformedNumbersCountsUnitsAndProvenanceRejected() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["session_id"] = " " }, { $0["stream_id"] = "" },
            { $0["points"] = 2 }, { $0["points"] = 16_385 }, { $0["points"] = true },
            { $0["frequencies_hz"] = [0, 100, 99, 300, 1000] },
            { $0["frequencies_hz"] = [0, 100.5, 100, 300, 1000] },
            { $0["frequencies_hz"] = [-1, 100, 100, 300, 1000] },
            { $0["frequencies_hz"] = [false, 100, 100, 300, 1000] },
            { $0["level_unit"] = "dB" }, { $0["delta_unit"] = "dBm" },
            { $0["baseline_levels"] = [-90, -90] }, { $0["threshold_levels"] = [-84, -84, -84, -84, "bad"] },
            { $0["baseline_levels"] = [true, -90, -90, -90, -90] },
            { $0["threshold_levels"] = [-84, -84, -85, -84, -84] },
            { $0["excess_levels"] = [-6, -6, 23, -6, -6] },
            { $0["exceedance_points"] = 2 }, { $0["region_count"] = 0 },
            { var r = $0["sweep"] as! [String: Any]; r["id"] = " "; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["level_unit"] = "s_meter_count"; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["levels"] = [-90]; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["settings"] = ["sequence": 1]; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["settings"] = ["rbw_hz": NSNull()]; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["settings"] = ["extension": ["value": NSNull()]]; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["interval"] = ["started_at": "2026-11-08", "ended_at": "bad"]; $0["sweep"] = r },
            { var r = $0["sweep"] as! [String: Any]; r["interval"] = ["started_at": "2026-11-08T02:00:00Z", "ended_at": "2026-11-08T01:00:00Z"]; $0["sweep"] = r }
        ]
        for (index, mutation) in mutations.enumerated() {
            var body = fixture(); mutation(&body)
            XCTAssertThrowsError(try decode(body), "Mutation \(index)")
        }
        for (key, value) in [("method", "other" as Any), ("edge_policy", "reflect"), ("comparison", "greater_or_equal"),
                             ("window_points", 4), ("window_points", true), ("margin_db", -1), ("margin_db", 121), ("margin_db", false)] {
            var body = fixture(); var parameters = body["parameters"] as! [String: Any]
            parameters[key] = value; body["parameters"] = parameters
            XCTAssertThrowsError(try decode(body), key)
        }
    }

    func testMalformedRegionsAndFirstTiedPeakAreRejected() throws {
        for (key, value) in [("start_index", -1 as Any), ("start_index", 3), ("end_index", 5), ("points", 2),
                             ("peak_index", 3), ("start_frequency_hz", 0), ("end_frequency_hz", 0),
                             ("peak_frequency_hz", 300), ("peak_excess_db", 0), ("peak_excess_db", true)] {
            var body = fixture(); var regions = body["regions"] as! [[String: Any]]
            regions[0][key] = value; body["regions"] = regions
            XCTAssertThrowsError(try decode(body), key)
        }
        var body = fixture()
        body["regions"] = []; body["region_count"] = 0
        XCTAssertThrowsError(try decode(body))
        var sweep = body["sweep"] as! [String: Any]
        sweep["levels"] = [-90, -90, -60, -60, -90]; body["sweep"] = sweep
        body["excess_levels"] = [-6, -6, 24, 24, -6]; body["exceedance_points"] = 2
        var region: [String: Any] = ["start_index": 2, "end_index": 3, "start_frequency_hz": 100,
            "end_frequency_hz": 300, "peak_index": 2, "peak_frequency_hz": 100, "peak_excess_db": 24, "points": 2]
        body["regions"] = [region]; body["region_count"] = 1
        XCTAssertEqual(try decode(body).regions.first?.peakIndex, 2)
        region["peak_index"] = 3; region["peak_frequency_hz"] = 300; body["regions"] = [region]
        XCTAssertThrowsError(try decode(body))
        region["peak_index"] = 2; region["peak_frequency_hz"] = 100
        region["end_index"] = 2; region["end_frequency_hz"] = 100; region["points"] = 1; body["regions"] = [region]
        XCTAssertThrowsError(try decode(body)) // a consecutive positive run cannot be split
    }

    func testNonfiniteDecodedDoublesRejectedEvenWithPermissiveDecoder() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.nonConformingFloatDecodingStrategy = .convertFromString(positiveInfinity: "Inf", negativeInfinity: "-Inf", nan: "NaN")
        for field in ["baseline_levels", "threshold_levels", "excess_levels"] {
            for value in ["Inf", "-Inf", "NaN"] {
                var body = fixture(); body[field] = [value, -90, -90, -90, -90] as [Any]
                XCTAssertThrowsError(try decoder.decode(PlatformSweepThreshold.self, from: JSONSerialization.data(withJSONObject: body)))
            }
        }
        var rawBody = fixture()
        var sweep = rawBody["sweep"] as! [String: Any]
        sweep["levels"] = ["NaN", -90, -60, -90, -90] as [Any]; rawBody["sweep"] = sweep
        XCTAssertThrowsError(try decoder.decode(PlatformSweepThreshold.self, from: JSONSerialization.data(withJSONObject: rawBody)))
        for field in ["margin_db", "peak_excess_db"] {
            var body = fixture()
            if field == "margin_db" {
                var p = body["parameters"] as! [String: Any]; p[field] = "NaN"; body["parameters"] = p
            } else {
                var r = body["regions"] as! [[String: Any]]; r[0][field] = "Inf"; body["regions"] = r
            }
            XCTAssertThrowsError(try decoder.decode(PlatformSweepThreshold.self, from: JSONSerialization.data(withJSONObject: body)))
        }
    }

    private func maximumFixture() -> [String: Any] {
        var body = fixture(window: 3, margin: 0)
        let count = 16_384
        let frequencies = (0..<count).map { 1_000_000_000_000 + $0 }
        let raw = (0..<count).map { $0 % 2 == 0 ? -100.1234567890123 : -60.2345678901234 }
        let baseline = (0..<count).map { $0 == 0 || $0 == count - 1 ? (raw[0] + raw[1]) / 2 : raw[($0 + 1) % count] }
        let excess = zip(raw, baseline).map(-)
        var sweep = body["sweep"] as! [String: Any]; sweep["levels"] = raw; body["sweep"] = sweep
        body["frequencies_hz"] = frequencies; body["baseline_levels"] = baseline
        body["threshold_levels"] = baseline; body["excess_levels"] = excess; body["points"] = count
        body["regions"] = stride(from: 1, to: count, by: 2).map { index -> [String: Any] in
            ["start_index": index, "end_index": index, "start_frequency_hz": frequencies[index], "end_frequency_hz": frequencies[index],
             "peak_index": index, "peak_frequency_hz": frequencies[index], "peak_excess_db": excess[index], "points": 1]
        }
        body["region_count"] = count / 2; body["exceedance_points"] = count / 2
        return body
    }

    func testMaximumAlternatingRegionDTOUsesAllBins() throws {
        let result = try decode(maximumFixture())
        XCTAssertEqual(result.points, 16_384)
        XCTAssertEqual(result.regions.count, 8192)
        XCTAssertEqual(result.regions.last?.peakIndex, 16_383)
    }

    func testOversizedThresholdResponseIsRefusedWithoutPartialResult() async throws {
        let body = maximumFixture()
        XCTAssertGreaterThan(try JSONSerialization.data(withJSONObject: body).count, 2_000_000)
        Stub.handler = { $0.respond(body) }
        do {
            _ = try await client().sweepThreshold(sessionID: "s", sweepID: "w", windowPoints: 3, marginDb: 0)
            XCTFail("The inherited 2 MB cap must refuse the entire response")
        } catch PlatformError.responseTooLarge {} catch { XCTFail("Unexpected \(error)") }
    }

    func testLiveSyntheticCoreResponseWhenExplicitlyConfigured() async throws {
        guard let url = ProcessInfo.processInfo.environment["ORI_THRESHOLD_SMOKE_BASE_URL"] else {
            throw XCTSkip("Optional read-only smoke requires an explicitly configured synthetic core server")
        }
        let api = try PlatformClient(baseURL: url)
        let result = try await api.sweepThreshold(sessionID: "threshold-one", sweepID: "one-sweep-2")
        XCTAssertEqual(result.streamId, "spectrum-one")
        XCTAssertEqual(result.regionCount, 2)
        XCTAssertEqual(result.regions.map(\.startIndex), [30, 69])
        XCTAssertEqual(result.regions.map(\.endIndex), [30, 71])
    }

    func testCancellationStopsTransport() async throws {
        let started = expectation(description: "Request started")
        let stopped = expectation(description: "Transport stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let api = try client()
        let task = Task { try await api.sweepThreshold(sessionID: "s", sweepID: "w") }
        await fulfillment(of: [started], timeout: 2)
        task.cancel()
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch is CancellationError {} catch { XCTFail("Unexpected \(error)") }
        await fulfillment(of: [stopped], timeout: 2)
    }
}
