import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class SweepContextTests: XCTestCase {
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
                : ["error": ["code": "context_incompatible", "message": "synthetic"]]
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
    private func raw(_ n: Int = 17, points: Int = 3) -> [String: Any] {
        ["id": "w\(n)", "session_id": "s", "stream_id": "tiny", "interval": ["started_at": String(format: "2026-11-08T01:00:%02d.000001Z", n), "ended_at": String(format: "2026-11-08T01:00:%02d.000002+00:00", n)],
         "settings": ["rbw_hz": 1000, "sequence": n, "extra": ["flag": false, "values": [1, 2]]],
         "frequencies_hz": Array(repeating: 100, count: points), "levels": (0..<points).map { -100.0 + Double(n) + Double($0) / 10 }, "level_unit": "dBm"]
    }
    private func fixture(before: Int = 2, after: Int = 2, availableBefore: Int? = nil, availableAfter: Int? = nil, points: Int = 3) -> [String: Any] {
        let b = availableBefore ?? before, a = availableAfter ?? after
        let items = (-b...a).map { offset -> [String: Any] in
            var item = raw(17 + offset, points: points); item["offset"] = offset
            item.removeValue(forKey: "frequencies_hz"); item.removeValue(forKey: "session_id"); item.removeValue(forKey: "stream_id")
            return item
        }
        return ["session_id": "s", "stream_id": "tiny", "anchor_id": "w17", "order": "started_at_then_id",
                "requested": ["before": before, "after": after], "available": ["before": b, "after": a],
                "missing": ["before": before - b, "after": after - a], "complete": before == b && after == a,
                "frequencies_hz": Array(repeating: 100, count: points), "level_unit": "dBm", "points": points, "items": items]
    }
    private func replacingItem(_ original: [String: Any], _ index: Int = 2, key: String, value: Any) -> [String: Any] {
        var result = original, items = original["items"] as! [[String: Any]]
        items[index][key] = value; result["items"] = items; return result
    }
    func testExactBoundedReadOnlyPOSTFullSingleDuplicateAndMaximumBins() async throws {
        for (before, after, points) in [(2, 2, 3), (0, 0, 1), (0, 0, 16_384), (3, 4, 16_384), (16, 16, 1)] {
            Stub.handler = { stub in
                let request = try! bufferedTestRequest(stub.request)
                XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 5)
                XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s/sweep-context")
                XCTAssertNil(request.url?.query)
                let body = try! JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                XCTAssertEqual(Set(body.keys), ["sweep_id", "before", "after"])
                XCTAssertEqual(body["sweep_id"] as? String, "w17"); XCTAssertEqual(body["before"] as? Int, before); XCTAssertEqual(body["after"] as? Int, after)
                XCTAssertLessThanOrEqual(request.httpBody!.count, 8192)
                stub.respond(self.fixture(before: before, after: after, points: points))
            }
            let result = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: before, after: after)
            XCTAssertEqual(result.points, points); XCTAssertEqual(result.items.count, before + 1 + after)
            XCTAssertEqual(result.items.map(\.offset), Array(-before...after))
            try result.validate(anchor: decode(raw(points: points)), before: before, after: after)
        }
    }
    func testPartialAndZeroSideWindowCounts() throws {
        for (b, a) in [(0, 0), (1, 0), (0, 2), (2, 1)] {
            let value: PlatformSweepContext = try decode(fixture(availableBefore: b, availableAfter: a))
            XCTAssertEqual(value.items.count, b + 1 + a); XCTAssertEqual(value.missing.before, 2 - b)
            XCTAssertEqual(value.missing.after, 2 - a); XCTAssertFalse(value.complete)
        }
        let zero: PlatformSweepContext = try decode(fixture(before: 0, after: 0))
        XCTAssertTrue(zero.complete); XCTAssertEqual(zero.items.map(\.offset), [0])
    }
    func testInvalidRequestsNeverReachTransport() async throws {
        Stub.handler = { _ in XCTFail("Invalid request reached transport") }
        for (b, a) in [(-1, 2), (2, -1), (17, 0), (0, 17), (Int.max, 2)] {
            do { _ = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: b, after: a); XCTFail("Accepted counts") }
            catch PlatformError.invalidCommand {}
        }
        for id in ["", "\n", String(repeating: "a", count: 129), String(repeating: "e\u{301}", count: 65)] {
            do { _ = try await api().sweepContext(sessionID: "s", sweepID: id, before: 2, after: 2); XCTFail("Accepted ID") }
            catch PlatformError.invalidCommand {}
        }
        do { _ = try await api().sweepContext(sessionID: " ", sweepID: "w17", before: 0, after: 0); XCTFail("Accepted scope") }
        catch PlatformError.invalidCommand {}
    }
    func testAllStatusesBoundedBeforeDecodeAndOnly200Accepted() async throws {
        for status in [200, 201, 204, 206, 400, 404, 409, 413, 500] {
            Stub.handler = { $0.bytes(Data(repeating: 32, count: 2_000_001), status: status) }
            do { _ = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: 2, after: 2); XCTFail("Accepted oversized \(status)") }
            catch PlatformError.responseTooLarge {}
        }
        for status in [201, 202, 204, 206, 299, 400, 404, 409, 413] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: 2, after: 2); XCTFail("Accepted \(status)") }
            catch PlatformError.http(let actual, let code, _) { XCTAssertEqual(actual, status); XCTAssertEqual(code, "context_incompatible") }
            catch PlatformError.invalidResponse { XCTAssertLessThan(status, 300) }
        }
    }
    func testMalformedCountsGridUnitsOffsetsUniquenessAndTotalCap() throws {
        let changes: [(String, Any)] = [("session_id", " "), ("stream_id", ""), ("anchor_id", "wrong"), ("order", "id"),
            ("level_unit", "dB"), ("points", 0), ("points", 16_385), ("points", true), ("complete", false),
            ("requested", ["before": true, "after": 2]), ("available", ["before": 3, "after": 1]),
            ("missing", ["before": 0, "after": 1]), ("items", []), ("frequencies_hz", [100, 99, 101]),
            ("frequencies_hz", [-1, 100, 100]), ("frequencies_hz", [100, 100, 9_007_199_254_740_992 as Int64]),
            ("frequencies_hz", [100, 100, 100.5]), ("frequencies_hz", [100, true, 100])]
        for (key, value) in changes {
            var body = fixture(); body[key] = value
            XCTAssertThrowsError(try decode(body, as: PlatformSweepContext.self), key)
        }
        for (key, value) in [("offset", 1 as Any), ("offset", true as Any), ("id", "w16" as Any), ("id", String(repeating: "x", count: 129) as Any),
                              ("level_unit", "S" as Any), ("levels", [-80] as Any), ("levels", [false, -80, -80] as Any),
                              ("settings", ["sequence": 17] as Any), ("settings", ["rbw_hz": NSNull()] as Any)] {
            XCTAssertThrowsError(try decode(replacingItem(fixture(), key: key, value: value), as: PlatformSweepContext.self), key)
        }
        XCTAssertThrowsError(try decode(fixture(before: 4, after: 4, points: 16_384), as: PlatformSweepContext.self))
        var unordered = fixture(), items = unordered["items"] as! [[String: Any]]
        items.swapAt(0, 1); unordered["items"] = items
        XCTAssertThrowsError(try decode(unordered, as: PlatformSweepContext.self))
    }
    func testStrictUTCAndExactMicrosecondOrderingWithBinaryIDTies() throws {
        for start in ["2026-11-08T01:00:17.000003Z", "2026-11-08T01:00:17.0000001Z", "2026-11-08T01:00:17.Z", "2026-02-30T01:00:17Z", "2026-11-08T03:00:17+02:00", "2026-11-08t01:00:17z"] {
            XCTAssertThrowsError(try decode(replacingItem(fixture(), key: "interval", value: ["started_at": start, "ended_at": "2026-11-08T01:00:17.000002Z"]), as: PlatformSweepContext.self), start)
        }
        var body = fixture(before: 1, after: 1), items = body["items"] as! [[String: Any]]
        let ids = ["a", "e\u{301}", "é"] // exact UTF-8 order, last two are canonically equivalent Swift strings
        for index in items.indices { items[index]["id"] = ids[index]; items[index]["interval"] = ["started_at": "2026-11-08T01:00:17.000001Z", "ended_at": "2026-11-08T01:00:17.000002Z"] }
        body["items"] = items; body["anchor_id"] = ids[1]
        let result: PlatformSweepContext = try decode(body)
        XCTAssertEqual(result.items.count, 3); XCTAssertEqual(Data(result.items[1].id.utf8), Data(ids[1].utf8))
        items[0]["id"] = "z"; body["items"] = items
        XCTAssertThrowsError(try decode(body, as: PlatformSweepContext.self))
        items[0]["id"] = "a"; items[0]["interval"] = ["started_at": "2026-11-08T01:00:17.000002Z", "ended_at": "2026-11-08T01:00:17.000002Z"]; body["items"] = items
        XCTAssertThrowsError(try decode(body, as: PlatformSweepContext.self))
    }
    func testSettingsCompatibilityIgnoresOnlySequenceAndPreservesJSONTypes() throws {
        let sourceSettings = raw()["settings"] as! [String: Any]
        for (key, value) in [("rbw_hz", 2000 as Any), ("extra", ["flag": 0, "values": [1, 2]] as Any), ("unknown", false as Any)] {
            var settings = sourceSettings; settings[key] = value
            XCTAssertThrowsError(try decode(replacingItem(fixture(), 0, key: "settings", value: settings), as: PlatformSweepContext.self))
        }
        var settings = sourceSettings; settings["sequence"] = NSNull()
        XCTAssertNoThrow(try decode(replacingItem(fixture(), 0, key: "settings", value: settings), as: PlatformSweepContext.self))
        for mode in ["string", "array", "key", "nestedKey"] {
            func settings(_ text: String) -> [String: Any] {
                switch mode {
                case "string": return ["label": text, "sequence": 17]
                case "array": return ["extension": [false, ["name": text]], "sequence": 17]
                case "key": return [text: "same", "sequence": 17]
                default: return ["extension": [text: "same"], "sequence": 17]
                }
            }
            var body = fixture(), items = body["items"] as! [[String: Any]]
            for index in items.indices { items[index]["settings"] = settings("é") }; body["items"] = items
            let value: PlatformSweepContext = try decode(body)
            var anchor = raw(); anchor["settings"] = settings("e\u{301}")
            XCTAssertThrowsError(try value.validate(anchor: decode(anchor), before: 2, after: 2), mode)
            items[0]["settings"] = settings("e\u{301}"); body["items"] = items
            XCTAssertThrowsError(try decode(body, as: PlatformSweepContext.self), mode)
        }
    }
    func testFrozenAnchorRequiresFullIdentityRawDataIntervalAndSequence() throws {
        let value: PlatformSweepContext = try decode(fixture())
        for (key, replacement) in [("id", "other" as Any), ("session_id", "other" as Any), ("stream_id", "other" as Any),
                                    ("settings", ["rbw_hz": 1000, "sequence": 99, "extra": ["flag": false, "values": [1, 2]]] as Any),
                                    ("levels", [-83, -82.9, -80] as Any), ("frequencies_hz", [100, 100, 101] as Any),
                                    ("interval", ["started_at": "2026-11-08T01:00:17.000001Z", "ended_at": "2026-11-08T01:00:17.000003Z"] as Any)] {
            var anchor = raw(); anchor[key] = replacement
            XCTAssertThrowsError(try value.validate(anchor: decode(anchor), before: 2, after: 2), key)
        }
        XCTAssertThrowsError(try value.validate(anchor: decode(raw()), before: 1, after: 2))
        XCTAssertThrowsError(try value.validate(anchor: decode(raw()), before: 2, after: 1))
    }
    func testRawKeyCollisionsAndDuplicateFieldsRejectedForContextAndSourceLists() async throws {
        for list in [false, true] {
            let envelope: [String: Any] = ["contract": "openradioinspector-platform", "contract_version": 1, "data": list ? [raw()] : fixture()]
            let rawJSON = String(data: try JSONSerialization.data(withJSONObject: envelope), encoding: .utf8)!
            for extra in ["\"é\":1,\"e\\u0301\":2,", "\"rbw_hz\":99,"] {
                let bad = rawJSON.replacingOccurrences(of: "\"settings\":{", with: "\"settings\":{\(extra)")
                Stub.handler = { $0.bytes(Data(bad.utf8)) }
                do {
                    if list { _ = try await api().listSweeps(sessionID: "s", streamID: "tiny") }
                    else { _ = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: 2, after: 2) }
                    XCTFail("Lossy JSON accepted")
                } catch PlatformError.invalidResponse {}
            }
        }
    }
    func testResponseIdentityByteExactAndUnicodeCodepointLimit() async throws {
        for field in ["session_id", "anchor_id"] {
            var body = fixture(before: 0, after: 0)
            body[field] = "é"
            if field == "anchor_id" { body = replacingItem(body, 0, key: "id", value: "é") }
            Stub.handler = { $0.respond(body) }
            do { _ = try await api().sweepContext(sessionID: field == "session_id" ? "e\u{301}" : "s", sweepID: field == "anchor_id" ? "e\u{301}" : "w17", before: 0, after: 0); XCTFail("Collapsed ID") }
            catch PlatformError.invalidResponse {}
        }
        let id = String(repeating: "😀", count: 128), session = String(repeating: "s", count: 200)
        var body = replacingItem(fixture(before: 0, after: 0), 0, key: "id", value: id); body["anchor_id"] = id; body["session_id"] = session
        Stub.handler = { $0.respond(body) }
        let result = try await api().sweepContext(sessionID: session, sweepID: id, before: 0, after: 0)
        XCTAssertEqual(result.anchorId.unicodeScalars.count, 128)
        Stub.handler = { $0.respond(self.fixture(before: 1, after: 2)) }
        do { _ = try await api().sweepContext(sessionID: "s", sweepID: "w17", before: 2, after: 2); XCTFail("Wrong requested counts") }
        catch PlatformError.invalidResponse {}
    }
    func testActualTransportCancellation() async throws {
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let client = try api(), task = Task { try await client.sweepContext(sessionID: "s", sweepID: "w17", before: 2, after: 2) }
        await fulfillment(of: [started], timeout: 1); task.cancel()
        do { _ = try await task.value; XCTFail("Cancelled returned") } catch is CancellationError {}
        await fulfillment(of: [stopped], timeout: 1)
    }
    func testActualHTTPSyntheticContext() async throws {
        guard let base = ProcessInfo.processInfo.environment["ORI_CONTEXT_SMOKE_BASE_URL"] else { throw XCTSkip("Set ORI_CONTEXT_SMOKE_BASE_URL to synthetic fixture") }
        let client = try PlatformClient(baseURL: base)
        let value = try await client.sweepContext(sessionID: "context-one", sweepID: "one-sweep-4", before: 2, after: 2)
        XCTAssertEqual(value.items.map(\.id), (2...6).map { "one-sweep-\($0)" }); XCTAssertEqual(value.items.map(\.offset), [-2, -1, 0, 1, 2])
        XCTAssertEqual(value.points, 101); XCTAssertTrue(value.complete); XCTAssertEqual(value.streamId, "spectrum-one")
        XCTAssertEqual(value.items[2].levels.first, -96); XCTAssertEqual(value.items[2].settings["sequence"], .number(4))
        let anchor = try await client.listSweeps(sessionID: "context-one", streamID: "spectrum-one").first { $0.id == "one-sweep-4" }
        try value.validate(anchor: XCTUnwrap(anchor), before: 2, after: 2)
        let edge = try await client.sweepContext(sessionID: "context-one", sweepID: "one-sweep-1", before: 2, after: 2)
        XCTAssertFalse(edge.complete); XCTAssertEqual(edge.missing.before, 2); XCTAssertEqual(edge.available.after, 2)
    }
}
