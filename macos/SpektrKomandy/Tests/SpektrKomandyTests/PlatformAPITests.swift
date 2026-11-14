import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

@testable import SpektrKomandy

final class PlatformAPITests: XCTestCase {
    private var sessions: [URLSession] = []

    override func tearDown() {
        for session in sessions { session.finishTasksAndInvalidate() }
        sessions.removeAll()
        Stub.handler = nil
        super.tearDown()
    }

    final class Stub: URLProtocol {
        static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do {
                guard let (response, data) = try Self.handler?(bufferedTestRequest(request)) else {
                    throw PlatformError.connection("Нет заглушки")
                }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
        override func stopLoading() {}
    }

    private func client(
        _ url: String = "https://example.test/root/api/v1/",
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> PlatformClient {
        Stub.handler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        let session = URLSession(configuration: configuration)
        sessions.append(session)
        return try PlatformClient(baseURL: url, session: session)
    }

    private func response(_ url: URL, status: Int = 200) -> HTTPURLResponse {
        HTTPURLResponse(url: url, statusCode: status, httpVersion: nil, headerFields: nil)!
    }

    private func envelope(_ data: String) -> Data {
        Data("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(data)}".utf8)
    }

    private let sessionJSON = """
    {"id":"session/old","name":"Обзор","state":"running","streams":[
      {"id":"tiny/sa","device":"tinySA Ultra","role":"spectrum_analyzer","level_unit":"dBm","state":"connected","capabilities":["sweep"]},
      {"id":"ar/dv1","device":"AR-DV1","role":"receiver","level_unit":"s_meter_count","state":"connected","capabilities":["tune"]}
    ]}
    """

    private func capacityJSON(
        state: String = "warning", storage: String = "sqlite", size: String = "900",
        limit: String = "1000", remaining: String = "100", blocked: Bool = false
    ) -> String {
        """
        {"storage_mode":"\(storage)","database_bytes":\(size),"max_database_bytes":\(limit),"remaining_bytes":\(remaining),"state":"\(state)","warning_threshold":0.9,"measurement_writes_blocked":\(blocked),"automatic_deletion":false}
        """
    }

    func testArchiveCapacityUsesGlobalEndpointAndDecodesAllStates() async throws {
        for state in ["unlimited", "ok", "warning", "full", "unavailable"] {
            let limited = ["ok", "warning", "full"].contains(state)
            let size: Int64? = state == "unavailable" ? nil : (state == "full" ? 1000 : (state == "ok" ? 500 : 900))
            let remaining = limited ? 1000 - (size ?? 0) : nil
            let body = capacityJSON(
                state: state, storage: state == "unavailable" ? "memory" : "sqlite",
                size: size.map(String.init) ?? "null",
                limit: limited ? "1000" : "null", remaining: remaining.map(String.init) ?? "null",
                blocked: state == "full"
            )
            let client = try client { request in
                XCTAssertEqual(request.httpMethod, "GET")
                XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/archive/capacity")
                XCTAssertNil(request.httpBody)
                return (self.response(request.url!), self.envelope(body))
            }
            let capacity = try await client.archiveCapacity()
            XCTAssertEqual(capacity.state.rawValue, state)
            XCTAssertEqual(capacity.storageMode, state == "unavailable" ? .memory : .sqlite)
            XCTAssertEqual(capacity.databaseBytes, size)
            XCTAssertEqual(capacity.maxDatabaseBytes, limited ? 1000 : nil)
            XCTAssertEqual(capacity.remainingBytes, remaining)
            XCTAssertEqual(capacity.measurementWritesBlocked, state == "full")
            XCTAssertEqual(capacity.warningThreshold, 0.9)
            XCTAssertFalse(capacity.automaticDeletion)
        }
    }

    func testArchiveCapacityRejectsMalformedNumbersAndState() async throws {
        for body in [capacityJSON(size: "-1"), capacityJSON(size: "0.5"),
                     capacityJSON(limit: "-10"), capacityJSON(remaining: "-1"),
                     capacityJSON(state: "unknown"),
                     capacityJSON().replacingOccurrences(of: "0.9", with: "1.9")] {
            let client = try client { request in
                (self.response(request.url!), self.envelope(body))
            }
            await XCTAssertThrowsPlatformError(try await client.archiveCapacity())
        }
    }

    private var unsupportedCapacityPolicies: [String] {
        [capacityJSON().replacingOccurrences(of: "\"automatic_deletion\":false", with: "\"automatic_deletion\":true"),
         capacityJSON().replacingOccurrences(of: "0.9", with: "0.8")]
    }

    private var incompleteLimitedCapacities: [String] {
        ["ok", "warning", "full"].flatMap { state in
            let valid = capacityJSON(state: state, blocked: state == "full")
            return [
                capacityJSON(state: state, size: "null"),
                capacityJSON(state: state, limit: "null"),
                capacityJSON(state: state, remaining: "null"),
                capacityJSON(state: state, limit: "0"),
                capacityJSON(state: state, storage: "memory"),
                valid.replacingOccurrences(of: "\"database_bytes\":900,", with: ""),
                valid.replacingOccurrences(of: "\"max_database_bytes\":1000,", with: ""),
                valid.replacingOccurrences(of: "\"remaining_bytes\":100,", with: ""),
            ]
        }
    }

    func testArchiveCapacityRejectsUnsupportedPolicy() async throws {
        for body in unsupportedCapacityPolicies {
            let client = try client { request in
                (self.response(request.url!), self.envelope(body))
            }
            await XCTAssertThrowsPlatformError(try await client.archiveCapacity())
        }
    }

    func testArchiveCapacityRequiresCompleteLimitedSQLiteCounts() async throws {
        for body in incompleteLimitedCapacities {
            let client = try client { request in
                (self.response(request.url!), self.envelope(body))
            }
            await XCTAssertThrowsPlatformError(try await client.archiveCapacity())
        }
        // Nullable does not mean optional: all byte-count keys must still be present.
        let unavailable = capacityJSON(state: "unavailable", storage: "memory", size: "null",
                                       limit: "null", remaining: "null")
        for key in ["database_bytes", "max_database_bytes", "remaining_bytes"] {
            let body = unavailable.replacingOccurrences(of: "\"\(key)\":null,", with: "")
            let client = try client { request in
                (self.response(request.url!), self.envelope(body))
            }
            await XCTAssertThrowsPlatformError(try await client.archiveCapacity())
        }
    }

    func testArchiveCapacitySnapshotIsolatesOldServerAndMalformedResponse() async throws {
        let cases = [(200, "{}"), (404, "{}"), (500, "{}")]
            + (unsupportedCapacityPolicies + incompleteLimitedCapacities).map { (200, $0) }
        for (status, body) in cases {
            let client = try client { request in
                let path = request.url!.path
                if path.hasSuffix("/archive/capacity") {
                    return (self.response(request.url!, status: status),
                            status == 200 ? self.envelope(body) :
                            Data(#"{"error":{"code":"unavailable","message":"capacity unavailable"}}"#.utf8))
                }
                if path.hasSuffix("/workers") || path.hasSuffix("/sweeps") || path.hasSuffix("/observations")
                    || path.hasSuffix("/candidates") || path.hasSuffix("/investigations") {
                    return (self.response(request.url!), self.envelope("[]"))
                }
                if path.hasSuffix("/investigations/next") {
                    return (self.response(request.url!), self.envelope("null"))
                }
                if path.hasSuffix("/task-scheduler") {
                    return (self.response(request.url!), self.envelope(#"{"enabled":false,"scheduler_alive":false,"policy_persistent":false,"restart_requires_opt_in":true,"runtime":{},"tasks":{"total":0,"running":0,"completed":0}}"#))
                }
                if path.hasSuffix("/task-scheduler/tasks") {
                    return (self.response(request.url!), self.envelope(#"{"items":[],"count":0}"#))
                }
                if path.contains("/automatic-") {
                    return (self.response(request.url!), self.envelope(#"{"policy":{"enabled":false},"runtime":{},"scheduler_alive":false,"policy_persistent":false,"restart_requires_opt_in":true}"#))
                }
                return (self.response(request.url!), self.envelope(self.sessionJSON))
            }
            let snapshot = try await client.snapshot(sessionID: "session/old")
            XCTAssertEqual(snapshot.session.id, "session/old")
            XCTAssertTrue(snapshot.sweeps.isEmpty)
            XCTAssertTrue(snapshot.observations.isEmpty)
            XCTAssertTrue(snapshot.workflowErrors.isEmpty)
            XCTAssertNotNil(snapshot.taskScheduler)
            XCTAssertNil(snapshot.taskSchedulerError)
            XCTAssertNil(snapshot.taskSchedulerTasksError)
            XCTAssertNil(snapshot.archiveCapacity)
            XCTAssertNotNil(snapshot.archiveCapacityError)
        }
    }

    func testArchiveCapacityIncludedInSnapshotOrExplicitlyPolledSeparately() async throws {
        var capacityRequests = 0
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/archive/capacity") {
                capacityRequests += 1
                return (self.response(request.url!), self.envelope(self.capacityJSON()))
            }
            if path.hasSuffix("/workers") || path.hasSuffix("/sweeps") || path.hasSuffix("/observations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            return (self.response(request.url!), self.envelope(self.sessionJSON))
        }
        let snapshot = try await client.snapshot(sessionID: "session/old")
        XCTAssertEqual(snapshot.archiveCapacity?.state, .warning)
        XCTAssertNil(snapshot.archiveCapacityError)
        let separate = try await client.snapshot(sessionID: "session/old", includeArchiveCapacity: false)
        XCTAssertNil(separate.archiveCapacity)
        XCTAssertNil(separate.archiveCapacityError)
        XCTAssertEqual(capacityRequests, 1)
    }

    func testBaseURLNormalizationAndValidation() throws {
        XCTAssertEqual(
            try PlatformBaseURL(" https://example.test/root/api/v1/ ").root.absoluteString,
            "https://example.test/root"
        )
        XCTAssertEqual(
            try PlatformBaseURL("https://example.test/").endpoint(path: "/sessions").absoluteString,
            "https://example.test/api/v1/sessions"
        )
        XCTAssertThrowsError(try PlatformBaseURL("127.0.0.1:8770"))
        XCTAssertThrowsError(try PlatformBaseURL("http://example.test"))
        XCTAssertThrowsError(try PlatformBaseURL("http://user:secret@example.test"))
        XCTAssertThrowsError(try PlatformBaseURL("https://example.test/?token=x"))
        XCTAssertEqual(
            try PlatformBaseURL("http://127.0.0.1:8770").root.absoluteString,
            "http://127.0.0.1:8770"
        )
    }

    func testListSessionsChecksEnvelopeAndDecodesRawUnits() async throws {
        let client = try client { request in
            XCTAssertEqual(request.url?.absoluteString, "https://example.test/root/api/v1/sessions")
            return (self.response(request.url!), self.envelope("[\(self.sessionJSON)]"))
        }
        let sessions = try await client.listSessions()
        XCTAssertEqual(sessions.first?.streams.first?.levelUnit, "dBm")
        XCTAssertEqual(sessions.first?.streams.last?.levelUnit, "s_meter_count")
        XCTAssertTrue(sessions.first?.streams.first?.isDbmAnalyzer == true)
    }

    func testSimulatorOptInIsExplicitAndProfileHasNoClientPort() async throws {
        for simulated in [true, false] {
            let client = try client { request in
                XCTAssertEqual(request.httpMethod, "POST")
                let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                let streams = body["streams"] as! [[String: Any]]
                if simulated {
                    XCTAssertEqual((streams[0]["settings"] as? [String: String])?["adapter"], "tinysa-sim")
                    XCTAssertEqual(streams[1]["port"] as? String, "sim://ardv1")
                } else {
                    XCTAssertNil(streams[0]["settings"])
                    XCTAssertNil(streams[0]["port"])
                    XCTAssertNil(streams[1]["port"])
                }
                return (self.response(request.url!), self.envelope(self.sessionJSON))
            }
            _ = try await client.createSession(name: " Обзор ", simulated: simulated)
        }
    }

    func testSerialProfileUsesExplicitAdaptersAndDistinctPorts() async throws {
        let client = try client { request in
            let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            let streams = body["streams"] as! [[String: Any]]
            XCTAssertEqual(streams[0]["port"] as? String, "/dev/ttyACM0")
            XCTAssertEqual(streams[1]["port"] as? String, "/dev/ttyUSB0")
            XCTAssertEqual((streams[0]["settings"] as? [String: Any])?["adapter"] as? String, "tinysa-serial")
            XCTAssertEqual((streams[1]["settings"] as? [String: Any])?["adapter"] as? String, "ardv1-serial")
            XCTAssertFalse((streams[1]["capabilities"] as? [String])?.contains("hardware_decode") ?? true)
            return (self.response(request.url!), self.envelope(self.sessionJSON))
        }
        _ = try await client.createSession(
            name: " Serial ", tinySAPort: "/dev/ttyACM0", receiverPort: "/dev/ttyUSB0"
        )
        await XCTAssertThrowsPlatformError(
            try await client.createSession(
                name: "Bad", tinySAPort: "sim://tinysa", receiverPort: "/dev/ttyUSB0"
            )
        )
        await XCTAssertThrowsPlatformError(
            try await client.createSession(
                name: "Bad", tinySAPort: "/dev/same", receiverPort: "/dev/same"
            )
        )
    }

    func testPersonalRCUsesAnExplicitReceiveRangeAndVerifiedReceiverProfile() async throws {
        let configuration = try PlatformSerialReceiveConfiguration(startHz: 430_000_000, stopHz: 440_000_000, points: 290, rbwHz: 30_000)
        let client = try client { request in
            let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            let streams = body["streams"] as! [[String: Any]]
            let receive = streams[0]["settings"] as! [String: Any]
            XCTAssertEqual(receive["start_hz"] as? Int, 430_000_000)
            XCTAssertEqual(receive["stop_hz"] as? Int, 440_000_000)
            XCTAssertEqual(receive["points"] as? Int, 290)
            XCTAssertEqual(receive["rbw_hz"] as? Int, 30_000)
            XCTAssertEqual((streams[1]["settings"] as? [String: Any])?["receiver_profile"] as? String, "ardv1-monitor-v1")
            return (self.response(request.url!), self.envelope(self.sessionJSON))
        }
        _ = try await client.createSerialSession(name: "RC", tinySAPort: "/dev/cu.tiny", ardv1Port: "/dev/cu.aor", receive: configuration, monitorReceiverProfile: true)
        XCTAssertThrowsError(try PlatformSerialReceiveConfiguration(startHz: 440_000_000, stopHz: 430_000_000, points: 290, rbwHz: 30_000))
        XCTAssertThrowsError(try PlatformSerialReceiveConfiguration(startHz: 100_000, stopHz: 801_000_000, points: 290, rbwHz: 10_000))
        XCTAssertThrowsError(try PlatformSerialReceiveConfiguration(startHz: 100_000, stopHz: 800_000_000, points: 291, rbwHz: 10_000))
        XCTAssertThrowsError(try PlatformSerialReceiveConfiguration(startHz: 100_000, stopHz: 800_000_000, points: 290, rbwHz: 20_000))
    }

    func testTuneEncodesBothIDsAndUsesIntegerHertz() async throws {
        let client = try client { request in
            XCTAssertEqual(
                request.url?.absoluteString,
                "https://example.test/root/api/v1/sessions/session%2Fold/streams/ar%2Fdv1/tune"
            )
            let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
            XCTAssertEqual(body["frequency_hz"] as? Int, 433075000)
            XCTAssertEqual(body["mode"] as? String, "DMR")
            let result = """
            {"tuned":{"frequency_hz":433075000,"mode":"DMR"},"worker":{"stream_id":"ar/dv1","state":"running","alive":true,"last_error":null}}
            """
            return (self.response(request.url!), self.envelope(result))
        }
        let tuned = try await client.tune(
            sessionID: "session/old", streamID: "ar/dv1", frequencyHz: 433075000, mode: " DMR "
        )
        XCTAssertEqual(tuned.tuned.frequencyHz, 433075000)
        await XCTAssertThrowsPlatformError(
            try await client.tune(sessionID: "s", streamID: "r", frequencyHz: 0, mode: "FM")
        )
    }

    func testSessionPathsRetainDistinctUTF8SpellingsWithoutChangingReturnedIDs() async throws {
        var paths: [String] = []; let lock = NSLock()
        let client = try client { request in
            let path = request.url!.absoluteString
            lock.withLock { paths.append(path) }
            let id = path.hasSuffix("%C3%A9") ? "é" : "e\u{301}"
            return (self.response(request.url!), self.envelope("{\"id\":\"" + id + "\",\"name\":\"synthetic\",\"state\":\"running\",\"streams\":[]}"))
        }
        let composed = try await client.getSession("é"), decomposed = try await client.getSession("e\u{301}")
        XCTAssertEqual(Array(composed.id.utf8), [195, 169]); XCTAssertEqual(Array(decomposed.id.utf8), [101, 204, 129])
        XCTAssertEqual(lock.withLock { paths }, ["https://example.test/root/api/v1/sessions/%C3%A9", "https://example.test/root/api/v1/sessions/e%CC%81"])
    }

    func testStreamingMetadataSnapshotDoesNotRefetchMeasurements() async throws {
        let lock = NSLock(); var measurementReads = 0
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/sweeps") || path.hasSuffix("/observations") {
                lock.withLock { measurementReads += 1 }
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/workers") || path.hasSuffix("/candidates") || path.hasSuffix("/investigations") { return (self.response(request.url!), self.envelope("[]")) }
            if path.hasSuffix("/investigations/next") { return (self.response(request.url!), self.envelope("null")) }
            if path.hasSuffix("/sessions/session/old") { return (self.response(request.url!), self.envelope(self.sessionJSON)) }
            if request.url!.absoluteString.hasSuffix("/sessions/session%2Fold") { return (self.response(request.url!), self.envelope(self.sessionJSON)) }
            return (self.response(request.url!, status: 404), self.envelope("{}"))
        }
        let snapshot = try await client.snapshot(sessionID: "session/old", includeArchiveCapacity: false, includeMeasurements: false)
        XCTAssertEqual(snapshot.session.id, "session/old")
        XCTAssertTrue(snapshot.sweeps.isEmpty); XCTAssertTrue(snapshot.observations.isEmpty)
        XCTAssertEqual(lock.withLock { measurementReads }, 0)
    }

    func testSnapshotKeepsSweepAndObservationSeparate() async throws {
        let client = try client { request in
            let path = request.url!.path
            if path.hasSuffix("/workers") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/sweeps") {
                XCTAssertEqual(request.url?.query, "stream_id=tiny%2Fsa&limit=40")
                let sweep = """
                [{"id":"sw1","session_id":"session/old","stream_id":"tiny/sa","interval":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"},"frequencies_hz":[100000000,100100000],"levels":[-80.5,-61.25],"level_unit":"dBm"}]
                """
                return (self.response(request.url!), self.envelope(sweep))
            }
            if path.hasSuffix("/observations") {
                let observation = """
                [{"id":"ob1","session_id":"session/old","stream_id":"ar/dv1","interval":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"},"frequency_hz":433075000,"mode":"DMR","level_unit":"s_meter_count","level":42,"squelch_open":true,"decoded_mode":"DMR"}]
                """
                return (self.response(request.url!), self.envelope(observation))
            }
            if path.hasSuffix("/candidates") || path.hasSuffix("/investigations") {
                return (self.response(request.url!), self.envelope("[]"))
            }
            if path.hasSuffix("/investigations/next") {
                return (self.response(request.url!), self.envelope("null"))
            }
            if path.hasSuffix("/automatic-handoff") || path.hasSuffix("/automatic-detection") {
                let status = "{\"policy\":{\"enabled\":false},\"runtime\":{},\"scheduler_alive\":false,\"policy_persistent\":false,\"restart_requires_opt_in\":true}"
                return (self.response(request.url!), self.envelope(status))
            }
            return (self.response(request.url!), self.envelope(self.sessionJSON))
        }
        let snapshot = try await client.snapshot(sessionID: "session/old")
        XCTAssertEqual(snapshot.sweeps[0].levels[1], -61.25)
        XCTAssertEqual(snapshot.observations[0].level, 42)
        XCTAssertEqual(snapshot.observations[0].levelUnit, "s_meter_count")
    }

    func testRejectsWrongContractServerErrorAndInvalidSweep() async throws {
        let wrong = try client { request in
            (self.response(request.url!), Data("{\"contract\":\"other\",\"contract_version\":1,\"data\":[]}".utf8))
        }
        await XCTAssertThrowsPlatformError(try await wrong.listSessions())

        let failure = try client { request in
            let data = Data("{\"error\":{\"code\":\"session_not_found\",\"message\":\"Нет сеанса\"}}".utf8)
            return (self.response(request.url!, status: 404), data)
        }
        do {
            _ = try await failure.listSessions()
            XCTFail("Ожидалась ошибка HTTP")
        } catch PlatformError.http(let status, let code, let message) {
            XCTAssertEqual(status, 404)
            XCTAssertEqual(code, "session_not_found")
            XCTAssertEqual(message, "Нет сеанса")
        }

        let malformed = try client { request in
            let body = "[{\"id\":\"sw\",\"session_id\":\"s\",\"stream_id\":\"tiny\",\"interval\":{\"started_at\":\"a\",\"ended_at\":\"b\"},\"frequencies_hz\":[2,1],\"levels\":[-60,-70],\"level_unit\":\"dBm\"}]"
            return (self.response(request.url!), self.envelope(body))
        }
        await XCTAssertThrowsPlatformError(
            try await malformed.listSweeps(sessionID: "s", streamID: "tiny")
        )
    }

    func testRedirectGuardAndFinalOriginAreRejected() async throws {
        let client = try client { request in
            let redirected = HTTPURLResponse(
                url: URL(string: "http://127.0.0.1:8770/api/v1/sessions")!,
                statusCode: 307, httpVersion: nil, headerFields: nil
            )!
            return (redirected, self.envelope("[]"))
        }
        await XCTAssertThrowsPlatformError(try await client.listSessions())

        let original = URL(string: "https://example.test/api/v1/sessions")!
        let session = URLSession(configuration: .ephemeral)
        sessions.append(session)
        let task = session.dataTask(with: original)
        let response = HTTPURLResponse(
            url: original, statusCode: 307, httpVersion: nil, headerFields: nil
        )!
        let completed = expectation(description: "redirect rejected")
        PlatformRedirectGuard().urlSession(
            session, task: task, willPerformHTTPRedirection: response,
            newRequest: URLRequest(url: URL(string: "http://127.0.0.1:8770/api/v1/sessions")!)
        ) { request in
            XCTAssertNil(request)
            completed.fulfill()
        }
        await fulfillment(of: [completed], timeout: 1)
    }

    func testSupportedSweepHistoryFitsResponseBudget() async throws {
        let frequencies = (100000000..<100004096).map(String.init).joined(separator: ",")
        let levels = Array(repeating: "-112.345678", count: 4096).joined(separator: ",")
        let row = """
        {"id":"sw","session_id":"s","stream_id":"tiny","interval":{"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-01-01T00:00:01Z"},"frequencies_hz":[\(frequencies)],"levels":[\(levels)],"level_unit":"dBm"}
        """
        let payload = envelope("[" + Array(repeating: row, count: 40).joined(separator: ",") + "]")
        XCTAssertGreaterThan(payload.count, 2_000_000)
        let api = try client { request in (self.response(request.url!), payload) }
        let result = try await api.listSweeps(sessionID: "s", streamID: "tiny")
        XCTAssertEqual(result.count, 40)
        XCTAssertEqual(result[0].levels.count, 4096)
    }

    func testIPv6LoopbackKeepsHTTPExceptionNarrow() throws {
        XCTAssertNoThrow(try PlatformBaseURL("http://[::1]:8770"))
        XCTAssertThrowsError(try PlatformBaseURL("http://[2001:db8::1]:8770"))
        XCTAssertThrowsError(try PlatformBaseURL("http://[::ffff:192.0.2.1]:8770"))
    }

    func testResponseDelegateCompletesCancellationAcrossExecutorsOnce() {
        final class Counter: @unchecked Sendable {
            let lock = NSLock()
            var value = 0
            func increment() { lock.lock(); value += 1; lock.unlock() }
        }
        let session = URLSession(configuration: .ephemeral)
        sessions.append(session)
        let task = session.dataTask(with: URL(string: "https://example.test")!)
        for _ in 0..<1000 {
            let delegate = PlatformResponseDelegate(maximumBytes: 16)
            let counter = Counter()
            delegate.completion = { _ in counter.increment() }
            DispatchQueue.concurrentPerform(iterations: 16) { index in
                if index % 2 == 0 { delegate.cancel() }
                else { delegate.urlSession(session, task: task, didCompleteWithError: CancellationError()) }
            }
            XCTAssertEqual(counter.value, 1)
        }
    }

    func testResponseDelegateRetainsCancellationUntilCallbackInstallation() {
        let delegate = PlatformResponseDelegate(maximumBytes: 16)
        delegate.cancel()
        var calls = 0
        delegate.completion = { _ in calls += 1 }
        XCTAssertEqual(calls, 1)
        delegate.cancel()
        XCTAssertEqual(calls, 1)
    }

    func testResponseDelegateFinishesCancellationOnlyOnce() {
        let delegate = PlatformResponseDelegate(maximumBytes: 16)
        var completions = 0
        delegate.completion = { _ in completions += 1 }
        let session = URLSession(configuration: .ephemeral)
        sessions.append(session)
        let task = session.dataTask(
            with: URLRequest(url: URL(string: "https://example.test/api/v1/sessions")!)
        )
        delegate.cancel()
        delegate.urlSession(session, task: task, didCompleteWithError: CancellationError())
        XCTAssertEqual(completions, 1)
    }

    func testDownloadDelegateRetainsFileAndFinishesOnlyOnce() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let temporary = folder.appendingPathComponent("system-download")
        let staged = folder.appendingPathComponent("owned-download")
        try Data("snapshot\n".utf8).write(to: temporary)
        let delegate = PlatformDownloadDelegate(stagingURL: staged)
        var completions = 0
        delegate.completion = { _, location, error in
            completions += 1
            XCTAssertEqual(location, staged)
            XCTAssertNil(error)
        }
        let session = URLSession(configuration: .ephemeral)
        sessions.append(session)
        let task = session.downloadTask(with: URL(string: "https://example.test/archive")!)
        delegate.urlSession(session, downloadTask: task, didFinishDownloadingTo: temporary)
        delegate.urlSession(session, task: task, didCompleteWithError: nil)
        XCTAssertEqual(completions, 1)
        XCTAssertEqual(try String(contentsOf: staged, encoding: .utf8), "snapshot\n")
        XCTAssertFalse(FileManager.default.fileExists(atPath: temporary.path))
    }

    private var cancellationHTTPFixtureScript: String {
        """
import http.server, os, socket, socketserver, sys, threading, time
LOG = sys.argv[1]
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(LOG, "a", encoding="utf-8") as stream:
            stream.write("get\\n")
        time.sleep(2)
        body = b'{"contract":"openradioinspector-platform","contract_version":1,"data":[]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
class LoopbackServer(http.server.ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
if len(sys.argv) > 2 and sys.argv[2] == "reject-reverse-dns":
    def reject_reverse_dns(*args):
        raise AssertionError("Synthetic HTTP fixture must not request reverse DNS")
    socket.getfqdn = reject_reverse_dns
server = LoopbackServer(("127.0.0.1", 0), Handler)
server.timeout = 0.1
stopped = threading.Event()
stdin_errors = []
def stop_at_eof():
    try:
        while os.read(0, 4096):
            pass
    except OSError as error:
        stdin_errors.append(error)
    finally:
        stopped.set()
threading.Thread(target=stop_at_eof, daemon=True).start()
print(server.server_address[1], flush=True)
try:
    while not stopped.is_set():
        server.handle_request()
finally:
    server.server_close()
if stdin_errors:
    raise stdin_errors[0]
"""
    }

    func testCancellationHTTPFixtureStopsWhenOwnerClosesInput() throws {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("ori-cancel-owner-\(UUID().uuidString).log").path
        let process = Process()
        let output = Pipe()
        let lifetime = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-c", cancellationHTTPFixtureScript, logPath, "reject-reverse-dns"]
        process.standardOutput = output
        process.standardInput = lifetime
        try process.run()
        defer {
            try? lifetime.fileHandleForWriting.close()
            if process.isRunning { process.terminate() }
            process.waitUntilExit()
            try? output.fileHandleForReading.close()
            try? FileManager.default.removeItem(atPath: logPath)
        }
        try lifetime.fileHandleForReading.close()
        try output.fileHandleForWriting.close()
        var readiness = pollfd(
            fd: output.fileHandleForReading.fileDescriptor,
            events: Int16(POLLIN), revents: 0
        )
        guard poll(&readiness, 1, 5000) == 1 else {
            XCTFail("Owned HTTP fixture did not start")
            return
        }
        let port = output.fileHandleForReading.availableData
        XCTAssertNotNil(Int(String(decoding: port, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)))
        try lifetime.fileHandleForWriting.close()
        let deadline = Date().addingTimeInterval(5)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.02)
        }
        XCTAssertFalse(process.isRunning, "HTTP fixture outlived its owner's input pipe")
        if !process.isRunning {
            process.waitUntilExit()
            XCTAssertEqual(process.terminationStatus, 0)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: logPath))
    }

    func testDefaultClientCancellationStopsRealHTTPRequest() async throws {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("ori-cancel-\(UUID().uuidString).log").path
        let script = cancellationHTTPFixtureScript
        // Darwin forbids fork() from Swift; preserve the Linux harness and spawn safely on macOS.
        #if canImport(Darwin)
        let process = Process()
        let output = Pipe()
        let lifetime = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-c", script, logPath]
        process.standardOutput = output
        process.standardInput = lifetime
        try process.run()
        defer {
            try? lifetime.fileHandleForWriting.close()
            if process.isRunning { process.terminate() }
            process.waitUntilExit()
            try? output.fileHandleForReading.close()
        }
        try lifetime.fileHandleForReading.close()
        try output.fileHandleForWriting.close()
        let descriptors = [output.fileHandleForReading.fileDescriptor]
        #else
        var descriptors: [Int32] = [-1, -1]
        var lifetime: [Int32] = [-1, -1]
        guard pipe(&descriptors) == 0 else {
            XCTFail("Unable to create owned output pipe")
            return
        }
        guard pipe(&lifetime) == 0 else {
            close(descriptors[0])
            close(descriptors[1])
            XCTFail("Unable to create owned lifetime pipe")
            return
        }
        let arguments = ["env", "python3", "-c", script, logPath].map {
            $0.withCString { strdup($0) }
        }
        var argv = arguments + [nil]
        let pid = fork()
        if pid == 0 {
            close(descriptors[0])
            close(lifetime[1])
            if dup2(lifetime[0], STDIN_FILENO) < 0 { _exit(126) }
            close(lifetime[0])
            if dup2(descriptors[1], STDOUT_FILENO) < 0 { _exit(126) }
            close(descriptors[1])
            argv.withUnsafeMutableBufferPointer { buffer in
                _ = execv("/usr/bin/env", buffer.baseAddress!)
            }
            _exit(127)
        }
        for argument in arguments { free(argument) }
        guard pid > 0 else {
            close(descriptors[0])
            close(descriptors[1])
            close(lifetime[0])
            close(lifetime[1])
            XCTFail("Не удалось запустить HTTP-сервер")
            return
        }
        close(descriptors[1])
        close(lifetime[0])
        defer {
            close(lifetime[1])
            close(descriptors[0])
            kill(pid, SIGKILL)
            _ = waitpid(pid, nil, 0)
        }
        #endif
        defer { try? FileManager.default.removeItem(atPath: logPath) }
        var readiness = pollfd(fd: descriptors[0], events: Int16(POLLIN), revents: 0)
        guard poll(&readiness, 1, 5000) == 1 else {
            XCTFail("HTTP-сервер не сообщил готовность за 5 секунд")
            return
        }
        var buffer = [UInt8](repeating: 0, count: 64)
        let count = read(descriptors[0], &buffer, buffer.count)
        let portText = String(decoding: buffer.prefix(max(0, Int(count))), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard count > 0, let port = Int(portText) else {
            XCTFail("HTTP-сервер не сообщил порт")
            return
        }
        let client = try PlatformClient(baseURL: "http://127.0.0.1:\(port)")
        let started = Date()
        let request = Task { try await client.listSessions() }
        try? await Task.sleep(nanoseconds: 100_000_000)
        request.cancel()
        do {
            _ = try await request.value
            XCTFail("Запрос должен быть отменён")
        } catch is CancellationError {
        } catch PlatformError.connection(_) {
        }
        XCTAssertLessThan(Date().timeIntervalSince(started), 1.5)

        let alreadyCancelled = Task { try await client.listSessions() }
        alreadyCancelled.cancel()
        do {
            _ = try await alreadyCancelled.value
            XCTFail("Уже отменённый вызов не должен стартовать запрос")
        } catch is CancellationError {
        } catch PlatformError.connection(_) {
        }
        try? await Task.sleep(nanoseconds: 300_000_000)
        let log = (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
        let requests = log.split(separator: "\n").filter { !$0.isEmpty }
        XCTAssertEqual(requests.count, 1, "Запрос должен быть только первый, отменённый во flight")
    }

    private func XCTAssertThrowsPlatformError<T>(
        _ expression: @autoclosure () async throws -> T,
        file: StaticString = #filePath, line: UInt = #line
    ) async {
        do {
            _ = try await expression()
            XCTFail("Ожидалась PlatformError", file: file, line: line)
        } catch is PlatformError {
        } catch {
            XCTFail("Другая ошибка: \(error)", file: file, line: line)
        }
    }
}

extension PlatformAPITests {
    func testScopedClientRemainsUsableAfterOriginalClientIsReleased() async throws {
        var original: PlatformClient? = try client("http://127.0.0.1:8772") { request in
            XCTAssertTrue(request.url!.path.hasPrefix("/api/v1/instances/owner-A/"))
            let data = request.url!.path.hasSuffix("/health") ? self.envelope("{\"runtime_instance_id\":\"owner-A\"}") : self.envelope("[]")
            return (self.response(request.url!), data)
        }
        weak var releasedOriginal = original
        let bound = try original!.scoped(toRuntimeInstance: "owner-A")
        original = nil
        XCTAssertNil(releasedOriginal)
        try await bound.verifyRuntimeOwner()
        let sessions = try await bound.listSessions()
        XCTAssertTrue(sessions.isEmpty)
    }

    func testReleasingScopedClientDoesNotInvalidateOriginalClient() async throws {
        let original = try client("http://127.0.0.1:8772") { request in
            XCTAssertEqual(request.url!.path, "/api/v1/sessions")
            return (self.response(request.url!), self.envelope("[]"))
        }
        var bound: PlatformClient? = try original.scoped(toRuntimeInstance: "owner-A")
        weak var releasedBound = bound
        bound = nil
        XCTAssertNil(releasedBound)
        let sessions = try await original.listSessions()
        XCTAssertTrue(sessions.isEmpty)
    }

    func testRuntimeScopePinsEveryEndpointAndRejectsAReplacementHealthIdentity() async throws {
        let api = try client("http://127.0.0.1:8772") { request in
            let url = request.url!
            XCTAssertTrue(url.path.hasPrefix("/api/v1/instances/owner-A/"))
            let data = url.path.hasSuffix("/health") ? self.envelope("{\"runtime_instance_id\":\"owner-A\"}") : self.envelope("[]")
            return (self.response(url), data)
        }
        let bound = try api.scoped(toRuntimeInstance: "owner-A")
        try await bound.verifyRuntimeOwner()
        _ = try await bound.listSessions()
        XCTAssertEqual(bound.baseURL.runtimeInstanceID, "owner-A")
        let replacement = try client("http://127.0.0.1:8772") { request in
            (self.response(request.url!), self.envelope("{\"runtime_instance_id\":\"owner-B\"}"))
        }.scoped(toRuntimeInstance: "owner-A")
        do { try await replacement.verifyRuntimeOwner(); XCTFail("A replacement peer must not become an accepted client") }
        catch PlatformError.invalidResponse { }
        for invalid in ["", "../owner", "owner/other", "a%2Fb"] {
            XCTAssertThrowsError(try PlatformBaseURL("http://127.0.0.1:8772", runtimeInstanceID: invalid))
        }
    }
}
