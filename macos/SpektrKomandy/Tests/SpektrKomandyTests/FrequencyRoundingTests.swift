import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class FrequencyRoundingTests: XCTestCase {
    typealias Stub = SweepThresholdTests.Stub
    private let maximum: Int64 = 9_007_199_254_740_991
    private func client() throws -> PlatformClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: configuration))
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    private func profile(start: Int64 = 100, end: Int64 = 200, step: Int64 = 10,
                         origin: Int64 = 0, tolerance: Int64 = 5) -> [String: Any] {
        ["format": "openradioinspector-frequency-rounding", "format_version": 1, "name": "Example",
         "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
         "rules": [["start_frequency_hz": start, "end_frequency_hz": end, "step_hz": step,
                    "origin_frequency_hz": origin, "max_offset_hz": tolerance]]]
    }
    private func json(_ object: [String: Any]) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]), as: UTF8.self)
    }
    private func fixture(session: String = "s", candidate: String = "c", frequency: Int64 = 105,
                         unit: String = "dBm", profile p: [String: Any]? = nil,
                         status: String = "rounded", nearest: Int64? = 100, offset: Int64? = -5,
                         rounded: Int64? = 100) -> [String: Any] {
        ["session_id": session, "candidate": ["id": candidate, "source_stream_id": "stream",
          "interval": ["started_at": "2026-11-08T12:00:00.000001+00:00", "ended_at": "2026-11-08T12:00:00.000002Z"],
          "center_frequency_hz": frequency, "level_unit": unit],
         "profile": p ?? profile(), "frequency_unit": "Hz", "method": "nearest_grid", "tie_break": "lower",
         "result": ["status": status, "rule_index": status == "outside_ranges" ? NSNull() : 0,
          "nearest_frequency_hz": nearest as Any? ?? NSNull(), "offset_hz": offset as Any? ?? NSNull(),
          "rounded_frequency_hz": rounded as Any? ?? NSNull()]]
    }
    private func decode<T: Decodable>(_ type: T.Type, _ object: [String: Any]) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: JSONSerialization.data(withJSONObject: object))
    }
    private func decode(_ object: [String: Any]) throws -> PlatformCandidateFrequencyRounding {
        try decode(PlatformCandidateFrequencyRounding.self, object)
    }
    private func respond(_ stub: Stub, raw: Data, status: Int = 200) {
        stub.client?.urlProtocol(stub, didReceive: HTTPURLResponse(url: stub.request.url!, statusCode: status,
            httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        stub.client?.urlProtocol(stub, didLoad: raw)
        stub.client?.urlProtocolDidFinishLoading(stub)
    }
    private func rejected(_ body: [String: Any], raw: String? = nil, submitted: String? = nil,
                          session: String = "s", candidate: String = "c") async throws {
        if let raw { Stub.handler = { self.respond($0, raw: Data(raw.utf8)) } }
        else { Stub.handler = { $0.respond(body) } }
        do {
            _ = try await client().candidateFrequencyRounding(sessionID: session, candidateID: candidate,
                profileJSON: submitted ?? json(profile()))
            XCTFail("Accepted malformed response")
        } catch PlatformError.invalidResponse {} catch { XCTFail("Unexpected error: \(error)") }
    }

    func testExactPOSTRawProfileIDsTimeoutAndUnits() async throws {
        let session = "s/a? &é", candidate = " e\u{301}/+? "
        var p = profile(); p["name"] = "  e\u{301}  "
        let raw = " \n" + (try json(p)) + "\n "
        p["name"] = "é"
        let body = fixture(session: session, candidate: candidate, unit: "s_meter_count", profile: p)
        Stub.handler = { stub in
            do {
                let request = try bufferedTestRequest(stub.request)
                XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 6)
                XCTAssertEqual(request.url?.absoluteString,
                    "https://example.test/root/api/v1/sessions/s%2Fa%3F%20%26%C3%A9/candidate-frequency-rounding")
                XCTAssertNil(URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.query)
                let data = try JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as! [String: String]
                XCTAssertEqual(Set(data.keys), ["candidate_id", "profile_json"])
                XCTAssertEqual(Array(data["candidate_id"]!.utf8), Array(candidate.utf8))
                XCTAssertEqual(Array(data["profile_json"]!.utf8), Array(raw.utf8))
            } catch { XCTFail("\(error)") }
            stub.respond(body)
        }
        let value = try await client().candidateFrequencyRounding(sessionID: session, candidateID: candidate, profileJSON: raw)
        XCTAssertEqual(value.candidate.levelUnit, "s_meter_count")
        XCTAssertEqual(value.candidate.centerFrequencyHz, 105)
        XCTAssertEqual(Array(value.profile.name.utf8), Array("é".utf8))
    }

    func testCustomRecordedLevelUnitRemainsExactAndBlankIsRejected() async throws {
        let unit = "custom/e\u{301} units"
        let body = fixture(unit: unit)
        Stub.handler = { $0.respond(body) }
        let value = try await client().candidateFrequencyRounding(sessionID: "s", candidateID: "c", profileJSON: json(profile()))
        XCTAssertEqual(Array(value.candidate.levelUnit.utf8), Array(unit.utf8))
        var source = body["candidate"] as! [String: Any]; source["session_id"] = "s"
        let original = try decode(PlatformCandidate.self, source)
        XCTAssertTrue(PlatformCandidateFrequencyRounding.validSource(original))
        try value.validate(source: original, profileJSON: json(profile()))
        source["level_unit"] = "custom/é units"
        XCTAssertThrowsError(try value.validate(source: decode(PlatformCandidate.self, source), profileJSON: json(profile())))
        XCTAssertThrowsError(try decode(fixture(unit: " \t")))
        XCTAssertThrowsError(try decode(fixture(unit: "\u{001C}\u{001F}")))
    }

    func testExactCoreWhitespaceForIDsScopesUnitsAndProfileNames() async throws {
        for exact in ["\u{200B}", "\u{FEFF}"] {
            var body = fixture(session: exact, candidate: exact, unit: exact)
            var source = body["candidate"] as! [String: Any]
            source["source_stream_id"] = exact; body["candidate"] = source
            Stub.handler = { stub in
                let request = try! bufferedTestRequest(stub.request)
                let sent = try! JSONDecoder().decode([String: String].self, from: request.httpBody!)
                XCTAssertEqual(Array(sent["candidate_id"]!.utf8), Array(exact.utf8))
                XCTAssertTrue(request.url!.absoluteString.contains(exact.addingPercentEncoding(withAllowedCharacters: .alphanumerics)!))
                stub.respond(body)
            }
            let value = try await client().candidateFrequencyRounding(sessionID: exact, candidateID: exact, profileJSON: json(profile()))
            for returned in [value.sessionId, value.candidate.id, value.candidate.sourceStreamId, value.candidate.levelUnit] {
                XCTAssertEqual(Array(returned.utf8), Array(exact.utf8))
            }
            source["session_id"] = exact
            let candidate = try decode(PlatformCandidate.self, source)
            XCTAssertTrue(PlatformCandidateFrequencyRounding.validSource(candidate))
            try value.validate(source: candidate, profileJSON: json(profile()))
            for name in [exact, "Name" + exact, exact + "Name"] {
                var p = profile(); p["name"] = name
                XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(p)))
            }
        }
        let response = try json(["contract": "openradioinspector-platform", "contract_version": 1, "data": fixture()])
        try await rejected(fixture(), raw: response.replacingOccurrences(of: "\"format\":",
            with: "\"\\ufeffformat\":\"openradioinspector-frequency-rounding\",\"format\":"))
        for field in ["format", "frequency_unit", "method", "tie_break"] {
            var p = profile(); p[field] = "\u{FEFF}" + (p[field] as! String)
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(p)), field)
        }
        for field in ["format", "name", "start_frequency_hz"] {
            let original = try json(profile())
            let disguised = original.replacingOccurrences(of: "\"" + field + "\"", with: "\"\\ufeff" + field + "\"")
            XCTAssertNotEqual(original, disguised)
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(disguised))
        }
        for scalar in [0x001C, 0x001D, 0x001E, 0x001F] {
            let blank = String(UnicodeScalar(scalar)!)
            Stub.handler = { _ in XCTFail("Blank request ID should not reach transport") }
            for (session, candidate) in [(blank, "c"), ("s", blank)] {
                do {
                    _ = try await client().candidateFrequencyRounding(sessionID: session, candidateID: candidate, profileJSON: json(profile()))
                    XCTFail("Accepted blank request ID")
                } catch PlatformError.invalidCommand {} catch { XCTFail("\(error)") }
            }
            XCTAssertThrowsError(try decode(fixture(session: blank)))
            for field in ["id", "source_stream_id", "level_unit"] {
                var body = fixture(); var source = body["candidate"] as! [String: Any]
                source[field] = blank; body["candidate"] = source
                XCTAssertThrowsError(try decode(body), field)
                source["session_id"] = "s"
                XCTAssertFalse(PlatformCandidateFrequencyRounding.validSource(try decode(PlatformCandidate.self, source)), field)
            }
        }
    }

    func testRawExactResponseKeysRejectFEFFAliasesAtEveryObjectLevel() async throws {
        let raw = try json(["contract": "openradioinspector-platform", "contract_version": 1, "data": fixture()])
        // Keep identical values in both spellings, so rejection cannot accidentally rely on
        // Foundation choosing a conflicting value when it drops the leading FEFF.
        for (field, value) in [
            ("contract", "\"openradioinspector-platform\""),
            ("session_id", "\"s\""),
            ("id", "\"c\""),
            ("started_at", "\"2026-11-08T12:00:00.000001+00:00\""),
            ("status", "\"rounded\""),
            ("format", "\"openradioinspector-frequency-rounding\""),
            ("start_frequency_hz", "100")
        ] {
            let original = "\"\(field)\":\(value)"
            let alias = "\"\\ufeff\(field)\":\(value)"
            for replacement in [alias + "," + original, original + "," + alias] {
                let malformed = raw.replacingOccurrences(of: original, with: replacement)
                XCTAssertNotEqual(malformed, raw, field)
                try await rejected(fixture(), raw: malformed)
            }
        }
    }

    func testIntegerGridEdgeCasesAndAllStatuses() throws {
        let cases: [(Int64, [String: Any], String, Int64?, Int64?, Int64?)] = [
            (105, profile(), "rounded", 100, -5, 100),
            (100, profile(), "unchanged", 100, 0, 100),
            (105, profile(tolerance: 4), "outside_tolerance", 100, -5, nil),
            (99, profile(), "outside_ranges", nil, nil, nil),
            (1, profile(start: 0, end: 20), "rounded", 0, -1, 0),
            (105, profile(origin: 1000), "rounded", 100, -5, 100),
            (104, profile(start: 104, end: 116, tolerance: 6), "rounded", 110, 6, 110),
            (116, profile(start: 104, end: 116, tolerance: 6), "rounded", 110, -6, 110),
            (maximum, profile(start: maximum - 10, end: maximum, step: 2), "rounded", maximum - 1, -1, maximum - 1),
            (maximum, profile(start: maximum, end: maximum, step: 1), "unchanged", maximum, 0, maximum)
        ]
        for (frequency, p, status, nearest, offset, rounded) in cases {
            let value = try decode(fixture(frequency: frequency, profile: p, status: status,
                nearest: nearest, offset: offset, rounded: rounded))
            XCTAssertEqual(value.result.status, status)
            XCTAssertEqual(value.result.nearestFrequencyHz, nearest)
            XCTAssertEqual(value.result.offsetHz, offset)
            XCTAssertEqual(value.result.roundedFrequencyHz, rounded)
        }
    }

    func testCanonicalProfileRoundTripAndStrictRawNumbers() throws {
        var p = profile(); p["name"] = " e\u{301} "
        let value = try PlatformFrequencyRoundingProfile.submitted(json(p))
        XCTAssertEqual(Array(value.name.utf8), Array("é".utf8))
        let canonical = try value.canonicalData()
        XCTAssertEqual(try PlatformFrequencyRoundingProfile.submitted(String(decoding: canonical, as: UTF8.self)), value)
        let object = try JSONSerialization.jsonObject(with: canonical) as! [String: Any]
        XCTAssertEqual(Set(object.keys), Set(profile().keys))
        let original = try json(profile())
        for token in ["true", "false", "null", "\"10\"", "10.0", "1e1", "10.5", "-1", "0", "9007199254740992"] {
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(
                original.replacingOccurrences(of: "\"step_hz\":10", with: "\"step_hz\":\(token)")), token)
        }
        for raw in ["{\"name\":\"first\"," + String(original.dropFirst()),
                    original.replacingOccurrences(of: "\"step_hz\":10", with: "\"step_hz\":10,\"step_hz\":10"),
                    "{\"é\":1,\"e\\u0301\":2," + String(original.dropFirst()),
                    original + "null", original.replacingOccurrences(of: "\"format_version\":1", with: "\"format_version\":1.0")] {
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(raw), raw)
        }
        for name in ["", "  ", "a\n", "a\u{200d}", String(repeating: "é", count: 81)] {
            var invalid = profile(); invalid["name"] = name
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(invalid)))
        }
        XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(String(repeating: " ", count: 65_537)))
        for p in [profile(start: 2, end: 3, step: 10), profile(start: 200, end: 100), profile(origin: maximum + 1)] {
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(p)))
        }
        var overlap = profile(); overlap["rules"] = (profile()["rules"] as! [[String: Any]]) + (profile()["rules"] as! [[String: Any]])
        XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(overlap)))
    }

    func testProfileRuleCountFieldsOrderingAndZeroTolerance() throws {
        var p = profile()
        for rules in [[], Array(repeating: (profile()["rules"] as! [[String: Any]])[0], count: 129)] {
            p["rules"] = rules
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(p)))
        }
        let rules: [[String: Any]] = (0..<128).map { i in
            ["start_frequency_hz": i * 10, "end_frequency_hz": i * 10 + 9,
             "step_hz": 1, "origin_frequency_hz": 0, "max_offset_hz": 0]
        }
        p["rules"] = rules
        XCTAssertEqual(try PlatformFrequencyRoundingProfile.submitted(json(p)).rules.count, 128)
        p["rules"] = Array(rules.reversed())
        XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(p)))
        for key in profile().keys {
            var missing = profile(); missing.removeValue(forKey: key)
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(missing)), key)
        }
        var extra = profile(); extra["extension"] = true
        XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(extra)))
        for field in ["start_frequency_hz", "end_frequency_hz", "step_hz", "origin_frequency_hz", "max_offset_hz"] {
            var invalid = profile(); var rule = (invalid["rules"] as! [[String: Any]])[0]
            rule.removeValue(forKey: field); invalid["rules"] = [rule]
            XCTAssertThrowsError(try PlatformFrequencyRoundingProfile.submitted(json(invalid)), field)
        }
        XCTAssertEqual(try decode(fixture(frequency: 100, profile: profile(tolerance: 0),
            status: "unchanged", nearest: 100, offset: 0, rounded: 100)).result.status, "unchanged")
        XCTAssertEqual(try decode(fixture(profile: profile(tolerance: 0), status: "outside_tolerance",
            rounded: nil)).result.status, "outside_tolerance")
    }

    func testCandidateCodePointLimitWithoutScopeLimits() async throws {
        let longScope = String(repeating: "s", count: 200)
        let candidate = String(repeating: "e\u{301}", count: 64)
        var body = fixture(session: longScope, candidate: candidate)
        var source = body["candidate"] as! [String: Any]; source["source_stream_id"] = longScope; body["candidate"] = source
        Stub.handler = { $0.respond(body) }
        let value = try await client().candidateFrequencyRounding(sessionID: longScope, candidateID: candidate, profileJSON: json(profile()))
        XCTAssertEqual(value.candidate.sourceStreamId, longScope)
        Stub.handler = { _ in XCTFail("Invalid request reached transport") }
        for (session, id, text) in [("", "c", "{}"), ("s", "", "{}"), ("s", " \n", "{}"),
            ("s", candidate + "x", "{}"), ("s", "c", String(repeating: "é", count: 32_769))] {
            do { _ = try await client().candidateFrequencyRounding(sessionID: session, candidateID: id, profileJSON: text); XCTFail("Accepted invalid request") }
            catch PlatformError.invalidCommand {} catch { XCTFail("\(error)") }
        }
    }

    func testFrozenProvenanceEveryFieldAndMutableFieldsExcluded() throws {
        let response = try decode(fixture())
        var source = fixture()["candidate"] as! [String: Any]; source["session_id"] = "s"
        source["state"] = "accepted"; source["score"] = 100; source["peak_level"] = -40
        source["observation_ids"] = ["new-observation"]
        try response.validate(source: decode(PlatformCandidate.self, source), profileJSON: json(profile()))
        let changes: [(String, Any)] = [("id", "other"), ("session_id", "other"), ("source_stream_id", "other"),
            ("center_frequency_hz", 106), ("level_unit", "s_meter_count"),
            ("interval", ["started_at": "2026-11-08T12:00:00.000002+00:00", "ended_at": "2026-11-08T12:00:00.000002Z"]),
            ("interval", ["started_at": "2026-11-08T12:00:00.000001+00:00", "ended_at": "2026-11-08T12:00:00.000003Z"])]
        for (key, value) in changes {
            var changed = source; changed[key] = value
            XCTAssertThrowsError(try response.validate(source: decode(PlatformCandidate.self, changed), profileJSON: json(profile())), key)
        }
        XCTAssertThrowsError(try response.validate(source: decode(PlatformCandidate.self, source), profileJSON: json(profile(tolerance: 6))))
        for unit in ["dBm", "s_meter_count"] {
            XCTAssertEqual(try decode(fixture(unit: unit)).candidate.levelUnit, unit)
        }
        var reversed = fixture(); var c = reversed["candidate"] as! [String: Any]
        c["interval"] = ["started_at": "2026-11-08T12:00:00.000002Z", "ended_at": "2026-11-08T12:00:00.000001Z"]
        reversed["candidate"] = c; XCTAssertThrowsError(try decode(reversed))
    }

    func testMalformedResultsProvenanceAndEchoRejected() async throws {
        for (field, value) in [("status", "unchanged" as Any), ("rule_index", 1), ("nearest_frequency_hz", 110),
            ("offset_hz", 5), ("rounded_frequency_hz", NSNull())] {
            var body = fixture(); var result = body["result"] as! [String: Any]; result[field] = value; body["result"] = result
            try await rejected(body)
        }
        for (field, value) in [("center_frequency_hz", 0 as Any), ("center_frequency_hz", maximum + 1),
            ("level_unit", " "), ("source_stream_id", ""), ("id", "other"), ("peak_level", -90)] {
            var body = fixture(); var candidate = body["candidate"] as! [String: Any]
            candidate[field] = value; body["candidate"] = candidate; try await rejected(body)
        }
        try await rejected(fixture(session: "other"))
        try await rejected(fixture(profile: profile(tolerance: 6)))
        try await rejected(fixture(candidate: "é"), candidate: "e\u{301}")
        try await rejected(fixture(session: "é"), session: "e\u{301}")
        let raw = try json(["contract": "openradioinspector-platform", "contract_version": 1, "data": fixture()])
        for field in ["contract_version", "center_frequency_hz", "rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"] {
            let value = ["contract_version": "1", "center_frequency_hz": "105", "rule_index": "0",
                "nearest_frequency_hz": "100", "offset_hz": "-5", "rounded_frequency_hz": "100"][field]!
            for token in [value + ".0", "true", "\"\(value)\""] {
                try await rejected(fixture(), raw: raw.replacingOccurrences(of: "\"\(field)\":\(value)", with: "\"\(field)\":\(token)"))
            }
        }
        try await rejected(fixture(), raw: raw.replacingOccurrences(of: "\"rule_index\":0", with: "\"rule_index\":0,\"rule_index\":0"))
    }

    func testOnlyHTTP200AndAllStatusResponseSizeCaps() async throws {
        let api = try client(), raw = try json(profile())
        for status in [201, 202, 204, 206, 299] {
            Stub.handler = { $0.respond(self.fixture(), status: status) }
            do { _ = try await api.candidateFrequencyRounding(sessionID: "s", candidateID: "c", profileJSON: raw); XCTFail("Accepted \(status)") }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
        for (status, code) in [(400, "invalid_request"), (404, "session_not_found"), (404, "candidate_not_found"),
            (409, "frequency_rounding_incompatible"), (413, "request_too_large")] {
            Stub.handler = { $0.respond(["code": code, "message": "Unavailable"], status: status) }
            do { _ = try await api.candidateFrequencyRounding(sessionID: "s", candidateID: "c", profileJSON: raw); XCTFail("Accepted error") }
            catch let error as PlatformError { XCTAssertEqual(error, .http(status: status, code: code, message: "Unavailable")) }
        }
        for status in [200, 201, 400, 404, 409, 413, 500] {
            Stub.handler = { self.respond($0, raw: Data(repeating: 32, count: 2_000_001), status: status) }
            do { _ = try await api.candidateFrequencyRounding(sessionID: "s", candidateID: "c", profileJSON: raw); XCTFail("Accepted oversized \(status)") }
            catch PlatformError.responseTooLarge {} catch { XCTFail("\(error)") }
        }
    }

    func testCancellationStopsTransport() async throws {
        let started = expectation(description: "Started"), stopped = expectation(description: "Stopped")
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let api = try client(), raw = try json(profile())
        let task = Task { try await api.candidateFrequencyRounding(sessionID: "s", candidateID: "c", profileJSON: raw) }
        await fulfillment(of: [started], timeout: 2); task.cancel()
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch is CancellationError {} catch { XCTFail("\(error)") }
        await fulfillment(of: [stopped], timeout: 2)
    }

    func testLiveSyntheticCoreWhenExplicitlyConfigured() async throws {
        guard let url = ProcessInfo.processInfo.environment["ORI_ROUNDING_SMOKE_BASE_URL"] else {
            throw XCTSkip("Requires explicitly configured synthetic rounding fixture")
        }
        let repository = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let raw = try String(contentsOf: repository.appendingPathComponent("docs/examples/frequency-rounding-profile.json"), encoding: .utf8)
        let api = try PlatformClient(baseURL: url)
        for (id, status, original, nearest, offset, unit) in [
            ("one-rounded", "rounded", Int64(103_025_123), Int64(103_000_000), Int64(-25_123), "dBm"),
            ("one-unchanged", "unchanged", 103_000_000, 103_000_000, 0, "dBm"),
            ("one-tolerance", "outside_tolerance", 103_050_000, 103_000_000, -50_000, "dBm"),
            ("one-receiver", "rounded", 145_506_250, 145_500_000, -6250, "s_meter_count")] {
            let value = try await api.candidateFrequencyRounding(sessionID: "rounding-one", candidateID: id, profileJSON: raw)
            XCTAssertEqual(value.result.status, status); XCTAssertEqual(value.candidate.centerFrequencyHz, original)
            XCTAssertEqual(value.result.nearestFrequencyHz, nearest); XCTAssertEqual(value.result.offsetHz, offset)
            XCTAssertEqual(value.candidate.levelUnit, unit)
            XCTAssertEqual(value.candidate.interval.startedAt, "2026-11-08T12:00:00.000001Z")
            XCTAssertEqual(value.candidate.interval.endedAt, "2026-11-08T12:00:01.000002Z")
        }
        let outside = try await api.candidateFrequencyRounding(sessionID: "rounding-one", candidateID: "one-outside", profileJSON: raw)
        XCTAssertEqual(outside.result.status, "outside_ranges"); XCTAssertNil(outside.result.nearestFrequencyHz)
    }
}
