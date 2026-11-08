import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class SweepAverageTests: XCTestCase {
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
                : ["error": ["code": "average_reference_incompatible", "message": "synthetic"]]
            respondBytes(try! JSONSerialization.data(withJSONObject: envelope), status: status)
        }
        func respondBytes(_ data: Data, status: Int = 200) {
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    private func client() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    private func interval(_ n: Int) -> [String: String] {
        ["started_at": "2026-11-08T01:00:0\(n).000001Z", "ended_at": "2026-11-08T01:00:0\(n).000002Z"]
    }
    private func settings(_ n: Int) -> [String: Any] {
        ["rbw_hz": 1000, "sequence": n, "extension": ["flag": false, "values": ["scan", "raw"]]]
    }
    private func raw(_ n: Int, points: Int = 3) -> [String: Any] {
        ["id": "w\(n)", "session_id": "s", "stream_id": "tiny", "interval": interval(n),
         "settings": settings(n), "frequencies_hz": Array(repeating: 100, count: points),
         "levels": n == 3 ? Array([Double]([-80, -100, -80]).prefix(points)) : Array(repeating: -90.0, count: points), "level_unit": "dBm"]
    }
    private func fixture(points: Int = 3) -> [String: Any] {
        let delta = Array([10.0, -10, 10].prefix(points))
        return ["session_id": "s", "stream_id": "tiny", "frequencies_hz": Array(repeating: 100, count: points),
                "reference": ["method": "mean_power", "count": 2, "source_ids": ["w1", "w2"],
                              "sources": (1...2).map { ["id": "w\($0)", "interval": interval($0), "settings": settings($0)] as [String: Any] },
                              "interval": ["started_at": interval(1)["started_at"]!, "ended_at": interval(2)["ended_at"]!],
                              "levels": Array(repeating: -90.0, count: points), "level_unit": "dBm"],
                "target": raw(3, points: points), "delta_levels": delta, "delta_unit": "dB", "points": points,
                "max_increase": ["frequency_hz": 100, "delta_db": delta.max()!],
                "min_change": ["frequency_hz": 100, "delta_db": delta.min()!]]
    }
    private func detectionFixture(points: Int = 3) -> [String: Any] {
        ["comparison": fixture(points: points),
         "detection": ["minimum_increase_db": 6.0, "minimum_bins": 2, "regions": [], "count": 0]]
    }
    private func mutate(_ original: [String: Any], path: [String], value: Any) -> [String: Any] {
        var result = original
        if path.count == 1 { result[path[0]] = value }
        else { result[path[0]] = mutate(result[path[0]] as! [String: Any], path: Array(path.dropFirst()), value: value) }
        return result
    }
    func testExactReadOnlyPOSTAndSingleDuplicateBins() async throws {
        let api = try client()
        for count in [1, 3] {
            Stub.handler = { stub in
                let request = try! bufferedTestRequest(stub.request)
                XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 5)
                XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s/sweep-average-comparison")
                XCTAssertNil(request.url?.query)
                let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                XCTAssertEqual(Set(body.keys), ["reference_ids", "target_id"])
                XCTAssertEqual(body["reference_ids"] as? [String], ["w1", "w2"]); XCTAssertEqual(body["target_id"] as? String, "w3")
                stub.respond(self.fixture(points: count))
            }
            let value = try await api.sweepAverageComparison(sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3")
            XCTAssertEqual(value.points, count)
            try value.validate(sources: [try decode(raw(1, points: count)), try decode(raw(2, points: count))], target: try decode(raw(3, points: count)))
        }
    }
    func testChangeDetectionPOSTAndParameterValidation() async throws {
        let api = try client()
        Stub.handler = { stub in
            let request = try! bufferedTestRequest(stub.request)
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s/sweep-average-change-detection")
            XCTAssertNil(request.url?.query)
            let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            XCTAssertEqual(Set(body.keys), ["reference_ids", "target_id", "minimum_increase_db", "minimum_bins"])
            XCTAssertEqual(body["reference_ids"] as? [String], ["w1", "w2"])
            XCTAssertEqual(body["target_id"] as? String, "w3")
            XCTAssertEqual(body["minimum_increase_db"] as? Double, 6)
            XCTAssertEqual(body["minimum_bins"] as? Int, 2)
            stub.respond(self.detectionFixture())
        }
        let value = try await api.sweepAverageChangeDetection(
            sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3",
            minimumIncreaseDb: 6, minimumBins: 2,
        )
        try value.validate(
            sources: [try decode(raw(1)), try decode(raw(2))], target: try decode(raw(3)),
            minimumIncreaseDb: 6, minimumBins: 2,
        )
        XCTAssertEqual(value.detection.count, 0)
        Stub.handler = { _ in XCTFail("Invalid detector parameters reached transport") }
        for (threshold, bins) in [(-1.0, 2), (121.0, 2), (.nan, 2), (6.0, 0), (6.0, 16_385)] {
            do {
                _ = try await api.sweepAverageChangeDetection(
                    sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3",
                    minimumIncreaseDb: threshold, minimumBins: bins,
                )
                XCTFail("Accepted invalid detector parameters")
            } catch PlatformError.invalidCommand {}
        }
    }
    func testExportArtifactRoundTripsAllAnalysisAndProvenance() throws {
        let value: PlatformSweepAverageChangeDetection = try decode(detectionFixture())
        let bytes = try PlatformSweepAverageExportArtifact(data: value).canonicalData()
        XCTAssertEqual(bytes.last, 0x0A)
        let object = try JSONSerialization.jsonObject(with: bytes) as! [String: Any]
        XCTAssertEqual(object["format"] as? String, "openradioinspector-sweep-average-change-detection")
        XCTAssertEqual(object["format_version"] as? Int, 1)
        let data = object["data"] as! [String: Any]
        let comparison = data["comparison"] as! [String: Any]
        XCTAssertEqual(comparison["delta_unit"] as? String, "dB")
        XCTAssertEqual(comparison["frequencies_hz"] as? [Int], [100, 100, 100])
        let reference = comparison["reference"] as! [String: Any]
        XCTAssertEqual(reference["source_ids"] as? [String], ["w1", "w2"])
        XCTAssertEqual((reference["sources"] as! [[String: Any]]).count, 2)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        XCTAssertEqual(
            try decoder.decode(PlatformSweepAverageChangeDetection.self,
                               from: JSONSerialization.data(withJSONObject: data)),
            value,
        )
    }
    func testInvalidRequestsDoNotReachTransport() async throws {
        Stub.handler = { _ in XCTFail("Invalid request reached network") }
        for (refs, target) in [(["w1"], "w3"), (Array(repeating: "w1", count: 17), "w3"),
                               (["w1", "w1"], "w3"), (["w1", "w2"], "w2"), ([" ", "w2"], "w3"),
                               ([String(repeating: "é", count: 129), "w2"], "w3"), (["w1", "w2"], "\n"),
                               ([String(repeating: "a\u{301}", count: 65), "w2"], "w3")] {
            do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: refs, targetID: target); XCTFail("Accepted invalid") }
            catch PlatformError.invalidCommand {} catch { XCTFail("Unexpected \(error)") }
        }
    }
    func testOnlyHTTP200AndStructuredErrors() async throws {
        for status in [201, 202, 204, 206, 299, 400, 404, 409, 413] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3"); XCTFail("Accepted \(status)") }
            catch PlatformError.http(let actual, _, _) { XCTAssertEqual(actual, status) }
            catch PlatformError.invalidResponse { XCTAssertLessThan(status, 300) }
        }
    }
    func testMalformedResponseFieldsAndProvenanceRejected() throws {
        let changes: [([String], Any)] = [(["reference", "method"], "mean_dbm"), (["reference", "count"], 3),
            (["reference", "source_ids"], ["w2", "w1"]), (["reference", "source_ids"], ["w1", "w1"]),
            (["reference", "level_unit"], "dB"), (["reference", "levels"], [-90]), (["reference", "sources"], []),
            (["target", "id"], "w1"), (["target", "level_unit"], "S"), (["target", "settings", "rbw_hz"], 1001),
            (["delta_unit"], "dBm"), (["delta_levels"], [10, -10, 9]), (["points"], 0),
            (["frequencies_hz"], [100, 99, 101]), (["frequencies_hz"], [100, 100, 9_007_199_254_740_992 as Int64]),
            (["max_increase", "delta_db"], 11), (["min_change", "frequency_hz"], 99),
            (["reference", "interval", "started_at"], "2026-11-08T01:00:01.000999Z"),
            (["target", "interval", "started_at"], "2026-11-08T01:00:03.000003Z"),
            (["target", "interval", "started_at"], "2026-11-08T03:00:03+02:00"),
            (["target", "interval", "ended_at"], "2026-11-08T01:00:03.0000001Z")]
        for (path, value) in changes {
            XCTAssertThrowsError(try decode(mutate(fixture(), path: path, value: value), as: PlatformSweepAverageComparison.self), path.joined(separator: "."))
        }
        var body = fixture(); var reference = body["reference"] as! [String: Any]
        var sources = reference["sources"] as! [[String: Any]]
        sources[0]["interval"] = ["started_at": "2026-11-08T01:00:01.000002Z", "ended_at": "2026-11-08T01:00:01.000001Z"]
        reference["sources"] = sources; body["reference"] = reference
        XCTAssertThrowsError(try decode(body, as: PlatformSweepAverageComparison.self))
    }
    func testFirstTieExtremaMustUseFirstFrequency() throws {
        var body = fixture(); body["frequencies_hz"] = [100, 200, 300]
        body["min_change"] = ["frequency_hz": 200, "delta_db": -10]
        XCTAssertNoThrow(try decode(body, as: PlatformSweepAverageComparison.self))
        body["max_increase"] = ["frequency_hz": 300, "delta_db": 10]
        XCTAssertThrowsError(try decode(body, as: PlatformSweepAverageComparison.self))
    }
    func testFullFrozenIdentityIncludingSequenceTypedSettingsLevelsAndBounds() throws {
        let value: PlatformSweepAverageComparison = try decode(fixture())
        let sources: [PlatformSweep] = [try decode(raw(1)), try decode(raw(2))]
        let target: PlatformSweep = try decode(raw(3))
        for (path, replacement) in [(["settings", "sequence"], 99 as Any), (["settings", "extension", "flag"], 0 as Any),
                                    (["levels"], [-89.0, -90, -90] as Any), (["frequencies_hz"], [100, 100, 101] as Any),
                                    (["interval", "ended_at"], "2026-11-08T01:00:03.000003Z" as Any)] {
            XCTAssertThrowsError(try value.validate(sources: sources, target: decode(mutate(raw(3), path: path, value: replacement))))
        }
        var changed = sources
        changed[0] = try decode(mutate(raw(1), path: ["settings", "sequence"], value: 99))
        XCTAssertThrowsError(try value.validate(sources: changed, target: target))
        let below: [PlatformSweep] = [try decode(mutate(raw(1), path: ["levels"], value: [-100, -100, -100])),
                                     try decode(mutate(raw(2), path: ["levels"], value: [-95, -95, -95]))]
        XCTAssertThrowsError(try value.validate(sources: below, target: target))
        XCTAssertEqual(PlatformSweepAverageComparison.sourceInterval(sources)?.startedAt, "2026-11-08T01:00:01.000001Z")
        // Legacy sweeps lacking settings remain decodable, but cannot become an averaged reference.
        var legacy = raw(1); legacy.removeValue(forKey: "settings")
        let old: PlatformSweep = try decode(legacy); XCTAssertNil(old.settings)
        XCTAssertFalse(PlatformSweepAverageComparison.validSources([old, sources[1]], target: target))
    }
    func testRequestOrderIdentityAndResponseByteBound() async throws {
        var body = fixture(); body["session_id"] = "other"
        Stub.handler = { $0.respond(body) }
        do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3"); XCTFail("Wrong session") }
        catch PlatformError.invalidResponse {}
        Stub.handler = { $0.respond(self.fixture()) }
        do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: ["w2", "w1"], targetID: "w3"); XCTFail("Wrong order") }
        catch PlatformError.invalidResponse {}
        Stub.handler = { $0.respondBytes(Data(repeating: 32, count: 2_000_001)) }
        do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3"); XCTFail("Oversized response") }
        catch PlatformError.responseTooLarge {}
    }
    func testTransportCancellation() async throws {
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let api = try client()
        let task = Task { try await api.sweepAverageComparison(sessionID: "s", referenceIDs: ["w1", "w2"], targetID: "w3") }
        await fulfillment(of: [started], timeout: 1); task.cancel()
        do { _ = try await task.value; XCTFail("Cancelled request returned") } catch is CancellationError {}
        await fulfillment(of: [stopped], timeout: 1)
    }
    func testLongScopeIDsAndMaximumCountRemainSupported() async throws {
        let session = String(repeating: "s", count: 200), stream = String(repeating: "t", count: 200)
        var body = fixture(); body["session_id"] = session; body["stream_id"] = stream
        var reference = body["reference"] as! [String: Any]
        let ids = (1...16).map { "r\($0)" }
        reference["count"] = 16; reference["source_ids"] = ids
        reference["sources"] = ids.map { ["id": $0, "interval": interval(1), "settings": settings(1)] as [String: Any] }
        reference["interval"] = interval(1); body["reference"] = reference
        Stub.handler = { $0.respond(body) }
        let result = try await client().sweepAverageComparison(sessionID: session, referenceIDs: ids, targetID: "w3")
        XCTAssertEqual(result.reference.count, 16); XCTAssertEqual(result.streamId, stream)
    }
    func testUTF8RequestLimitBeforeTransport() async throws {
        Stub.handler = { _ in XCTFail("Oversized UTF-8 request reached transport") }
        let ids = (1...16).map { String(repeating: "😀", count: 126) + String($0) }
        do { _ = try await client().sweepAverageComparison(sessionID: "s", referenceIDs: ids, targetID: String(repeating: "😀", count: 128)); XCTFail("Oversized UTF-8 request accepted") }
        catch PlatformError.invalidCommand {}
    }
    func testMicrosecondEnvelopeOrderAndStrictTinyBounds() throws {
        var first = raw(1), second = raw(2)
        first["interval"] = ["started_at": "2026-11-08T01:00:00.000002Z", "ended_at": "2026-11-08T01:00:00.000003Z"]
        second["interval"] = ["started_at": "2026-11-08T01:00:00.000001Z", "ended_at": "2026-11-08T01:00:00.000004Z"]
        let envelope = PlatformSweepAverageComparison.sourceInterval([try decode(first), try decode(second)])
        XCTAssertEqual(envelope?.startedAt, "2026-11-08T01:00:00.000001Z")
        XCTAssertEqual(envelope?.endedAt, "2026-11-08T01:00:00.000004Z")
        var body = fixture(), reference = fixture()["reference"] as! [String: Any], target = raw(3)
        reference["levels"] = [1e-13, 1e-13, 1e-13]; body["reference"] = reference
        target["levels"] = [1e-13, 1e-13, 1e-13]; body["target"] = target
        body["delta_levels"] = [0, 0, 0]; body["max_increase"] = ["frequency_hz": 100, "delta_db": 0]; body["min_change"] = ["frequency_hz": 100, "delta_db": 0]
        let value: PlatformSweepAverageComparison = try decode(body)
        first = raw(1); second = raw(2); first["levels"] = [-1e-100, -1e-100, -1e-100]; second["levels"] = [-1e-100, -1e-100, -1e-100]
        XCTAssertThrowsError(try value.validate(sources: [decode(first), decode(second)], target: decode(target)))
    }

    func testActualHTTPMeanPowerSmoke() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_AVERAGE_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_AVERAGE_SMOKE_BASE_URL to the synthetic average fixture") }
        let api = try PlatformClient(baseURL: base)
        let result = try await api.sweepAverageComparison(sessionID: "average-one", referenceIDs: ["one-sweep-1", "one-sweep-2", "one-sweep-3"], targetID: "one-sweep-4")
        XCTAssertEqual(result.reference.count, 3); XCTAssertEqual(result.points, 101)
        XCTAssertEqual(result.reference.levels[0], -94.31798275933005, accuracy: 1e-11)
        XCTAssertEqual(result.maxIncrease.frequencyHz, 103_000_000); XCTAssertEqual(result.maxIncrease.deltaDb, 20, accuracy: 1e-11)
        XCTAssertEqual(result.minChange.frequencyHz, 107_000_000); XCTAssertEqual(result.minChange.deltaDb, -10, accuracy: 1e-11)
        XCTAssertEqual(result.reference.sources[2].settings["sequence"], .number(3))
    }

    func testActualHTTPChangeDetectionSmoke() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_AVERAGE_SMOKE_BASE_URL"] else {
            throw XCTSkip("Set ORI_AVERAGE_SMOKE_BASE_URL to the synthetic average fixture")
        }
        let result = try await PlatformClient(baseURL: base).sweepAverageChangeDetection(
            sessionID: "average-one", referenceIDs: ["one-sweep-1", "one-sweep-2", "one-sweep-3"],
            targetID: "one-sweep-4", minimumIncreaseDb: 6, minimumBins: 1,
        )
        XCTAssertEqual(result.detection.count, 1)
        XCTAssertEqual(result.detection.regions[0].startIndex, 30)
        XCTAssertEqual(result.detection.regions[0].endIndex, 30)
        XCTAssertEqual(result.detection.regions[0].startFrequencyHz, 103_000_000)
        XCTAssertEqual(result.detection.regions[0].peakIncreaseDb, 20, accuracy: 1e-11)
    }
}
