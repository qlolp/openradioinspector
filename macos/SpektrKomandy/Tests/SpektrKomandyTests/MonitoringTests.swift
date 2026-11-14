import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class MonitoringTests: XCTestCase {
    private var sessions: [URLSession] = []

    override func tearDown() {
        sessions.forEach { $0.invalidateAndCancel() }
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
                guard let (response, body) = try Self.handler?(bufferedTestRequest(request)) else {
                    throw PlatformError.connection("Нет заглушки")
                }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: body)
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
        override func stopLoading() {}
    }

    private func makeClient(_ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)) throws -> PlatformClient {
        Stub.handler = handler
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        sessions.append(session)
        return try PlatformClient(baseURL: "https://example.test/root", session: session)
    }

    private func response(_ request: URLRequest, status: Int = 200, mime: String = "application/json") -> HTTPURLResponse {
        HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": mime])!
    }

    private func envelope(_ json: String) -> Data {
        Data("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(json)}".utf8)
    }

    private var wav: Data {
        // Mono PCM16, 24 kHz, one sample, with a complete RIFF length.
        Data([82,73,70,70,38,0,0,0,87,65,86,69,102,109,116,32,16,0,0,0,
              1,0,1,0,192,93,0,0,128,187,0,0,2,0,16,0,100,97,116,97,2,0,0,0,0,0])
    }
    func testIntegerHzAndNestedMetadataRoundTripWithoutLosingPrecision() throws {
        let bytes = Data("{\"frequency_hz\":9007199254740991,\"items\":[true,null,\"Объект\"]}".utf8)
        let value = try JSONDecoder().decode(MonitoringValue.self, from: bytes)
        XCTAssertEqual(value["frequency_hz"], .integer(9007199254740991))
        XCTAssertEqual(value["items"].items, [.boolean(true), .null, .string("Объект")])
        XCTAssertEqual(try JSONDecoder().decode(MonitoringValue.self, from: JSONEncoder().encode(value)), value)
    }
    func testUnknownFieldsAndEmptyListsRemainReadable() throws {
        let value = try JSONDecoder().decode(MonitoringValue.self, from: Data("{\"items\":[],\"next_cursor\":null,\"future\":{\"version\":2}}".utf8))
        XCTAssertTrue(value["items"].items.isEmpty)
        XCTAssertEqual(value["future"]["version"], .integer(2))
        XCTAssertFalse(value.pretty.isEmpty)
    }

    func testProjectListRequestsBoundedPagesAndEncodesCursor() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(request.httpBody)
            XCTAssertTrue(request.url!.path.hasSuffix("/monitoring-projects"))
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            XCTAssertEqual(query.first { $0.name == "limit" }?.value, "200")
            XCTAssertEqual(query.first { $0.name == "after" }?.value, "last/id+cursor==")
            return (self.response(request), self.envelope(#"{"items":[{"id":"later-project"}],"next_cursor":"next-page-id"}"#))
        }
        let result = try await client.monitoringProjects(after: "last/id+cursor==")
        XCTAssertEqual(result["items"].items.first?["id"].text, "later-project")
        XCTAssertEqual(result["next_cursor"].text, "next-page-id")
    }

    func testProjectListRejectsInvalidLimitsAndOversizedPages() async throws {
        let client = try makeClient { request in
            let value: MonitoringValue = .object(["items": .array(Array(repeating: .object(["id": .string("p")]), count: 201))])
            return (self.response(request), try self.valueEnvelope(value))
        }
        for limit in [0, 201] {
            do { _ = try await client.monitoringProjects(limit: limit); XCTFail("Invalid limit must fail") }
            catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error") } }
        }
        do { _ = try await client.monitoringProjects(); XCTFail("Oversized page must fail") }
        catch let error as PlatformError { if case .invalidResponse = error {} else { XCTFail("Unexpected error") } }
    }

    func testProjectPagesKeepSelectedScopeAndCacheCursorsForPreviousNavigation() {
        func page(_ id: String, next: String? = nil) -> MonitoringValue {
            .object(["items": .array([.object(["id": .string(id)])]), "next_cursor": next.map(MonitoringValue.string) ?? .null])
        }
        var pages = MonitoringProjectPageState()
        pages.apply(page("p1", next: "first-last-id"), page: 0)
        XCTAssertEqual(pages.selectedID, "p1")
        XCTAssertEqual(pages.cursor(for: 1), "first-last-id")
        pages.apply(page("p201", next: "second-last-id"), page: 1)
        XCTAssertEqual(pages.selectedID, "p1")
        XCTAssertEqual(pages.items.map { $0["id"].text }, ["p1", "p201"])
        pages.select("p201")
        pages.apply(page("p401"), page: 2)
        XCTAssertEqual(pages.selectedID, "p201")
        XCTAssertEqual(pages.cursor(for: 1), "first-last-id")
        XCTAssertEqual(pages.cursor(for: 2), "second-last-id")
        XCTAssertNil(pages.cursor(for: 0))
        XCTAssertEqual(pages.nextCursor, "")
        pages.apply(page("p201", next: "second-last-id"), page: 1)
        XCTAssertEqual(pages.pageIndex, 1)
        XCTAssertEqual(pages.selectedID, "p201")
        pages.select("")
        pages.apply(page("p401"), page: 2)
        XCTAssertEqual(pages.selectedID, "", "Paging cannot choose an unselected project")
    }

    func testCreatedProjectRemainsSelectedOutsideFirstPageAndEndpointResetDropsCursors() {
        var pages = MonitoringProjectPageState()
        pages.apply(.object(["items": .array([.object(["id": .string("p1")])]), "next_cursor": .string("old-endpoint")]), page: 0)
        pages.created(.object(["id": .string("random-id-beyond-first-page")]))
        pages.apply(.object(["items": .array([.object(["id": .string("p1")])])]), page: 0)
        XCTAssertEqual(pages.selectedID, "random-id-beyond-first-page")
        XCTAssertEqual(pages.items.map { $0["id"].text }, ["random-id-beyond-first-page", "p1"])
        pages = MonitoringProjectPageState()
        XCTAssertEqual(pages.pageIndex, 0)
        XCTAssertEqual(pages.selectedID, "")
        XCTAssertEqual(pages.nextCursor, "")
        XCTAssertTrue(pages.items.isEmpty)
        XCTAssertNil(pages.cursor(for: 1))
    }

    func testProjectAndEventReadsUseTheirOwnResourcesAndPreserveSnakeCaseFields() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET")
            let path = request.url!.absoluteString
            if path.hasSuffix("/p/events/e") {
                return (self.response(request), self.envelope("{\"id\":\"e\",\"analysis\":{\"requested_mode\":\"AUTO\"}}"))
            }
            XCTAssertTrue(path.hasSuffix("/monitoring-projects/p/status"))
            return (self.response(request), self.envelope("{\"receiver_busy\":true,\"audio_configured\":false,\"project\":{\"id\":\"p\"}}"))
        }
        let status = try await client.monitoringProject("p", resource: "status")
        XCTAssertEqual(status["receiver_busy"].bool, true)
        XCTAssertEqual(status["audio_configured"].bool, false)
        let event = try await client.monitoringEvent(projectID: "p", eventID: "e")
        XCTAssertEqual(event["analysis"]["requested_mode"].text, "AUTO")
    }

    func testConfigureSendsOnlyExplicitAudioAssignmentAndMode() async throws {
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.path.hasSuffix("/monitoring-projects/p/configure"))
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            XCTAssertEqual(body["receiver_mode"], .string("AUTO"))
            XCTAssertEqual(body["audio"]["device"], .integer(4))
            XCTAssertEqual(body["audio"]["name"], .string("USB AR-DV1"))
            XCTAssertEqual(body["audio"]["confirm"], .boolean(true))
            return (self.response(request), self.envelope("{\"id\":\"p\"}"))
        }
        _ = try await client.monitoringProject("p", resource: "configure", body: [
            "receiver_mode": .string("AUTO"),
            "audio": .object(["device": .integer(4), "name": .string("USB AR-DV1"), "confirm": .boolean(true)])
        ])
    }

    func testConfigureWithoutAudioSendsExplicitNull() async throws {
        let client = try makeClient { request in
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            guard case .object(let values) = body else { throw PlatformError.invalidResponse("Expected object") }
            XCTAssertEqual(values["audio"], .null)
            return (self.response(request), self.envelope("{}"))
        }
        _ = try await client.monitoringProject("p", resource: "configure", body: ["receiver_mode": .string("AM"), "audio": .null])
    }

    func testAudioInputsUsesCoreListAndPreservesUnavailableReason() async throws {
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.path.hasSuffix("/monitoring-projects/audio-inputs"))
            return (self.response(request), self.envelope("{\"available\":false,\"devices\":[],\"reason\":\"Аудиосистема недоступна\"}"))
        }
        let inputs = try await client.monitoring("/audio-inputs")
        XCTAssertEqual(inputs["available"].bool, false)
        XCTAssertEqual(inputs["devices"].items, [])
        XCTAssertEqual(inputs["reason"].text, "Аудиосистема недоступна")
    }

    func testArmDisarmAndAllowedRequestsPreserveBodies() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            if request.url!.path.hasSuffix("/allowed") {
                XCTAssertEqual(body["watch_changes"], .boolean(true))
            } else {
                XCTAssertTrue(request.url!.path.hasSuffix("/arm") || request.url!.path.hasSuffix("/disarm"))
                XCTAssertEqual(body, .object([:]))
            }
            return (self.response(request), self.envelope("{}"))
        }
        _ = try await client.monitoringProject("p", resource: "arm", body: [:])
        _ = try await client.monitoringProject("p", resource: "disarm", body: [:])
        _ = try await client.monitoringProject("p", resource: "allowed", body: ["watch_changes": .boolean(true)])
    }

    func testAudioIsReadThroughEventAPIWithEncodedIdentifiers() async throws {
        let expected = wav
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertTrue(request.url!.absoluteString.hasSuffix("/monitoring-projects/p%2F1/events/e%2F2/audio"))
            XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "audio/wav")
            return (self.response(request, mime: "audio/wav"), expected)
        }
        let bytes = try await client.monitoringEventAudio(projectID: "p/1", eventID: "e/2")
        XCTAssertEqual(bytes, expected)
    }

    func testAudioRejectsOversizedArtifact() async throws {
        let client = try makeClient { request in (self.response(request, mime: "audio/wav"), self.wav) }
        do {
            _ = try await client.monitoringEventAudio(projectID: "p", eventID: "e", maximumBytes: 44)
            XCTFail("Oversized WAV must fail")
        } catch let error as PlatformError { XCTAssertEqual(error, .responseTooLarge) }
    }

    func testAudioRejectsWrongMIMEAndTruncatedRIFF() async throws {
        for wrongMIME in [true, false] {
            let client = try makeClient { request in
                (self.response(request, mime: wrongMIME ? "text/html" : "audio/wav"),
                 wrongMIME ? self.wav : Data(self.wav.dropLast()))
            }
            do {
                _ = try await client.monitoringEventAudio(projectID: "p", eventID: "e")
                XCTFail("Invalid WAV must fail")
            } catch let error as PlatformError {
                if case .invalidResponse = error {} else { XCTFail("Unexpected error: \(error)") }
            }
        }
    }

    func testAudioReportsCoreErrorAndRejectsChangedOrigin() async throws {
        let missing = try makeClient { request in
            (self.response(request, status: 404), Data("{\"error\":{\"code\":\"monitoring_not_found\",\"message\":\"Нет записи\"}}".utf8))
        }
        do {
            _ = try await missing.monitoringEventAudio(projectID: "p", eventID: "e")
            XCTFail("Missing audio must fail")
        } catch let error as PlatformError {
            XCTAssertEqual(error, .http(status: 404, code: "monitoring_not_found", message: "Нет записи"))
        }
        let redirected = try makeClient { _ in
            (HTTPURLResponse(url: URL(string: "https://other.test/audio")!, statusCode: 200,
                             httpVersion: nil, headerFields: ["Content-Type": "audio/wav"])!, self.wav)
        }
        do {
            _ = try await redirected.monitoringEventAudio(projectID: "p", eventID: "e")
            XCTFail("Changed origin must fail")
        } catch let error as PlatformError {
            if case .connection = error {} else { XCTFail("Unexpected error: \(error)") }
        }
    }

    func testInvalidResourceAndAudioLimitsFailWithoutNetwork() async throws {
        let client = try makeClient { _ in XCTFail("No network expected"); throw CancellationError() }
        do {
            _ = try await client.monitoringProject("p", resource: "../arm", body: [:])
            XCTFail("Invalid resource must fail")
        } catch let error as PlatformError {
            if case .invalidCommand = error {} else { XCTFail("Unexpected error: \(error)") }
        }
        do {
            _ = try await client.monitoringEventAudio(projectID: "p", eventID: "e", maximumBytes: 33 * 1024 * 1024)
            XCTFail("Unbounded download must fail")
        } catch let error as PlatformError {
            if case .invalidCommand = error {} else { XCTFail("Unexpected error: \(error)") }
        }
    }
    private func historyValue(id: String = "sweep", frequencies: [Int64] = [100, 200], levels: [Double] = [-80, -50], raw: Bool = false) throws -> MonitoringValue {
        var object: [String: Any] = ["id": id, "session_id": "session", "stream_id": "analyzer",
            "interval": ["started_at": "2026-11-10T10:00:00.100Z", "ended_at": "2026-11-10T10:00:01.600Z"],
            "frequencies_hz": frequencies, "levels": levels, "level_unit": "dBm", "settings": ["rbw_hz": 1000]]
        if !raw { object["original_point_count"] = max(3, frequencies.count); object["display_only"] = true }
        return try JSONDecoder().decode(MonitoringValue.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func valueEnvelope(_ value: MonitoringValue) throws -> Data { envelope(String(decoding: try JSONEncoder().encode(value), as: UTF8.self)) }

    func testHistoryQueryPreservesUTCIntegerHzEventAndOpaqueCursor() async throws {
        let cursor = "time/id+tie?=", event = "event/other?&"
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.absoluteString.contains("/monitoring-projects/p%2F1/history?"))
            let query = Dictionary(uniqueKeysWithValues: URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!.map { ($0.name, $0.value!) })
            XCTAssertEqual(query["limit"], "64")
            XCTAssertEqual(query["after"], cursor)
            XCTAssertEqual(query["event_id"], event)
            XCTAssertEqual(query["min_hz"], "9007199254740990")
            XCTAssertEqual(query["max_hz"], "9007199254740991")
            XCTAssertEqual(query["start"], "2026-11-10T10:00:00.100Z")
            XCTAssertEqual(query["end"], "2026-11-10T10:00:10Z")
            return (self.response(request), self.envelope("{\"items\":[],\"next_cursor\":null}"))
        }
        _ = try await client.monitoringHistory(projectID: "p/1", query: .init(limit: 64,
            start: "2026-11-10T10:00:00.100Z", end: "2026-11-10T10:00:10Z",
            minHz: 9007199254740990, maxHz: 9007199254740991, eventID: event), after: cursor)
    }

    func testInvalidHistoryFiltersAndPageLimitsNeverReachNetwork() async throws {
        let client = try makeClient { _ in XCTFail("No network expected"); throw CancellationError() }
        let queries: [MonitoringHistoryQuery] = [.init(limit: 0), .init(limit: 65), .init(minHz: -1),
            .init(minHz: 200, maxHz: 100), .init(start: "2026-11-10T10:00:00"),
            .init(start: "badZ"), .init(start: "2026-11-11T10:00:00Z", end: "2026-11-10T10:00:00Z")]
        for query in queries {
            do { _ = try await client.monitoringHistory(projectID: "p", query: query); XCTFail("Invalid query must fail") }
            catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error: \(error)") } }
        }
    }

    func testHistoryRowsRetainActualTimeDifferentGridsAndCoreDisplayFlag() throws {
        let first = try MonitoringHistoryRow(historyValue())
        let second = try MonitoringHistoryRow(historyValue(id: "other", frequencies: [150, 300], levels: [-90, -60]))
        XCTAssertEqual(first.endedAt.timeIntervalSince(first.startedAt), 1.5, accuracy: 0.001)
        XCTAssertEqual(first.frequenciesHz, [100, 200])
        XCTAssertEqual(second.frequenciesHz, [150, 300])
        XCTAssertTrue(first.displayOnly)
        XCTAssertEqual(first.originalPointCount, 3)
        let raw = try MonitoringHistoryRow(historyValue(raw: true), raw: true)
        XCTAssertFalse(raw.displayOnly)
        XCTAssertEqual(raw.originalPointCount, 2)
        // A frequency filter may fall between original bins, producing no display samples.
        let empty = try MonitoringHistoryRow(historyValue(frequencies: [], levels: []))
        XCTAssertTrue(empty.levels.isEmpty)
    }

    func testHistoryRejectsMisalignedUnorderedAndOversizedDisplayRows() throws {
        XCTAssertThrowsError(try MonitoringHistoryRow(historyValue(frequencies: [100, 200], levels: [-70])))
        XCTAssertThrowsError(try MonitoringHistoryRow(historyValue(frequencies: [200, 100], levels: [-70, -80])))
        XCTAssertThrowsError(try MonitoringHistoryRow(historyValue(frequencies: Array(0...320).map(Int64.init), levels: Array(repeating: -70, count: 321))))
        XCTAssertThrowsError(try MonitoringHistoryRow(historyValue(raw: true)))
    }

    func testHistoryPageRejectsServerExceedingRequestedBound() async throws {
        let row = try historyValue()
        let client = try makeClient { request in
            (self.response(request), try self.valueEnvelope(.object(["items": .array([row, row]), "next_cursor": .null])))
        }
        do { _ = try await client.monitoringHistory(projectID: "p", query: .init(limit: 1)); XCTFail("Oversized page must fail") }
        catch let error as PlatformError { if case .invalidResponse = error {} else { XCTFail("Unexpected error: \(error)") } }
    }

    func testScopedRawSweepAndIncompatibleBaselineComparison() async throws {
        let row = try historyValue(id: "s/1", raw: true)
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.absoluteString.contains("/monitoring-projects/p%2F1/history/s%2F1"))
            if request.url!.path.hasSuffix("/comparison") {
                return (self.response(request), self.envelope("{\"sweep_id\":\"s/1\",\"baseline_id\":\"b\",\"compatible\":false,\"reason\":\"Different settings\",\"items\":[],\"display_only\":true}"))
            }
            return (self.response(request), try self.valueEnvelope(row))
        }
        let raw = try await client.monitoringHistorySweep(projectID: "p/1", sweepID: "s/1")
        XCTAssertEqual(raw["frequencies_hz"].items, [.integer(100), .integer(200)])
        let comparison = try await client.monitoringHistorySweep(projectID: "p/1", sweepID: "s/1", comparison: true)
        XCTAssertEqual(comparison["compatible"].bool, false)
        XCTAssertEqual(comparison["reason"].text, "Different settings")
    }

    func testManualAndSourceSearchCommandsCarryOnlyCapturedValues() async throws {
        var actions: [String] = []
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            let path = request.url!.path
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            if path.hasSuffix("/manual/acquire") {
                XCTAssertEqual(body, .object(["role": .string("receiver")]))
                actions.append("acquire")
                return (self.response(request), self.envelope("{\"token\":\"held\",\"role\":\"receiver\",\"expires_seconds\":30}"))
            }
            XCTAssertEqual(body["token"], .string("held"))
            if path.hasSuffix("/manual/tune") {
                XCTAssertEqual(body["frequency_hz"], .integer(145500000)); XCTAssertEqual(body["mode"], .string("FM")); actions.append("tune")
            } else if path.hasSuffix("/search/start") {
                XCTAssertEqual(body["event_id"], .string("event")); XCTAssertEqual(body["span_hz"], .integer(1000000)); actions.append("start")
            } else if path.hasSuffix("/search/mark") {
                XCTAssertEqual(body["label"], .string("Точка 1")); actions.append("mark")
            } else if path.hasSuffix("/manual/renew") { actions.append("renew") }
            else if path.hasSuffix("/manual/return") { actions.append("return") }
            else { XCTFail("Unexpected control path") }
            // Renewal intentionally does not return the already-held token.
            return (self.response(request), self.envelope("{\"role\":\"receiver\",\"expires_seconds\":30}"))
        }
        _ = try await client.monitoringControl(projectID: "p", action: "manual/acquire", body: ["role": .string("receiver")])
        _ = try await client.monitoringControl(projectID: "p", action: "manual/tune", body: ["token": .string("held"), "frequency_hz": .integer(145500000), "mode": .string("FM")])
        _ = try await client.monitoringControl(projectID: "p", action: "search/start", body: ["token": .string("held"), "event_id": .string("event"), "span_hz": .integer(1000000)])
        _ = try await client.monitoringControl(projectID: "p", action: "search/mark", body: ["token": .string("held"), "label": .string("Точка 1")])
        let renewed = try await client.monitoringControl(projectID: "p", action: "manual/renew", body: ["token": .string("held")])
        XCTAssertEqual(renewed["token"], .null)
        _ = try await client.monitoringControl(projectID: "p", action: "manual/return", body: ["token": .string("held")])
        XCTAssertEqual(actions, ["acquire", "tune", "start", "mark", "renew", "return"])
    }

    func testExpiredManualLeaseSurfacesCoreConflictWithoutReplay() async throws {
        var requests = 0
        let client = try makeClient { request in
            requests += 1
            return (self.response(request, status: 409), Data("{\"error\":{\"code\":\"monitoring_conflict\",\"message\":\"Аренда истекла; обзор на паузе\"}}".utf8))
        }
        do {
            _ = try await client.monitoringControl(projectID: "p", action: "manual/renew", body: ["token": .string("expired")])
            XCTFail("Expired lease must fail")
        } catch let error as PlatformError {
            XCTAssertEqual(error, .http(status: 409, code: "monitoring_conflict", message: "Аренда истекла; обзор на паузе"))
        }
        XCTAssertEqual(requests, 1)
    }

    func testSearchStatusAndSourcePaginationStayScopedAndBounded() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET", "Search-series polling must remain read-only")
            XCTAssertNil(request.httpBody)
            if request.url!.path.hasSuffix("/search/status") {
                return (self.response(request), self.envelope("{\"active\":false,\"coverage\":\"paused\",\"series\":[],\"marks\":[],\"maximum_level\":null,\"error\":\"worker stopped\"}"))
            }
            XCTAssertTrue(request.url!.absoluteString.contains("/p/events/e%2F1/sources?"))
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            XCTAssertEqual(query.first { $0.name == "after" }?.value, "ref/1+two")
            XCTAssertEqual(query.first { $0.name == "limit" }?.value, "64")
            return (self.response(request), self.envelope("{\"items\":[{\"sweep_id\":\"raw\",\"session_id\":\"old\",\"stream_id\":\"a\"}],\"next_cursor\":\"next\"}"))
        }
        let status = try await client.monitoringSearchStatus(projectID: "p")
        XCTAssertEqual(status["coverage"].text, "paused")
        XCTAssertEqual(status["error"].text, "worker stopped")
        let refs = try await client.monitoringEventSources(projectID: "p", eventID: "e/1", limit: 64, after: "ref/1+two")
        XCTAssertEqual(refs["items"].items.first?["sweep_id"].text, "raw")
        XCTAssertEqual(refs["next_cursor"].text, "next")
    }

    func testSearchRejectsMoreThan256CorePointsAndUnknownControlRoutes() async throws {
        let client = try makeClient { request in
            let series = MonitoringValue.object(["series": .array(Array(repeating: .object([:]), count: 257))])
            return (self.response(request), try self.valueEnvelope(series))
        }
        do { _ = try await client.monitoringSearchStatus(projectID: "p"); XCTFail("Oversized series must fail") }
        catch let error as PlatformError { if case .invalidResponse = error {} else { XCTFail("Unexpected error: \(error)") } }
        do { _ = try await client.monitoringControl(projectID: "p", action: "manual/../arm", body: [:]); XCTFail("Unknown control must fail") }
        catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error: \(error)") } }
    }

    func testComparisonKeepsCoreSelectedFrequenciesAndDeltaWithoutRecomputing() async throws {
        let payload = #"{"sweep_id":"s","compatible":true,"display_only":true,"items":[{"frequency_hz":145500000,"baseline_p50_dbm":-80,"baseline_p95_dbm":-76,"current_dbm":-67,"delta_db":13}]}"#
        let client = try makeClient { request in (self.response(request), self.envelope(payload)) }
        let value = try await client.monitoringHistorySweep(projectID: "p", sweepID: "s", comparison: true)
        XCTAssertEqual(value["items"].items.first?["frequency_hz"], .integer(145500000))
        XCTAssertEqual(value["items"].items.first?["delta_db"], .integer(13))
    }

    func testRawSweepWithAnotherIdentityIsRejected() async throws {
        let row = try historyValue(id: "other", raw: true)
        let client = try makeClient { request in (self.response(request), try self.valueEnvelope(row)) }
        do { _ = try await client.monitoringHistorySweep(projectID: "p", sweepID: "requested"); XCTFail("Wrong sweep must fail") }
        catch let error as PlatformError { if case .invalidResponse = error {} else { XCTFail("Unexpected error: \(error)") } }
    }

    private var publicSettingsJSON: String {
        #"{"autostart":false,"archive":{"minimum_free_bytes":268435456,"minimum_retention_seconds":3600},"webhook":{"enabled":false,"url":"https://hooks.example.test/receive","secret_configured":true},"detector":{"margin_db":6,"confirmations":3,"window_sweeps":5}}"#
    }

    func testSettingsResponseAllowlistDropsSecretsAtEveryConfigurationLevel() async throws {
        var value = try JSONDecoder().decode(MonitoringValue.self, from: Data(publicSettingsJSON.utf8))
        guard case .object(var object) = value else { return XCTFail("Expected object") }
        object["secret"] = .string("server-private-value")
        if case .object(var webhook)? = object["webhook"] {
            webhook["secret"] = .string("server-private-value"); object["webhook"] = .object(webhook)
        }
        if case .object(var detector)? = object["detector"] {
            detector["secret"] = .string("server-private-value"); object["detector"] = .object(detector)
        }
        value = .object(object)
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertTrue(request.url!.absoluteString.hasSuffix("/monitoring-projects/p%2F1/settings"))
            return (self.response(request), try self.valueEnvelope(value))
        }
        let safe = try await client.monitoringSettings(projectID: "p/1")
        XCTAssertEqual(safe["webhook"]["secret"], .null)
        XCTAssertEqual(safe["detector"]["secret"], .null)
        XCTAssertEqual(safe["secret"], .null)
        XCTAssertFalse(safe.pretty.contains("server-private-value"))
        XCTAssertEqual(safe["webhook"]["secret_configured"].bool, true)
    }

    func testBlankWebhookSecretIsOmittedAndPartialSettingsRemainPartial() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            XCTAssertEqual(body, .object(["webhook": .object(["enabled": .boolean(false)])]))
            return (self.response(request), self.envelope(self.publicSettingsJSON))
        }
        _ = try await client.monitoringSettings(projectID: "p", body: ["webhook": .object(["enabled": .boolean(false), "secret": .string("")])])
    }

    func testNewWebhookSecretIsSentOnlyInExplicitSettingsBody() async throws {
        let credential = "private-test-credential"
        let client = try makeClient { request in
            XCTAssertFalse(request.url!.absoluteString.contains(credential))
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            XCTAssertTrue(body["webhook"]["secret"].text == credential)
            return (self.response(request), self.envelope(self.publicSettingsJSON))
        }
        let result = try await client.monitoringSettings(projectID: "p", body: ["autostart": .boolean(true), "webhook": .object(["secret": .string(credential)])])
        XCTAssertFalse(result.pretty.contains(credential))
    }

    func testSettingsErrorsNeverExposeReflectedSecretOrServerMessage() async throws {
        let credential = "private-reflected-value"
        let client = try makeClient { request in
            let error: MonitoringValue = .object(["error": .object(["code": .string(credential), "message": .string("Invalid credential: " + credential)])])
            return (self.response(request, status: 400), try JSONEncoder().encode(error))
        }
        do {
            _ = try await client.monitoringSettings(projectID: "p", body: ["webhook": .object(["secret": .string(credential)])])
            XCTFail("Invalid settings must fail")
        } catch let error as PlatformError {
            XCTAssertFalse(error.localizedDescription.contains(credential))
            if case .http(let status, let code, _) = error { XCTAssertEqual(status, 400); XCTAssertFalse(code.contains(credential)) }
            else { XCTFail("Expected HTTP failure") }
        }
    }

    func testStorageAndAlertPollingMethodsOnlyReadAndPreservePreciseBytesAndCursor() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertNil(request.httpBody)
            if request.url!.path.hasSuffix("/storage") {
                return (self.response(request), self.envelope(#"{"capacity":{},"filesystem_free_bytes":9007199254740991,"minimum_free_bytes":268435456,"available_bytes":123,"bytes_per_second":null,"estimated_retention_seconds":null,"required_retention_seconds":3600,"ready":true,"automatic_deletion":false}"#))
            }
            XCTAssertTrue(request.url!.absoluteString.contains("/monitoring-projects/p%2F1/alerts?"))
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!
            XCTAssertEqual(query.first { $0.name == "after" }?.value, "opaque/created+id==")
            XCTAssertEqual(query.first { $0.name == "limit" }?.value, "100")
            return (self.response(request), self.envelope(#"{"items":[{"id":"notice","notification_id":"notice","acknowledged_at":null,"delivery":{"state":"pending","attempts":2,"last_error":null}}],"next_cursor":"next"}"#))
        }
        let storage = try await client.monitoringStorage(projectID: "p/1")
        XCTAssertEqual(storage["filesystem_free_bytes"], .integer(9007199254740991))
        XCTAssertEqual(storage["estimated_retention_seconds"], .null)
        let alerts = try await client.monitoringAlerts(projectID: "p/1", after: "opaque/created+id==")
        XCTAssertEqual(alerts["next_cursor"].text, "next")
        XCTAssertEqual(alerts["items"].items.first?["delivery"]["attempts"], .integer(2))
    }

    func testAlertAcknowledgementAndEventVerdictUseSeparateScopedExplicitPosts() async throws {
        var posts = 0
        let client = try makeClient { request in
            posts += 1; XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            if request.url!.absoluteString.hasSuffix("/p%2F1/alerts/a%2F1/ack") {
                XCTAssertEqual(body, .object([:]))
            } else {
                XCTAssertTrue(request.url!.absoluteString.hasSuffix("/p%2F1/events/e%2F1/review"))
                XCTAssertEqual(body, .object(["disposition": .string("false_positive"), "comment": .string("Проверено оператором")]))
            }
            return (self.response(request), self.envelope("{}"))
        }
        _ = try await client.monitoringAcknowledgeAlert(projectID: "p/1", alertID: "a/1")
        _ = try await client.monitoringReviewEvent(projectID: "p/1", eventID: "e/1", disposition: "false_positive", comment: "Проверено оператором")
        XCTAssertEqual(posts, 2)
    }

    func testAlertLimitsAndUnknownVerdictsFailBeforeNetwork() async throws {
        let client = try makeClient { _ in XCTFail("No network expected"); throw CancellationError() }
        for limit in [0, 101] {
            do { _ = try await client.monitoringAlerts(projectID: "p", limit: limit); XCTFail("Invalid limit must fail") }
            catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error") } }
        }
        do { _ = try await client.monitoringReviewEvent(projectID: "p", eventID: "e", disposition: "automatic_tune", comment: ""); XCTFail("Unknown verdict must fail") }
        catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error") } }
    }

    func testAlertPagesRejectOversizedServerResponse() async throws {
        let client = try makeClient { request in
            let value: MonitoringValue = .object(["items": .array(Array(repeating: .object(["id": .string("n")]), count: 101))])
            return (self.response(request), try self.valueEnvelope(value))
        }
        do { _ = try await client.monitoringAlerts(projectID: "p"); XCTFail("Oversized page must fail") }
        catch let error as PlatformError { if case .invalidResponse = error {} else { XCTFail("Unexpected error") } }
    }

    func testLocalAlertSoundStartsQuietAndRequiresOptInForNewUnacknowledgedIDs() {
        func alert(_ id: String, acknowledged: Bool = false, attempts: Int64 = 0) -> MonitoringValue {
            .object(["id": .string(id), "acknowledged_at": acknowledged ? .string("2026-11-10T10:00:00Z") : .null,
                     "delivery": .object(["attempts": .integer(attempts)])])
        }
        var state = MonitoringAlertSoundState()
        XCTAssertFalse(state.observe([alert("old")], enabled: true), "First snapshot stays quiet even after opt-in")
        XCTAssertFalse(state.observe([alert("muted-new"), alert("old")], enabled: false))
        XCTAssertFalse(state.observe([alert("muted-new", attempts: 2), alert("old")], enabled: true), "Delivery retry does not replay sound")
        XCTAssertFalse(state.observe([alert("already-acked", acknowledged: true), alert("muted-new")], enabled: true))
        XCTAssertTrue(state.observe([alert("new"), alert("already-acked", acknowledged: true)], enabled: true))
        XCTAssertFalse(state.observe([alert("new"), alert("already-acked", acknowledged: true)], enabled: true))
        state = MonitoringAlertSoundState()
        XCTAssertFalse(state.observe([alert("another-project")], enabled: true), "New scope starts quietly")
    }

    func testProjectSessionFollowFiresOncePerChangedActiveSessionAndScope() {
        var follow = MonitoringProjectSessionFollowState()
        XCTAssertNil(follow.sessionToFollow(endpoint: "core", projectID: "p", activeSessionID: ""))
        XCTAssertEqual(follow.sessionToFollow(endpoint: "core", projectID: "p", activeSessionID: "prepared"), "prepared")
        XCTAssertNil(follow.sessionToFollow(endpoint: "core", projectID: "p", activeSessionID: "prepared"))
        XCTAssertEqual(follow.sessionToFollow(endpoint: "core", projectID: "p", activeSessionID: "armed-new"), "armed-new")
        // Polling cannot force a later manual research-session selection back to the guard session.
        XCTAssertNil(follow.sessionToFollow(endpoint: "core", projectID: "p", activeSessionID: "armed-new"))
        XCTAssertEqual(follow.sessionToFollow(endpoint: "other-core", projectID: "p", activeSessionID: "armed-new"), "armed-new")
        XCTAssertEqual(follow.sessionToFollow(endpoint: "core", projectID: "other-project", activeSessionID: "armed-new"), "armed-new")
    }

    func testSettingsTransportErrorsNeverExposeCredentialText() async throws {
        let credential = "private-network-error-value"
        let client = try makeClient { _ in
            throw URLError(.cannotConnectToHost, userInfo: [NSLocalizedDescriptionKey: credential])
        }
        do {
            _ = try await client.monitoringSettings(projectID: "p", body: ["webhook": .object(["secret": .string(credential)])])
            XCTFail("Connection must fail")
        } catch {
            XCTAssertFalse(error.localizedDescription.contains(credential))
        }
    }

    func testPreparationSavesScopedOperatorDeclarationsIncludingFalseFlags() async throws {
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url!.absoluteString.hasSuffix("/monitoring-projects/p%2F1/preparation"))
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            XCTAssertEqual(body, .object(["antenna_notes": .string("Антенна на крыше, тракт A"),
                "receive_path_confirmed": .boolean(true), "location_confirmed": .boolean(false)]))
            return (self.response(request), self.envelope("{}"))
        }
        _ = try await client.monitoringSavePreparation(projectID: "p/1", antennaNotes: "Антенна на крыше, тракт A", receivePathConfirmed: true, locationConfirmed: false)
    }

    func testPreparationStartStopAreExplicitEmptyPostsAndPreflightIsReadOnly() async throws {
        var posts = 0
        let client = try makeClient { request in
            if request.url!.path.hasSuffix("/preflight") {
                XCTAssertEqual(request.httpMethod, "GET"); XCTAssertNil(request.httpBody)
                return (self.response(request), self.envelope(#"{"baseline_collection":{"sweeps":700,"elapsed_seconds":60.5,"minimum_sweeps":20,"minimum_seconds":60,"ready":true},"hardware_validation":"not_run"}"#))
            }
            posts += 1
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url!.absoluteString.hasSuffix("/p%2F1/preparation/start") || request.url!.absoluteString.hasSuffix("/p%2F1/preparation/stop"))
            XCTAssertEqual(try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody)), .object([:]))
            return (self.response(request), self.envelope(#"{"project":{"active_session_id":"collection-new","state":"collecting"},"workers":[]}"#))
        }
        let started = try await client.monitoringStartPreparation(projectID: "p/1")
        XCTAssertEqual(started["project"]["active_session_id"].text, "collection-new")
        let preflight = try await client.monitoringProject("p/1", resource: "preflight")
        XCTAssertEqual(preflight["baseline_collection"]["sweeps"].integer, 700)
        XCTAssertEqual(preflight["baseline_collection"]["elapsed_seconds"].double, 60.5)
        XCTAssertEqual(preflight["baseline_collection"]["ready"].bool, true)
        XCTAssertEqual(preflight["hardware_validation"].text, "not_run")
        XCTAssertEqual(posts, 1, "Progress reading cannot start workers")
        _ = try await client.monitoringStopPreparation(projectID: "p/1")
        XCTAssertEqual(posts, 2)
    }

    func testInvalidPreparationNotesAndMissingProjectFailBeforeNetwork() async throws {
        let client = try makeClient { _ in XCTFail("No network expected"); throw CancellationError() }
        for notes in ["", " \n\t", String(repeating: "я", count: 1001)] {
            do {
                _ = try await client.monitoringSavePreparation(projectID: "p", antennaNotes: notes, receivePathConfirmed: false, locationConfirmed: false)
                XCTFail("Invalid notes must fail")
            } catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected failure") } }
        }
        do { _ = try await client.monitoringStartPreparation(projectID: ""); XCTFail("Project is required") }
        catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected failure") } }
        do { _ = try await client.monitoringStopPreparation(projectID: ""); XCTFail("Project is required") }
        catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected failure") } }
    }

    func testBalancedDetectorDefaultsHaveIntegerCountsAndRoundTrip() throws {
        let defaults = MonitoringDetectorSettings()
        XCTAssertEqual(defaults.marginDB, 6); XCTAssertEqual(defaults.impulseDB, 12)
        XCTAssertEqual(defaults.hysteresisDB, 3); XCTAssertEqual(defaults.dwellSeconds, 10)
        XCTAssertEqual(defaults.body["confirmations"], .integer(3))
        XCTAssertEqual(defaults.body["window_sweeps"], .integer(5))
        let value = try JSONDecoder().decode(MonitoringValue.self, from: JSONEncoder().encode(MonitoringValue.object(defaults.body)))
        XCTAssertEqual(try MonitoringDetectorSettings(value), defaults)
    }

    func testDetectorRejectsNonfiniteRangesAndInconsistentRelationships() {
        func draft(margin: Double = 6, impulse: Double = 12, hysteresis: Double = 3,
                   confirmations: Int64 = 3, window: Int64 = 5, dwell: Double = 10) throws -> MonitoringDetectorSettings {
            try .init(marginDB: margin, impulseDB: impulse, hysteresisDB: hysteresis,
                confirmations: confirmations, windowSweeps: window, dwellSeconds: dwell)
        }
        XCTAssertThrowsError(try draft(margin: .nan))
        XCTAssertThrowsError(try draft(impulse: .infinity))
        XCTAssertThrowsError(try draft(margin: 0))
        XCTAssertThrowsError(try draft(margin: 61, impulse: 70))
        XCTAssertThrowsError(try draft(impulse: 121))
        XCTAssertThrowsError(try draft(impulse: 5))
        XCTAssertThrowsError(try draft(hysteresis: -1))
        XCTAssertThrowsError(try draft(hysteresis: 7))
        XCTAssertThrowsError(try draft(margin: 30, impulse: 40, hysteresis: 21))
        XCTAssertThrowsError(try draft(confirmations: 0))
        XCTAssertThrowsError(try draft(confirmations: 6))
        XCTAssertThrowsError(try draft(window: 16))
        XCTAssertThrowsError(try draft(dwell: 0))
        XCTAssertThrowsError(try draft(dwell: 31))
    }

    func testDetectorCountsMustBeIntegersAndKnownNumericFieldsStayPublic() async throws {
        let raw = #"{"autostart":false,"archive":{"minimum_free_bytes":268435456,"minimum_retention_seconds":3600},"webhook":{"enabled":false,"url":"","secret_configured":false},"detector":{"margin_db":6,"impulse_db":12,"hysteresis_db":3,"confirmations":3.5,"window_sweeps":5,"dwell_seconds":10,"secret":"hidden-detector-value"}}"#
        let client = try makeClient { request in (self.response(request), self.envelope(raw)) }
        let value = try await client.monitoringSettings(projectID: "p")
        XCTAssertEqual(value["detector"]["confirmations"], .null)
        XCTAssertEqual(value["detector"]["window_sweeps"], .integer(5))
        XCTAssertFalse(value.pretty.contains("hidden-detector-value"))
        XCTAssertThrowsError(try MonitoringDetectorSettings(value["detector"]))
    }

    func testDetectorSaveIsPartialAndLeavesWebhookArchiveAndAutostartUntouched() async throws {
        let expected = MonitoringDetectorSettings().body
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url!.path.hasSuffix("/p/settings"))
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            guard case .object(let fields) = body else { throw PlatformError.invalidResponse("Expected settings object") }
            XCTAssertEqual(Set(fields.keys), ["detector"])
            XCTAssertEqual(body["detector"]["confirmations"], .integer(3))
            XCTAssertEqual(body["detector"]["window_sweeps"], .integer(5))
            return (self.response(request), self.envelope(self.publicSettingsJSON))
        }
        _ = try await client.monitoringSettings(projectID: "p", body: ["detector": .object(expected)])
    }

    func testAudioClipBindsOriginalHashAndExactFrameRangeToEncodedEvent() async throws {
        let hash = String(repeating: "a", count: 64)
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.absoluteString.contains("/monitoring-projects/p%2F1/events/e%2F2/audio/clip?"))
            let query = Dictionary(uniqueKeysWithValues: URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems!.map { ($0.name, $0.value!) })
            XCTAssertEqual(query, ["start_frame":"1","end_frame":"2","source_artifact_id":"source/1","source_sha256":hash])
            XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "audio/wav")
            return (self.response(request, mime: "audio/wav"), self.wav)
        }
        let actual = try await client.monitoringEventAudioClip(projectID: "p/1", eventID: "e/2", startFrame: 1, endFrame: 2, sourceArtifactID: "source/1", sourceSHA256: hash)
        XCTAssertEqual(actual, wav)
    }

    func testAudioMarkSendsSourceIdentityWithoutStartingCapture() async throws {
        let hash = String(repeating: "b", count: 64)
        let client = try makeClient { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url!.path.hasSuffix("/audio/marks"))
            let body = try JSONDecoder().decode(MonitoringValue.self, from: XCTUnwrap(request.httpBody))
            XCTAssertEqual(body["source_sha256"], .string(hash)); XCTAssertEqual(body["start_frame"], .integer(0)); XCTAssertEqual(body["end_frame"], .integer(1))
            return (self.response(request), self.envelope("{\"id\":\"mark\"}"))
        }
        _ = try await client.monitoringAddAudioMark(projectID: "p", eventID: "e", startFrame: 0, endFrame: 1, label: "Voice", sourceArtifactID: "source/1", sourceSHA256: hash)
    }

    func testAudioEvidenceRejectsInvalidRangesBeforeAnyRequest() async throws {
        let client = try makeClient { _ in XCTFail("Invalid input must not reach core"); throw URLError(.badURL) }
        for (start, end, hash) in [(Int64(-1),Int64(2),String(repeating:"a",count:64)),(0,0,String(repeating:"a",count:64)),(0,1,"unknown")] {
            do {
                _ = try await client.monitoringEventAudioClip(projectID: "p", eventID: "e", startFrame: start, endFrame: end, sourceArtifactID: "source/1", sourceSHA256: hash)
                XCTFail("Invalid source/range accepted")
            } catch let error as PlatformError { if case .invalidCommand = error {} else { XCTFail("Unexpected error: \(error)") } }
        }
    }
}
