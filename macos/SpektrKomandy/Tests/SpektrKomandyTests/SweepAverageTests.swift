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
                "target": raw(3, points: points).filter { ["id", "interval", "settings", "levels", "level_unit"].contains($0.key) },
                "delta_levels": delta, "delta_unit": "dB", "points": points,
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
    private func region(_ start: Int, _ end: Int, peak: Int, increase: Double, frequencies: [Int64]) -> [String: Any] {
        ["start_index": start, "end_index": end, "start_frequency_hz": frequencies[start],
         "end_frequency_hz": frequencies[end], "peak_index": peak, "peak_frequency_hz": frequencies[peak],
         "peak_increase_db": increase, "bin_count": end - start + 1]
    }
    private func detectionFixture(deltas: [Double], threshold: Double, bins: Int, regions: [[String: Any]]) -> [String: Any] {
        var comparison = fixture(), reference = fixture()["reference"] as! [String: Any], target = fixture()["target"] as! [String: Any]
        let frequencies = deltas.indices.map { Int64(100 + $0 / 2) }
        comparison["points"] = deltas.count; comparison["frequencies_hz"] = frequencies
        reference["levels"] = Array(repeating: -90.0, count: deltas.count); comparison["reference"] = reference
        target["levels"] = deltas.map { -90 + $0 }; comparison["target"] = target; comparison["delta_levels"] = deltas
        comparison["max_increase"] = ["frequency_hz": frequencies[deltas.firstIndex(of: deltas.max()!)!], "delta_db": deltas.max()!]
        comparison["min_change"] = ["frequency_hz": frequencies[deltas.firstIndex(of: deltas.min()!)!], "delta_db": deltas.min()!]
        return ["comparison": comparison, "detection": ["minimum_increase_db": threshold, "minimum_bins": bins, "regions": regions, "count": regions.count]]
    }
    private func detect(_ api: PlatformClient? = nil, refs: [String] = ["w1", "w2"], target: String = "w3", threshold: Double = 6, bins: Int = 2) async throws -> PlatformSweepAverageChangeDetection {
        try await (api ?? client()).sweepAverageChangeDetection(sessionID: "s", referenceIDs: refs, targetID: target, minimumIncreaseDb: threshold, minimumBins: bins)
    }

    func testDetectionMaximalRegionsEqualityFilteringAndFirstPeakTies() throws {
        let deltas: [Double] = [6, 12, 12, 6, -1, 20, -10, 7, 9]
        let frequencies = deltas.indices.map { Int64(100 + $0 / 2) }
        let first = region(0, 3, peak: 1, increase: 12, frequencies: frequencies)
        let last = region(7, 8, peak: 8, increase: 9, frequencies: frequencies)
        let good = detectionFixture(deltas: deltas, threshold: 6, bins: 2, regions: [first, last])
        let value: PlatformSweepAverageChangeDetection = try decode(good)
        XCTAssertEqual(value.detection.regions.map(\.startIndex), [0, 7])
        var changedFirst = first; changedFirst["peak_index"] = 2; changedFirst["peak_frequency_hz"] = frequencies[2]
        let badLists: [[[String: Any]]] = [[], [first], [last], [last, first], [first, first, last], [changedFirst, last],
            [region(0, 1, peak: 1, increase: 12, frequencies: frequencies), region(2, 3, peak: 2, increase: 12, frequencies: frequencies), last],
            [region(0, 8, peak: 5, increase: 20, frequencies: frequencies)],
            [first, region(5, 5, peak: 5, increase: 20, frequencies: frequencies), last]]
        for regions in badLists {
            var invalid = good; invalid["detection"] = ["minimum_increase_db": 6, "minimum_bins": 2, "regions": regions, "count": regions.count]
            XCTAssertThrowsError(try decode(invalid, as: PlatformSweepAverageChangeDetection.self))
        }
        for (field, replacement) in [("start_index", -1 as Any), ("end_index", Int.max as Any), ("bin_count", 3 as Any),
                                      ("start_frequency_hz", 99 as Any), ("peak_increase_db", 11 as Any)] {
            var changed = first; changed[field] = replacement
            XCTAssertThrowsError(try decode(detectionFixture(deltas: deltas, threshold: 6, bins: 2, regions: [changed, last]), as: PlatformSweepAverageChangeDetection.self))
        }
        XCTAssertThrowsError(try decode(mutate(good, path: ["detection", "count"], value: 1), as: PlatformSweepAverageChangeDetection.self))
        XCTAssertNoThrow(try decode(detectionFixture(deltas: [0], threshold: 0, bins: 1,
            regions: [region(0, 0, peak: 0, increase: 0, frequencies: [100])]), as: PlatformSweepAverageChangeDetection.self))
        XCTAssertNoThrow(try decode(detectionFixture(deltas: [120], threshold: 120, bins: 1,
            regions: [region(0, 0, peak: 0, increase: 120, frequencies: [100])]), as: PlatformSweepAverageChangeDetection.self))
        XCTAssertNoThrow(try decode(detectionFixture(deltas: [6, 6], threshold: 6, bins: 16_384, regions: []), as: PlatformSweepAverageChangeDetection.self))
        let many = Array(repeating: 6.0, count: 16_384), grid = (0..<16_384).map { Int64(100 + $0 / 2) }
        XCTAssertNoThrow(try decode(detectionFixture(deltas: many, threshold: 6, bins: 16_384,
            regions: [region(0, 16_383, peak: 0, increase: 6, frequencies: grid)]), as: PlatformSweepAverageChangeDetection.self))
        XCTAssertThrowsError(try decode(detectionFixture(deltas: many + [6], threshold: 6, bins: 16_384, regions: []), as: PlatformSweepAverageChangeDetection.self))
    }

    func testDetectorTransportRejectsWrongParametersIdentityAndMalformedJSONTypes() async throws {
        let api = try client()
        for (path, value) in [(["detection", "minimum_increase_db"], 7 as Any), (["detection", "minimum_bins"], 3 as Any),
                               (["comparison", "session_id"], "other" as Any), (["comparison", "target", "id"], "other" as Any)] {
            Stub.handler = { $0.respond(self.mutate(self.detectionFixture(), path: path, value: value)) }
            do { _ = try await detect(api); XCTFail("Accepted mismatched \(path)") } catch PlatformError.invalidResponse {}
        }
        let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": detectionFixture()]
        let good = String(data: try JSONSerialization.data(withJSONObject: envelope, options: [.sortedKeys]), encoding: .utf8)!
        for (old, new) in [("\"minimum_bins\":2", "\"minimum_bins\":2.5"),
                           ("\"minimum_bins\":2", "\"minimum_bins\":2.0"),
                           ("\"minimum_bins\":2", "\"minimum_bins\":2e0"),
                           ("\"minimum_bins\":2", "\"minimum_bins\":true"),
                           ("\"minimum_bins\":2", "\"minimum_bins\":\"2\""),
                           ("\"minimum_bins\":2", "\"minimumBins\":2"),
                           ("\"minimum_bins\":2", "\"minimum_bins\":2,\"minimum_bins\":2"),
                           ("\"minimum_increase_db\":6", "\"minimum_increase_db\":1e999"),
                           ("\"minimum_increase_db\":6", "\"minimum_increase_db\":true"),
                           ("\"points\":3", "\"points\":true"),
                           ("\"frequency_hz\":100", "\"frequency_hz\":100.5"),
                           ("\"regions\":[]", "\"regions\":[null]") ] {
            XCTAssertTrue(good.contains(old), old)
            Stub.handler = { $0.respondBytes(Data(good.replacingOccurrences(of: old, with: new).utf8)) }
            do { _ = try await detect(api); XCTFail("Accepted malformed \(new)") } catch PlatformError.invalidResponse {}
        }
        Stub.handler = { $0.respondBytes(Data(repeating: 32, count: 2_000_001)) }
        do { _ = try await detect(api); XCTFail("Accepted oversized response") } catch PlatformError.responseTooLarge {}
        for status in [201, 202, 204, 206, 299, 400, 404, 409, 413] {
            Stub.handler = { $0.respond(self.detectionFixture(), status: status) }
            do { _ = try await detect(api); XCTFail("Accepted status \(status)") }
            catch PlatformError.http(let actual, _, _) { XCTAssertEqual(actual, status) }
            catch PlatformError.invalidResponse { XCTAssertLessThan(status, 300) }
        }
    }

    func testDetectorExactUnicodeIdentityAndProvenance() async throws {
        let composed = "ré", decomposed = "re\u{301}"
        var body = detectionFixture(), comparison = fixture(), reference = fixture()["reference"] as! [String: Any]
        var sources = reference["sources"] as! [[String: Any]]
        reference["source_ids"] = [composed, decomposed]; sources[0]["id"] = composed; sources[1]["id"] = decomposed
        reference["sources"] = sources; comparison["reference"] = reference; body["comparison"] = comparison
        Stub.handler = { $0.respond(body) }
        let result = try await detect(refs: [composed, decomposed])
        XCTAssertEqual(result.comparison.reference.sourceIds.map { Array($0.utf8) }, [Array(composed.utf8), Array(decomposed.utf8)])
        Stub.handler = { $0.respond(self.detectionFixture()) }
        do { _ = try await detect(refs: ["w2", "w1"]); XCTFail("Accepted wrong source order") } catch PlatformError.invalidResponse {}
        let source: PlatformSweep = try decode(mutate(raw(1), path: ["settings", "label"], value: composed))
        let other: PlatformSweep = try decode(mutate(raw(2), path: ["settings", "label"], value: decomposed))
        let target: PlatformSweep = try decode(mutate(raw(3), path: ["settings", "label"], value: composed))
        XCTAssertFalse(PlatformSweepAverageComparison.validSources([source, other], target: target))
        var targetBody = raw(3); targetBody["stream_id"] = composed
        var sourceBody = raw(1); sourceBody["stream_id"] = decomposed
        XCTAssertFalse(PlatformSweepAverageComparison.validSources([try decode(sourceBody), other], target: try decode(targetBody)))
    }

    func testDetectorInvalidSourceRequestsAndUTF8LimitNeverReachTransport() async throws {
        Stub.handler = { _ in XCTFail("Invalid detector request reached transport") }
        let emoji = (1...16).map { String(repeating: "😀", count: 126) + String($0) }
        for (refs, target) in [(["w1"], "w3"), (["w1", "w1"], "w3"), (["w1", "w2"], "w2"),
                               ([" ", "w2"], "w3"), ([String(repeating: "é", count: 129), "w2"], "w3"),
                               (emoji, String(repeating: "😀", count: 128))] {
            do { _ = try await detect(refs: refs, target: target); XCTFail("Accepted invalid IDs") } catch PlatformError.invalidCommand {}
        }
        for threshold in [Double.infinity, -.infinity] {
            do { _ = try await detect(threshold: threshold); XCTFail("Accepted infinity") } catch PlatformError.invalidCommand {}
        }
    }

    func testDetectorCoreWhitespacePreservesZeroWidthIDsAndSettings() async throws {
        for exact in ["\u{200B}", "\u{FEFF}"] {
            for targetIsExact in [false, true] {
                let refs = targetIsExact ? ["w1", "w2"] : [exact, "w2"], targetID = targetIsExact ? exact : "w3"
                var body = detectionFixture(), comparison = fixture(), reference = fixture()["reference"] as! [String: Any]
                var target = comparison["target"] as! [String: Any]
                var sources = reference["sources"] as! [[String: Any]]
                func preservedSettings(_ n: Int) -> [String: Any] {
                    var value = settings(n)
                    value["\u{FEFF}extension"] = ["\u{FEFF}key": [exact, "\u{FEFF}value"], "key": "plain"]
                    return value
                }
                for i in sources.indices { sources[i]["id"] = refs[i]; sources[i]["settings"] = preservedSettings(i + 1) }
                reference["source_ids"] = refs; reference["sources"] = sources
                target["id"] = targetID; target["settings"] = preservedSettings(3)
                comparison["reference"] = reference; comparison["target"] = target
                comparison["session_id"] = exact; comparison["stream_id"] = exact; body["comparison"] = comparison
                Stub.handler = { stub in
                    struct Request: Decodable { let reference_ids: [String]; let target_id: String }
                    let request = try! bufferedTestRequest(stub.request)
                    let sent = try! JSONDecoder().decode(Request.self, from: request.httpBody!)
                    XCTAssertEqual(sent.reference_ids.map { Array($0.utf8) }, refs.map { Array($0.utf8) })
                    XCTAssertEqual(Array(sent.target_id.utf8), Array(targetID.utf8))
                    XCTAssertTrue(request.url!.absoluteString.contains(exact.addingPercentEncoding(withAllowedCharacters: .alphanumerics)!))
                    stub.respond(body)
                }
                let value = try await client().sweepAverageChangeDetection(sessionID: exact, referenceIDs: refs, targetID: targetID,
                    minimumIncreaseDb: 6, minimumBins: 2)
                XCTAssertEqual(Array(value.comparison.sessionId.utf8), Array(exact.utf8))
                XCTAssertEqual(Array(value.comparison.streamId.utf8), Array(exact.utf8))
                XCTAssertEqual(Array(value.comparison.target.id.utf8), Array(targetID.utf8))
                XCTAssertNotNil(value.comparison.target.settings["\u{FEFF}extension"])
                let frozen: [PlatformSweep] = try (1...3).map { n in
                    var item = raw(n); item["session_id"] = exact; item["stream_id"] = exact
                    item["id"] = n == 3 ? targetID : refs[n - 1]; item["settings"] = preservedSettings(n)
                    return try decode(item)
                }
                try value.validate(sources: Array(frozen.prefix(2)), target: frozen[2], minimumIncreaseDb: 6, minimumBins: 2)
            }
        }
    }

    func testDetectorRejectsEveryPythonWhitespaceOnlyID() async throws {
        let scalars = Array(0x0009...0x000D) + Array(0x001C...0x0020) + [0x0085, 0x00A0, 0x1680]
            + Array(0x2000...0x200A) + [0x2028, 0x2029, 0x202F, 0x205F, 0x3000]
        XCTAssertEqual(scalars.count, 29)
        Stub.handler = { _ in XCTFail("Whitespace-only detector ID reached transport") }
        for blank in [""] + scalars.map({ String(UnicodeScalar($0)!) }) {
            for (session, refs, target) in [(blank, ["w1", "w2"], "w3"), ("s", [blank, "w2"], "w3"), ("s", ["w1", "w2"], blank)] {
                do {
                    _ = try await client().sweepAverageChangeDetection(sessionID: session, referenceIDs: refs, targetID: target,
                        minimumIncreaseDb: 6, minimumBins: 2)
                    XCTFail("Accepted blank ID \(Array(blank.utf8))")
                } catch PlatformError.invalidCommand {}
            }
            for path in [["comparison", "session_id"], ["comparison", "stream_id"], ["comparison", "target", "id"]] {
                XCTAssertThrowsError(try decode(mutate(detectionFixture(), path: path, value: blank), as: PlatformSweepAverageChangeDetection.self))
            }
        }
    }

    func testDetectorRejectsOriginalFEFFKeyAliasesAtEveryFixedObjectLevel() async throws {
        let comparison = detectionFixture(deltas: [6, 6], threshold: 6, bins: 2,
            regions: [region(0, 1, peak: 0, increase: 6, frequencies: [100, 100])])
        let good: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": comparison]
        let paths = [[], ["data"], ["data", "comparison"], ["data", "detection"],
                     ["data", "comparison", "reference"], ["data", "comparison", "reference", "interval"],
                     ["data", "comparison", "target"], ["data", "comparison", "target", "interval"],
                     ["data", "comparison", "max_increase"], ["data", "comparison", "min_change"]]
        func aliases(_ root: [String: Any], path: [String]) -> [[String: Any]] {
            if let key = path.first {
                return aliases(root[key] as! [String: Any], path: Array(path.dropFirst())).map { child in
                    var result = root; result[key] = child; return result
                }
            }
            return root.map { key, value in var result = root; result["\u{FEFF}" + key] = value; return result }
        }
        var invalids = paths.flatMap { aliases(good, path: $0) }
        var data = comparison, reference = (comparison["comparison"] as! [String: Any])["reference"] as! [String: Any]
        let source = (reference["sources"] as! [[String: Any]])[0]
        for modified in aliases(source, path: []) + aliases(source, path: ["interval"]) {
            var sources = reference["sources"] as! [[String: Any]]; sources[0] = modified
            var changedReference = reference; changedReference["sources"] = sources
            var changedComparison = comparison["comparison"] as! [String: Any]; changedComparison["reference"] = changedReference
            data = comparison; data["comparison"] = changedComparison
            var envelope = good; envelope["data"] = data; invalids.append(envelope)
        }
        let found = comparison["detection"] as! [String: Any], one = (found["regions"] as! [[String: Any]])[0]
        for modified in aliases(one, path: []) {
            var changed = found; changed["regions"] = [modified]; data = comparison; data["detection"] = changed
            var envelope = good; envelope["data"] = data; invalids.append(envelope)
        }
        for envelope in invalids {
            let bytes = try JSONSerialization.data(withJSONObject: envelope)
            // JSONDecoder sees both original spellings, regardless of Foundation dictionary bridging.
            Stub.handler = { $0.respondBytes(bytes) }
            do { _ = try await detect(); XCTFail("Accepted FEFF field alias") } catch PlatformError.invalidResponse {}
        }
        for field in ["contract", "method", "level_unit", "delta_unit"] {
            let raw = String(data: try JSONSerialization.data(withJSONObject: good, options: [.sortedKeys]), encoding: .utf8)!
            let altered = raw.replacingOccurrences(of: "\"\(field)\":\"", with: "\"\(field)\":\"\\ufeff")
            XCTAssertNotEqual(raw, altered)
            Stub.handler = { $0.respondBytes(Data(altered.utf8)) }
            do { _ = try await detect(); XCTFail("Accepted FEFF-prefixed contract string") } catch PlatformError.invalidResponse {}
        }
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

    func testActualHTTPMaximalChangeDetectionAndFrozenSources() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_AVERAGE_DETECTION_SMOKE_BASE_URL"] else {
            throw XCTSkip("Set ORI_AVERAGE_DETECTION_SMOKE_BASE_URL to the synthetic detector fixture")
        }
        let api = try PlatformClient(baseURL: base)
        let sweeps = try await api.listSweeps(sessionID: "changes-one", streamID: "spectrum-one")
        let sources = try ["one-sweep-1", "one-sweep-2", "one-sweep-3"].map { id in try XCTUnwrap(sweeps.first { $0.id == id }) }
        let target = try XCTUnwrap(sweeps.first { $0.id == "one-sweep-4" })
        for (threshold, bins, starts, ends, peaks) in [(6.0, 2, [30, 69], [33, 70], [31, 70]),
                                                      (12.0, 2, [31], [32], [31]),
                                                      (6.0, 1, [30, 69, 90], [33, 70, 90], [31, 70, 90]),
                                                      (120.0, 1, [], [], [])] {
            let value = try await api.sweepAverageChangeDetection(sessionID: "changes-one", referenceIDs: sources.map(\.id),
                targetID: target.id, minimumIncreaseDb: threshold, minimumBins: bins)
            try value.validate(sources: sources, target: target, minimumIncreaseDb: threshold, minimumBins: bins)
            XCTAssertEqual(value.comparison.points, 101)
            XCTAssertEqual(value.comparison.frequenciesHz[31], value.comparison.frequenciesHz[32])
            XCTAssertEqual(value.detection.regions.map(\.startIndex), starts)
            XCTAssertEqual(value.detection.regions.map(\.endIndex), ends)
            XCTAssertEqual(value.detection.regions.map(\.peakIndex), peaks)
        }
    }
}
