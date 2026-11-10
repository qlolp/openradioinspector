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
}
