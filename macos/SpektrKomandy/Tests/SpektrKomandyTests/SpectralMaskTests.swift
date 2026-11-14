import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Foundation
import XCTest
@testable import SpektrKomandy

final class SpectralMaskTests: XCTestCase {
    typealias Stub = SweepThresholdTests.Stub
    static func mask(name: String = "Mask") -> [String: Any] {
        ["format": "openradioinspector-spectral-mask", "format_version": 1, "name": name,
         "frequency_unit": "Hz", "level_unit": "dBm", "interpolation": "linear_dbm",
         "vertices": [["frequency_hz": 0, "limit_dbm": -80], ["frequency_hz": 1000, "limit_dbm": -80]]]
    }
    func text(_ object: Any) throws -> String { String(decoding: try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]), as: UTF8.self) }
    func fixture(session: String = "s", sweep: String = "w", name: String = "Mask") -> [String: Any] {
        ["session_id": session, "stream_id": "tiny",
         "sweep": ["id": sweep, "interval": ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"],
                   "levels": [-90, -80, -60, -60, -90], "level_unit": "dBm", "settings": ["rbw_hz": 1000]],
         "mask": Self.mask(name: name), "frequencies_hz": [0, 100, 100, 300, 1000],
         "threshold_levels": [-80, -80, -80, -80, -80], "excess_levels": [-10, 0, 20, 20, -10],
         "level_unit": "dBm", "delta_unit": "dB", "comparison": "strictly_greater", "points": 5,
         "exceedance_points": 2, "region_count": 1,
         "regions": [["start_index": 2, "end_index": 3, "start_frequency_hz": 100, "end_frequency_hz": 300,
                      "peak_index": 2, "peak_frequency_hz": 100, "peak_excess_db": 20, "points": 2]]]
    }
    func decode(_ object: [String: Any]) throws -> PlatformSweepMaskAnalysis {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweepMaskAnalysis.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func client() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1", session: URLSession(configuration: config))
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }

    func testRawBodyExactKeysEncodingAndNormalizedArtifactEcho() async throws {
        let raw = try text(Self.mask(name: "  e\u{301}  "))
        Stub.handler = { stub in
            let request = try! bufferedTestRequest(stub.request)
            XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 5)
            XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s%2Fa%3F/sweep-mask-analysis")
            XCTAssertNil(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.query)
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: String]
            XCTAssertEqual(body, ["sweep_id": "w &+/é", "mask_json": raw])
            stub.respond(self.fixture(session: "s/a?", sweep: "w &+/é", name: "é"))
        }
        let result = try await client().sweepMaskAnalysis(sessionID: "s/a?", sweepID: "w &+/é", maskJSON: raw)
        XCTAssertEqual(result.mask.name, "é"); XCTAssertEqual(result.excessLevels, [-10, 0, 20, 20, -10])
        XCTAssertEqual(result.regions.first?.peakIndex, 2)
        let saved = try result.mask.canonicalData()
        XCTAssertEqual(try PlatformSpectralMask.submitted(String(decoding: saved, as: UTF8.self)), result.mask)
    }

    func testDuplicateKeysAndMalformedDraftReachStrictCoreUnchanged() async throws {
        for raw in ["{\"name\":\"a\",\"name\":\"b\"}", "{not-json}", "[]"] {
            var count = 0
            Stub.handler = { stub in
                count += 1
                let body = try! JSONSerialization.jsonObject(with: bufferedTestRequest(stub.request).httpBody!) as! [String: String]
                XCTAssertEqual(body["mask_json"], raw)
                stub.respond(["code": "invalid_request", "message": "Invalid mask"], status: 400)
            }
            do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: raw); XCTFail() }
            catch let error as PlatformError { XCTAssertEqual(error, .http(status: 400, code: "invalid_request", message: "Invalid mask")) }
            XCTAssertEqual(count, 1)
        }
    }
    func testArtifactAndRequestByteCapsAvoidNetwork() async throws {
        Stub.handler = { _ in XCTFail("Should reject locally") }
        for (session, sweep, raw) in [("", "w", "{}"), ("s", " ", "{}"),
                ("s", "w", String(repeating: "é", count: 32769)),
                ("s", String(repeating: "a", count: 524288), "{}") ] {
            do { _ = try await client().sweepMaskAnalysis(sessionID: session, sweepID: sweep, maskJSON: raw); XCTFail() }
            catch PlatformError.invalidCommand {} catch { XCTFail("\(error)") }
        }
    }
    func testOnly200AndStructuredErrorsWithoutRetry() async throws {
        let raw = try text(Self.mask())
        for status in [201, 202, 204, 206, 299] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: raw); XCTFail() }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
        for (status, code) in [(400, "invalid_request"), (404, "session_not_found"), (404, "sweep_not_found"), (409, "mask_incompatible"), (413, "request_too_large")] {
            var count = 0
            Stub.handler = { count += 1; $0.respond(["code": code, "message": "Failure"], status: status) }
            do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: raw); XCTFail() }
            catch let error as PlatformError { XCTAssertEqual(error, .http(status: status, code: code, message: "Failure")) }
            XCTAssertEqual(count, 1)
        }
    }
    func testWrongIdentityOrCanonicalMaskRejected() async throws {
        let raw = try text(Self.mask())
        for field in ["session", "sweep", "mask", "contract"] {
            Stub.handler = { $0.respond(self.fixture(session: field == "session" ? "other" : "s",
                    sweep: field == "sweep" ? "other" : "w", name: field == "mask" ? "other" : "Mask"),
                    contract: field == "contract" ? "other" : "openradioinspector-platform") }
            do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: raw); XCTFail(field) }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
    }
    func testStrictArtifactsNamesTypesAndKeys() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["extra"] = 1 }, { $0.removeValue(forKey: "name") }, { $0["format_version"] = true },
            { $0["format"] = "other" }, { $0["level_unit"] = "dB" },
            { $0["frequency_unit"] = "MHz" }, { $0["interpolation"] = "linear_power" },
            { $0["name"] = "\tbad" }, { $0["name"] = " \u{200B}" }, { $0["name"] = "\u{E000}" },
            { $0["name"] = String(repeating: "a", count: 81) }, { $0["name"] = " " },
            { $0["vertices"] = [["frequency_hz": 0, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": 0, "limit_dbm": -80], ["frequency_hz": 0, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": -1, "limit_dbm": -80], ["frequency_hz": 1000, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": 0, "limit_dbm": -80], ["frequency_hz": 9007199254740992 as Int64, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": false, "limit_dbm": -80], ["frequency_hz": 1000, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": 0, "limit_dbm": false], ["frequency_hz": 1000, "limit_dbm": -80]] },
            { $0["vertices"] = [["frequency_hz": 0, "limit_dbm": -80, "extra": 1], ["frequency_hz": 1000, "limit_dbm": -80]] },
            { $0["vertices"] = (0..<129).map { ["frequency_hz": $0, "limit_dbm": -80] } }
        ]
        for (index, mutation) in mutations.enumerated() {
            var object = Self.mask(); mutation(&object)
            XCTAssertThrowsError(try PlatformSpectralMask.submitted(text(object)), "mutation \(index)")
        }
        let raw = try text(Self.mask())
        for invalid in [raw.replacingOccurrences(of: "\"format_version\":1", with: "\"format_version\":1.0"),
                raw.replacingOccurrences(of: "\"frequency_hz\":0", with: "\"frequency_hz\":0.0"),
                raw.replacingOccurrences(of: "\"name\":\"Mask\"", with: "\"name\":\"Mask\",\"name\":\"Mask\"")] {
            XCTAssertThrowsError(try PlatformSpectralMask.submitted(invalid))
        }
    }
    func testMalformedDTOUnitsProvenanceArraysCoverageAndRegions() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["points"] = 0 }, { $0["points"] = 16_385 }, { $0["points"] = true },
            { $0["level_unit"] = "dB" }, { $0["delta_unit"] = "dBm" }, { $0["comparison"] = "greater_or_equal" },
            { $0["frequencies_hz"] = [-1, 100, 100, 300, 1000] },
            { $0["frequencies_hz"] = [0, 100, 100, 300, 1001] },
            { $0["frequencies_hz"] = [0, 100, 99, 300, 1000] },
            { $0["frequencies_hz"] = [0, 100, 100, 300, 9007199254740992 as Int64] },
            { $0["threshold_levels"] = [-80, -80] }, { $0["threshold_levels"] = [-80, -80, -81, -80, -80] },
            { $0["excess_levels"] = [-10, 0, 19, 20, -10] }, { $0["exceedance_points"] = 3 },
            { $0["regions"] = []; $0["region_count"] = 0 }, { $0["region_count"] = 2 },
            { var s = $0["sweep"] as! [String: Any]; s["settings"] = ["sequence": 1]; $0["sweep"] = s },
            { var s = $0["sweep"] as! [String: Any]; s["settings"] = ["rbw_hz": NSNull()]; $0["sweep"] = s },
            { var s = $0["sweep"] as! [String: Any]; s["level_unit"] = "s_meter_count"; $0["sweep"] = s }
        ]
        for (index, mutation) in mutations.enumerated() {
            var body = fixture(); mutation(&body); XCTAssertThrowsError(try decode(body), "mutation \(index)")
        }
        for (key, value) in [("start_index", -1 as Any), ("end_index", 2), ("peak_index", 3),
                ("peak_frequency_hz", 300), ("peak_excess_db", 21), ("points", 1)] {
            var body = fixture(); var r = body["regions"] as! [[String: Any]]; r[0][key] = value; body["regions"] = r
            XCTAssertThrowsError(try decode(body), key)
        }
    }
    func testSingletonAndLinearInterpolation() throws {
        var body = fixture(); var sweep = body["sweep"] as! [String: Any]
        sweep["levels"] = [-80]; body["sweep"] = sweep; body["frequencies_hz"] = [500]
        body["threshold_levels"] = [-80]; body["excess_levels"] = [0]
        body["points"] = 1; body["regions"] = []; body["region_count"] = 0; body["exceedance_points"] = 0
        var mask = Self.mask(); mask["vertices"] = [["frequency_hz": 0, "limit_dbm": -100], ["frequency_hz": 1000, "limit_dbm": -60]]
        body["mask"] = mask
        XCTAssertEqual(try decode(body).points, 1)
        body["threshold_levels"] = [-79]; body["excess_levels"] = [-1]
        XCTAssertThrowsError(try decode(body))
    }
    func testMaximumBinArrayAndAllAlternatingRegions() throws {
        let count = 16_384
        var body = fixture(); var sweep = body["sweep"] as! [String: Any]
        sweep["levels"] = (0..<count).map { $0 % 2 == 0 ? -90 : -60 }; body["sweep"] = sweep
        body["frequencies_hz"] = (0..<count).map { $0 / 20 }
        body["threshold_levels"] = Array(repeating: -80, count: count)
        body["excess_levels"] = (0..<count).map { $0 % 2 == 0 ? -10 : 20 }
        body["points"] = count; body["region_count"] = count / 2; body["exceedance_points"] = count / 2
        body["regions"] = stride(from: 1, to: count, by: 2).map { index in
            ["start_index": index, "end_index": index, "start_frequency_hz": index / 20, "end_frequency_hz": index / 20,
             "peak_index": index, "peak_frequency_hz": index / 20, "peak_excess_db": 20, "points": 1]
        }
        let result = try decode(body)
        XCTAssertEqual(result.points, count); XCTAssertEqual(result.regions.count, 8192)
        XCTAssertEqual(result.regions.last?.peakIndex, 16_383)
    }
    func testRawResponseDuplicateKeysAndNonIntegerTokensRejected() async throws {
        let payload = try text(["contract": "openradioinspector-platform", "contract_version": 1, "data": fixture()])
        let invalid = [
            payload.replacingOccurrences(of: "\"name\":\"Mask\"", with: "\"name\":\"Mask\",\"name\":\"Mask\""),
            payload.replacingOccurrences(of: "\"format_version\":1", with: "\"format_version\":1.0"),
            payload.replacingOccurrences(of: "\"frequency_hz\":0", with: "\"frequency_hz\":0.0"),
            payload.replacingOccurrences(of: "\"points\":5", with: "\"points\":5.0"),
            payload.replacingOccurrences(of: "\"peak_index\":2", with: "\"peak_index\":2e0")
        ]
        for raw in invalid {
            XCTAssertNotEqual(raw, payload)
            Stub.handler = { stub in
                stub.client?.urlProtocol(stub, didReceive: HTTPURLResponse(url: stub.request.url!, statusCode: 200,
                    httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
                stub.client?.urlProtocol(stub, didLoad: Data(raw.utf8)); stub.client?.urlProtocolDidFinishLoading(stub)
            }
            do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: text(Self.mask())); XCTFail() }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
    }
    func testInheritedResponseLimitRemainsTwoMillionBytes() async throws {
        var body = fixture(); body["padding"] = String(repeating: "a", count: 2_000_001)
        Stub.handler = { $0.respond(body) }
        do { _ = try await client().sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: text(Self.mask())); XCTFail() }
        catch PlatformError.responseTooLarge {} catch { XCTFail("\(error)") }
    }
    func testCancellationStopsReadOnlyPost() async throws {
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let api = try client(), raw = try text(Self.mask())
        let task = Task { try await api.sweepMaskAnalysis(sessionID: "s", sweepID: "w", maskJSON: raw) }
        await fulfillment(of: [started], timeout: 2); task.cancel()
        do { _ = try await task.value; XCTFail() } catch is CancellationError {} catch { XCTFail("\(error)") }
        await fulfillment(of: [stopped], timeout: 2)
    }
    func testLiveSyntheticCoreResponseWhenExplicitlyConfigured() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_MASK_SMOKE_BASE_URL"] else { throw XCTSkip("Synthetic core not configured") }
        var root = URL(fileURLWithPath: #filePath); for _ in 0..<5 { root.deleteLastPathComponent() }
        let raw = try String(contentsOf: root.appendingPathComponent("docs/examples/spectral-mask.json"), encoding: .utf8)
        let result = try await PlatformClient(baseURL: base).sweepMaskAnalysis(sessionID: "mask-one", sweepID: "one-sweep-2", maskJSON: raw)
        XCTAssertEqual(result.streamId, "spectrum-one"); XCTAssertEqual(result.regions.map(\.startIndex), [30, 69])
        XCTAssertEqual(result.regions.map(\.endIndex), [30, 71]); XCTAssertEqual(result.exceedancePoints, 4)
    }
}
