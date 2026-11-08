import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class ThreeSweepThresholdsTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: Any, status: Int = 200, code: String = "three_thresholds_incompatible") {
            let envelope: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": code, "message": "synthetic"]]
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
    private let defaults: [Double] = [-90, -80, -70]
    private func raw(_ levels: [Double] = [-90, -70, -70, -100, -60, -80]) -> [String: Any] {
        ["id": "w", "session_id": "s", "stream_id": "tiny", "interval": ["started_at": "2026-11-08T01:00:00.000001Z", "ended_at": "2026-11-08T01:00:00.000002+00:00"],
         "settings": ["rbw_hz": 1000, "sequence": 2, "extra": ["flag": false, "values": [1, 2]]],
         "frequencies_hz": levels.indices.map { 100 + $0 / 2 }, "levels": levels, "level_unit": "dBm"]
    }
    private func fixture(levels: [Double] = [-90, -80, -70], samples: [Double] = [-90, -70, -70, -100, -60, -80]) -> [String: Any] {
        let source = raw(samples), hz = source["frequencies_hz"] as! [Int]
        let lines: [[String: Any]] = levels.enumerated().map { number, threshold in
            let excess = samples.map { $0 - threshold }
            var cursor = 0, regions: [[String: Any]] = []
            while cursor < samples.count {
                if excess[cursor] <= 0 { cursor += 1; continue }
                let first = cursor; var peak = cursor
                while cursor < samples.count && excess[cursor] > 0 {
                    if excess[cursor] > excess[peak] { peak = cursor }; cursor += 1
                }
                regions.append(["start_index": first, "end_index": cursor - 1, "start_frequency_hz": hz[first], "end_frequency_hz": hz[cursor - 1],
                                "peak_index": peak, "peak_frequency_hz": hz[peak], "peak_excess_db": excess[peak], "points": cursor - first])
            }
            return ["id": "line-\(number + 1)", "level_dbm": threshold, "excess_levels": excess, "regions": regions,
                    "exceedance_points": excess.filter { $0 > 0 }.count, "region_count": regions.count]
        }
        return ["session_id": "s", "stream_id": "tiny", "sweep": source, "frequencies_hz": hz,
                "method": "three_horizontal", "comparison": "strictly_greater", "level_unit": "dBm", "delta_unit": "dB", "points": samples.count, "lines": lines]
    }
    private func mutate(_ original: [String: Any], path: [String], value: Any) -> [String: Any] {
        var result = original
        if path.count == 1 { result[path[0]] = value }
        else { result[path[0]] = mutate(result[path[0]] as! [String: Any], path: Array(path.dropFirst()), value: value) }
        return result
    }
    func testExactReadOnlyPOSTUnsortedEqualSingleAndFullBins() async throws {
        for (levels, samples) in [([-70.0, -90, -80], [-90.0, -70, -70, -100, -60, -80]),
                                  ([-80.0, -80, -80], [-80.0]),
                                  ([-90.0, -80, -70], Array(repeating: -100.0, count: 16_384))] {
            Stub.handler = { stub in
                let request = try! bufferedTestRequest(stub.request)
                XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 5)
                XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s/sweep-three-thresholds")
                XCTAssertNil(request.url?.query)
                let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                XCTAssertEqual(Set(body.keys), ["sweep_id", "levels_dbm"])
                XCTAssertEqual(body["sweep_id"] as? String, "w"); XCTAssertEqual(body["levels_dbm"] as? [Double], levels)
                XCTAssertLessThanOrEqual(request.httpBody!.count, 8192)
                stub.respond(self.fixture(levels: levels, samples: samples))
            }
            let result = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: levels)
            XCTAssertEqual(result.points, samples.count); XCTAssertEqual(result.lines.map(\.id), ["line-1", "line-2", "line-3"])
            XCTAssertEqual(result.sweep.levels, samples)
            try result.validate(source: decode(raw(samples)), levelsDbm: levels)
        }
    }
    func testInvalidRequestsNeverReachTransport() async throws {
        Stub.handler = { _ in XCTFail("Invalid request reached transport") }
        for levels in [[], [-80], [-80, -70], [-90, -80, -70, -60], [.infinity, 0, 1], [.nan, 0, 1]] as [[Double]] {
            do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: levels); XCTFail("Accepted invalid levels") }
            catch PlatformError.invalidCommand {}
        }
        for sweep in ["", "\n", String(repeating: "a", count: 129), String(repeating: "a\u{301}", count: 65)] {
            do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: sweep, levelsDbm: defaults); XCTFail("Accepted invalid ID") }
            catch PlatformError.invalidCommand {}
        }
        do { _ = try await api().threeSweepThresholds(sessionID: "\n", sweepID: "w", levelsDbm: defaults); XCTFail("Accepted blank scope") }
        catch PlatformError.invalidCommand {}
    }
    func testLongScopeAndUnicodeSweepIDs() async throws {
        let session = String(repeating: "s", count: 200), stream = String(repeating: "t", count: 200), id = String(repeating: "😀", count: 128)
        var body = fixture(); body["session_id"] = session; body["stream_id"] = stream
        var sweep = body["sweep"] as! [String: Any]; sweep["id"] = id; body["sweep"] = sweep
        Stub.handler = { $0.respond(body) }
        let result = try await api().threeSweepThresholds(sessionID: session, sweepID: id, levelsDbm: defaults)
        XCTAssertEqual(result.streamId, stream); XCTAssertEqual(result.sweep.id, id)
    }
    func testOnlyHTTP200AndStructuredErrors() async throws {
        for status in [201, 202, 204, 206, 299, 400, 404, 409, 413] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: defaults); XCTFail("Accepted \(status)") }
            catch PlatformError.http(let actual, let code, _) { XCTAssertEqual(actual, status); XCTAssertEqual(code, "three_thresholds_incompatible") }
            catch PlatformError.invalidResponse { XCTAssertLessThan(status, 300) }
        }
    }
    func testInvalidMethodUnitsCountsSourceAndFrequencies() throws {
        let changes: [([String], Any)] = [(["session_id"], " "), (["stream_id"], ""), (["method"], "rolling_median"),
            (["comparison"], "greater_or_equal"), (["level_unit"], "dB"), (["delta_unit"], "dBm"),
            (["points"], 0), (["points"], 16_385), (["points"], true), (["sweep", "id"], String(repeating: "x", count: 129)),
            (["sweep", "level_unit"], "S"), (["sweep", "levels"], [-80]), (["sweep", "settings"], ["sequence": 1]),
            (["sweep", "settings", "rbw_hz"], NSNull()), (["frequencies_hz"], [100, 100, 101, 101, 102, 101]),
            (["frequencies_hz"], [-1, 0, 1, 2, 3, 4]), (["frequencies_hz"], [0, 1, 2, 3, 4, 9_007_199_254_740_992 as Int64]),
            (["frequencies_hz"], [0, 1, 2, 3, 4, 4.5]), (["lines"], [])]
        for (path, value) in changes {
            XCTAssertThrowsError(try decode(mutate(fixture(), path: path, value: value), as: PlatformThreeSweepThresholds.self), path.joined(separator: "."))
        }
        var legacy = raw(); legacy.removeValue(forKey: "settings")
        let old: PlatformSweep = try decode(legacy); XCTAssertNil(old.settings)
        XCTAssertFalse(PlatformThreeSweepThresholds.validSource(old))
    }
    func testStrictMicrosecondUTCValidationAndCalendarRoundTrip() throws {
        for value in ["2026-11-08T01:00:00.000003Z", "2026-11-08T01:00:00.0000001Z", "2026-11-08T01:00:00.Z",
                      "2026-11-08T03:00:00+02:00", "2026-11-08T01:00:00", "2026-02-30T01:00:00Z", "2026-11-08t01:00:00z"] {
            XCTAssertThrowsError(try decode(mutate(fixture(), path: ["sweep", "interval", "started_at"], value: value), as: PlatformThreeSweepThresholds.self), value)
        }
        for fraction in ["", ".0", ".00000", ".000001"] {
            XCTAssertNoThrow(try decode(mutate(fixture(), path: ["sweep", "interval", "started_at"], value: "2026-11-08T01:00:00\(fraction)Z"), as: PlatformThreeSweepThresholds.self))
        }
    }
    func testEveryLineIdentityArithmeticRegionAndFirstTieValidated() throws {
        for index in 0..<3 {
            let original = fixture()
            let lines = original["lines"] as! [[String: Any]]
            for (key, value) in [("id", "line-9" as Any), ("level_dbm", -91 as Any), ("excess_levels", [0] as Any),
                                 ("exceedance_points", -1 as Any), ("region_count", 0 as Any), ("regions", [] as Any)] {
                var body = original, changed = lines; changed[index][key] = value; body["lines"] = changed
                XCTAssertThrowsError(try decode(body, as: PlatformThreeSweepThresholds.self), "\(index) \(key)")
            }
            var body = original, swapped = lines; swapped.swapAt(0, 1); body["lines"] = swapped
            XCTAssertThrowsError(try decode(body, as: PlatformThreeSweepThresholds.self))
        }
        let original = fixture()
        for (key, value) in [("start_index", -1 as Any), ("end_index", 5 as Any), ("points", 1 as Any),
                             ("start_frequency_hz", 99 as Any), ("end_frequency_hz", 99 as Any),
                             ("peak_index", 2 as Any), ("peak_frequency_hz", 101 as Any), ("peak_excess_db", 21 as Any)] {
            var body = original, lines = body["lines"] as! [[String: Any]], regions = lines[0]["regions"] as! [[String: Any]]
            regions[0][key] = value; lines[0]["regions"] = regions; body["lines"] = lines
            XCTAssertThrowsError(try decode(body, as: PlatformThreeSweepThresholds.self), key)
        }
        let equal: PlatformThreeSweepThresholds = try decode(fixture(levels: [-80, -80, -80], samples: [-80]))
        XCTAssertEqual(equal.lines.map(\.exceedancePoints), [0, 0, 0]); XCTAssertTrue(equal.lines.allSatisfy { $0.regions.isEmpty })
        var overflow = fixture(levels: [0, 0, 0], samples: [-Double.greatestFiniteMagnitude])
        var lines = overflow["lines"] as! [[String: Any]]; lines[0]["level_dbm"] = Double.greatestFiniteMagnitude; overflow["lines"] = lines
        XCTAssertThrowsError(try decode(overflow, as: PlatformThreeSweepThresholds.self))
    }
    func testFullFrozenProvenanceIncludingSequenceAndTypedSettings() throws {
        let result: PlatformThreeSweepThresholds = try decode(fixture())
        for (path, value) in [(["id"], "other" as Any), (["session_id"], "other" as Any), (["stream_id"], "other" as Any),
                              (["settings", "sequence"], 99 as Any), (["settings", "extra", "flag"], 0 as Any),
                              (["interval", "ended_at"], "2026-11-08T01:00:00.000003Z" as Any),
                              (["levels"], [-90, -70, -70, -100, -61, -80] as Any), (["frequencies_hz"], [0, 1, 2, 3, 4, 5] as Any)] {
            XCTAssertThrowsError(try result.validate(source: decode(mutate(raw(), path: path, value: value)), levelsDbm: defaults), path.joined(separator: "."))
        }
        XCTAssertThrowsError(try result.validate(source: decode(raw()), levelsDbm: [-70, -80, -90]))
        XCTAssertEqual(result.sweep.settings["sequence"], .number(2))
    }
    func testResponseIdentityAndByteBoundBeforeDecode() async throws {
        for body in [mutate(fixture(), path: ["session_id"], value: "other"), mutate(fixture(), path: ["sweep", "id"], value: "other"), fixture(levels: [-80, -90, -70])] {
            Stub.handler = { $0.respond(body) }
            do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: defaults); XCTFail("Wrong identity") }
            catch PlatformError.invalidResponse {}
        }
        Stub.handler = { $0.bytes(Data(repeating: 32, count: 2_000_001)) }
        do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: defaults); XCTFail("Oversized response") }
        catch PlatformError.responseTooLarge {}
    }
    func testUnicodeIdentityAndNestedSettingsAreByteExact() async throws {
        let composed = "é", decomposed = "e\u{301}"
        XCTAssertEqual(composed, decomposed)
        XCTAssertFalse(PlatformThreeSweepThresholds.sameID(composed, decomposed))
        for field in ["session_id", "stream_id", "id"] {
            var source = raw(), response = fixture()
            if field == "id" { response = mutate(response, path: ["sweep", "id"], value: composed) }
            else { response[field] = composed }
            source[field] = decomposed
            let value: PlatformThreeSweepThresholds = try decode(response)
            XCTAssertThrowsError(try value.validate(source: decode(source), levelsDbm: defaults), field)
            if field != "stream_id" {
                Stub.handler = { $0.respond(response) }
                do {
                    _ = try await api().threeSweepThresholds(sessionID: field == "session_id" ? decomposed : "s", sweepID: field == "id" ? decomposed : "w", levelsDbm: defaults)
                    XCTFail("Canonical Unicode identity substituted")
                } catch PlatformError.invalidResponse {}
            }
        }
        for mode in ["string", "array", "key", "nestedKey"] {
            func settings(_ text: String) -> [String: Any] {
                switch mode {
                case "string": return ["label": text, "sequence": 1]
                case "array": return ["extension": [false, ["name": text]], "sequence": 1]
                case "key": return [text: "same", "sequence": 1]
                default: return ["extension": [text: "same"], "sequence": 1]
                }
            }
            var source = raw(); source["settings"] = settings(decomposed)
            let response = mutate(fixture(), path: ["sweep", "settings"], value: settings(composed))
            let value: PlatformThreeSweepThresholds = try decode(response)
            XCTAssertThrowsError(try value.validate(source: decode(source), levelsDbm: defaults), mode)
            source["settings"] = settings(composed)
            XCTAssertNoThrow(try value.validate(source: decode(source), levelsDbm: defaults), mode)
        }
    }
    func testActualTransportCancellation() async throws {
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let client = try api(), levels = defaults
        let task = Task { try await client.threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: levels) }
        await fulfillment(of: [started], timeout: 1); task.cancel()
        do { _ = try await task.value; XCTFail("Cancelled returned") } catch is CancellationError {}
        await fulfillment(of: [stopped], timeout: 1)
    }
    func testRawCanonicalKeyCollisionsAndDuplicateFieldsFailBeforeDTO() async throws {
        let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": fixture()]
        let raw = String(data: try JSONSerialization.data(withJSONObject: envelope), encoding: .utf8)!
        for key in ["settings", "extra"] {
            // Both keys exist in the raw JSON, but a Swift Dictionary would collapse them.
            let ambiguous = raw.replacingOccurrences(of: "\"\(key)\":{", with: "\"\(key)\":{\"é\":1,\"e\\u0301\":2,")
            XCTAssertNotEqual(ambiguous.utf8.count, raw.utf8.count)
            Stub.handler = { $0.bytes(Data(ambiguous.utf8)) }
            do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: defaults); XCTFail("Lossy settings decoded") }
            catch PlatformError.invalidResponse {}
        }
        let duplicate = raw.replacingOccurrences(of: "\"settings\":{", with: "\"settings\":{\"rbw_hz\":99,")
        Stub.handler = { $0.bytes(Data(duplicate.utf8)) }
        do { _ = try await api().threeSweepThresholds(sessionID: "s", sweepID: "w", levelsDbm: defaults); XCTFail("Duplicate key decoded") }
        catch PlatformError.invalidResponse {}
    }
    func testActualHTTPThreeThresholdSmoke() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_THREE_THRESHOLDS_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_THREE_THRESHOLDS_SMOKE_BASE_URL to synthetic fixture") }
        let result = try await PlatformClient(baseURL: base).threeSweepThresholds(sessionID: "three-one", sweepID: "one-sweep-2", levelsDbm: defaults)
        XCTAssertEqual(result.points, 101); XCTAssertEqual(result.streamId, "spectrum-one")
        XCTAssertEqual(result.lines.map(\.exceedancePoints), [4, 2, 1]); XCTAssertEqual(result.lines.map(\.regionCount), [2, 2, 1])
        XCTAssertEqual(result.lines.map { $0.regions[0].peakExcessDb }, [25, 15, 5])
        XCTAssertTrue(result.lines.allSatisfy { $0.regions[0].peakIndex == 30 && $0.regions[0].peakFrequencyHz == 103_000_000 })
        XCTAssertEqual(result.sweep.settings["sequence"], .number(2))
    }
}
