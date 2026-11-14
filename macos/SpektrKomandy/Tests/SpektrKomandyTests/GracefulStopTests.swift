import Foundation
import Testing
@testable import SpektrKomandy

@Suite(.serialized)
struct GracefulStopTests {
    final class ProtocolStub: URLProtocol, @unchecked Sendable {
        nonisolated(unsafe) static var handler: ((URLRequest) throws -> Data)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do {
                let data = try Self.handler?(bufferedTestRequest(request)) ?? Data()
                let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
        override func stopLoading() {}
    }

    func response(_ value: String) -> Data {
        Data("{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(value)}".utf8)
    }

    @Test func gracefulStopNegotiatesSupportAndReturnsPendingState() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProtocolStub.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); ProtocolStub.handler = nil }
        var paths: [String] = []
        ProtocolStub.handler = { request in
            paths.append(request.url!.path)
            if request.url!.path.hasSuffix("/capabilities") {
                return response(#"{"features":{"graceful_device_stop":true,"unrelated":"value"}}"#)
            }
            let body = try #require(request.httpBody)
            let payload = try #require(JSONSerialization.jsonObject(with: body) as? [String: String])
            #expect(payload == ["mode": "graceful"])
            return response(#"{"stream_id":"tiny","state":"running","alive":true,"stopping":true,"stop_mode":"graceful"}"#)
        }
        let client = try PlatformClient(baseURL: "http://127.0.0.1:8770", session: session)
        let worker = try await client.stopStream(sessionID: "session", streamID: "tiny", mode: .graceful)
        #expect(paths.count == 2)
        #expect(worker.stopping == true && worker.alive)
        #expect(worker.stopMode == "graceful")
    }

    @Test func oldCoreDoesNotReceiveAnUnexpectedForcedStop() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProtocolStub.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); ProtocolStub.handler = nil }
        var posts = 0
        ProtocolStub.handler = { request in
            if request.httpMethod == "POST" { posts += 1 }
            return response(#"{"features":{}}"#)
        }
        let client = try PlatformClient(baseURL: "http://127.0.0.1:8770", session: session)
        await #expect(throws: (any Error).self) {
            _ = try await client.stopWorkers("session", mode: .graceful)
        }
        #expect(posts == 0)
    }

    @Test func explicitForceDoesNotRequireNewCapabilities() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ProtocolStub.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); ProtocolStub.handler = nil }
        ProtocolStub.handler = { request in
            #expect(request.httpMethod == "POST")
            let body = try #require(request.httpBody)
            let payload = try #require(JSONSerialization.jsonObject(with: body) as? [String: String])
            #expect(payload == ["mode": "force"])
            return response(#"{"stream_id":"tiny","state":"disconnected","alive":false}"#)
        }
        let client = try PlatformClient(baseURL: "http://127.0.0.1:8770", session: session)
        let worker = try await client.stopStream(sessionID: "session", streamID: "tiny", mode: .force)
        #expect(!worker.alive && worker.stopping == nil)
    }
}
