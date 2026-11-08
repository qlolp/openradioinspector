import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

@testable import SpektrKomandy

final class AudioStreamTests: XCTestCase {
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

    private func makeClient(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> PlatformClient {
        Stub.handler = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        let session = URLSession(configuration: configuration)
        sessions.append(session)
        return try PlatformClient(baseURL: "https://example.test", session: session)
    }

    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }

    private func statusObject(
        session: String = "s", stream: String = "tiny",
        subscribers: Int = 1, maxSubscribers: Int = 4,
        queued: Int = 2, dropped: Int = 3
    ) -> [String: Any] {
        ["session_id": session, "stream_id": stream,
         "subscribers": subscribers, "max_subscribers": maxSubscribers,
         "queued_blocks": queued, "dropped_blocks": dropped]
    }

    func testStatusDecodingGuards() throws {
        let good = try decode(statusObject(), as: PlatformAudioStreamStatus.self)
        XCTAssertEqual(good.subscribers, 1)
        XCTAssertEqual(good.maxSubscribers, 4)
        XCTAssertEqual(good.droppedBlocks, 3)
        XCTAssertThrowsError(try decode(statusObject(session: " "), as: PlatformAudioStreamStatus.self))
        XCTAssertThrowsError(try decode(statusObject(subscribers: 5), as: PlatformAudioStreamStatus.self))
        XCTAssertThrowsError(try decode(statusObject(maxSubscribers: 0), as: PlatformAudioStreamStatus.self))
        XCTAssertThrowsError(try decode(statusObject(dropped: -1), as: PlatformAudioStreamStatus.self))
    }

    func testAudioStreamStatusOverHTTP() async throws {
        let payload = "{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":" +
            "{\"session_id\":\"s\",\"stream_id\":\"tiny/1\",\"subscribers\":2,\"max_subscribers\":4," +
            "\"queued_blocks\":1,\"dropped_blocks\":7}}"
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.path.hasSuffix("/sessions/s/audio/status"))
            XCTAssertTrue(request.url!.query!.contains("stream_id=tiny%2F1"), "слэш в query экранируется")
            return (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!,
                    Data(payload.utf8))
        }
        let status = try await client.audioStreamStatus(sessionID: "s", streamID: "tiny/1")
        XCTAssertEqual(status.subscribers, 2)
        XCTAssertEqual(status.droppedBlocks, 7)
    }

    func testAudioStreamSampleIsBounded() async throws {
        let pcm = Data([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        let client = try makeClient { request in
            XCTAssertTrue(request.url!.path.hasSuffix("/sessions/s/audio/stream"))
            XCTAssertTrue(request.url!.query!.contains("stream_id=tiny%2F1"), "слэш в query экранируется")
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200, httpVersion: nil,
                headerFields: ["Content-Type": "audio/pcm",
                               "X-Sample-Rate": "48000",
                               "X-Sample-Format": "pcm16le"])!
            return (response, pcm)
        }
        let sample = try await client.audioStreamSample(sessionID: "s", streamID: "tiny/1", maxBytes: 4)
        XCTAssertEqual(sample.pcm, Data([1, 2, 3, 4]), "выборка ограничена, хвост отброшен")
        XCTAssertEqual(sample.sampleRateHz, 48000)
        XCTAssertEqual(sample.sampleFormat, "pcm16le")
    }

    func testSampleRefusesWrongContractHeaders() async throws {
        let client = try makeClient { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200, httpVersion: nil,
                headerFields: ["Content-Type": "text/html"])!
            return (response, Data("nope".utf8))
        }
        await XCTAssertThrowsErrorAsync(
            try await client.audioStreamSample(sessionID: "s", streamID: "tiny/1"))
        let client2 = try makeClient { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200, httpVersion: nil,
                headerFields: ["Content-Type": "audio/pcm",
                               "X-Sample-Format": "pcm16le"])!
            return (response, Data([0]))
        }
        await XCTAssertThrowsErrorAsync(
            try await client2.audioStreamSample(sessionID: "s", streamID: "tiny/1"))
    }

    func testEmptyStreamRefusedBeforeAnyRequest() async throws {
        let client = try makeClient { _ in
            XCTFail("запрос не должен уходить без ID потока")
            throw PlatformError.connection("неожиданный запрос")
        }
        await XCTAssertThrowsErrorAsync(
            try await client.audioStreamSample(sessionID: "s", streamID: "  "))
        await XCTAssertThrowsErrorAsync(
            try await client.audioStreamStatus(sessionID: "s", streamID: ""))
    }
}

func XCTAssertThrowsErrorAsync<T>(
    _ expression: @autoclosure () async throws -> T,
    file: StaticString = #filePath, line: UInt = #line
) async {
    do {
        _ = try await expression()
        XCTFail("Ожидалась ошибка", file: file, line: line)
    } catch {}
}
