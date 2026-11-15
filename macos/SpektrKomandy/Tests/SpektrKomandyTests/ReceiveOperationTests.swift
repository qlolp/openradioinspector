import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import SpektrKomandy

@Suite(.serialized)
struct ReceiveOperationTests {
    private final class Stub: URLProtocol {
        static var replies: [Data] = []
        static var paths: [String] = []
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func stopLoading() {}
        override func startLoading() {
            guard let url = request.url,
                  let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil),
                  !Self.replies.isEmpty else { return }
            Self.paths.append(url.path)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Self.replies.removeFirst())
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    private func client(_ values: [[String: Any]]) throws -> PlatformClient {
        Stub.paths = []
        Stub.replies = try values.map { try JSONSerialization.data(withJSONObject: ["contract": "openradioinspector-platform", "contract_version": 1, "data": $0]) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: configuration))
    }
    @Test func genericOperationAcceptsArtifactWithoutSettingsRevision() async throws {
        let api = try client([["operation_id": "job"], ["state": "succeeded", "context_current": true,
            "result": ["artifact": ["id": "zero", "kind": "zero_span"]]]])
        let result = try await api.tinySAOperation(sessionID: "s", streamID: "t", operation: "acquire.zero", parameters: [:], revision: 7)
        #expect(result["artifact"]["id"].text == "zero")
        #expect(Stub.paths.count == 2)
    }
    @Test func rejectsSuccessFromPreviousConnection() async throws {
        let api = try client([["operation_id": "job"], ["state": "succeeded", "context_current": false,
            "result": ["revision": 8]]])
        await #expect(throws: (any Error).self) {
            try await api.tinySAOperation(sessionID: "s", streamID: "t", operation: "watch.start", parameters: [:])
        }
    }
    @Test func cachedStatusDoesNotSubmitUSBInspection() async throws {
        let api = try client([["profile_verified": false, "settings": [:]]])
        _ = try await api.tinySACachedStatus(sessionID: "s", streamID: "t")
        #expect(Stub.paths == ["/api/v1/sessions/s/streams/t/instrument/settings"])
    }
}
