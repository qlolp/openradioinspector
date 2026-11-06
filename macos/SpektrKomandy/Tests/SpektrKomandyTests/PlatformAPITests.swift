import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
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
                guard let (response, data) = try Self.handler?(request) else {
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
        _ url: String = "http://example.test/root/api/v1/",
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

    func testBaseURLNormalizationAndValidation() throws {
        XCTAssertEqual(
            try PlatformBaseURL(" http://example.test/root/api/v1/ ").root.absoluteString,
            "http://example.test/root"
        )
        XCTAssertEqual(
            try PlatformBaseURL("http://example.test/").endpoint(path: "/sessions").absoluteString,
            "http://example.test/api/v1/sessions"
        )
        XCTAssertThrowsError(try PlatformBaseURL("127.0.0.1:8770"))
        XCTAssertThrowsError(try PlatformBaseURL("http://user:secret@example.test"))
        XCTAssertThrowsError(try PlatformBaseURL("http://example.test/?token=x"))
    }

    func testListSessionsChecksEnvelopeAndDecodesRawUnits() async throws {
        let client = try client { request in
            XCTAssertEqual(request.url?.absoluteString, "http://example.test/root/api/v1/sessions")
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
            XCTAssertEqual((streams[0]["settings"] as? [String: String])?["adapter"], "tinysa-serial")
            XCTAssertEqual((streams[1]["settings"] as? [String: String])?["adapter"], "ardv1-serial")
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

    func testTuneEncodesBothIDsAndUsesIntegerHertz() async throws {
        let client = try client { request in
            XCTAssertEqual(
                request.url?.absoluteString,
                "http://example.test/root/api/v1/sessions/session%2Fold/streams/ar%2Fdv1/tune"
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
