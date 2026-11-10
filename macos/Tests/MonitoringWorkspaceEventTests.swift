import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class MonitoringWorkspaceEventTests: XCTestCase {
    final class Stub: URLProtocol {
        static var responses: Responses?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            guard let responses = Self.responses else {
                client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
                return
            }
            let json = responses.reply(to: request)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200,
                                           httpVersion: nil, headerFields: nil)!
            let body = "{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(json)}"
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    final class Responses {
        private let lock = NSLock()
        private var json = "{}"
        private var requests: [URLRequest] = []
        func set(_ json: String) { lock.withLock { self.json = json } }
        func reply(to request: URLRequest) -> String {
            lock.withLock { requests.append(request); return json }
        }
        var lastRequest: URLRequest? { lock.withLock { requests.last } }
    }

    @MainActor func testEventListsPreserveProjectsSelectionAndActionTarget() async throws {
        let responses = Responses()
        Stub.responses = responses
        defer { Stub.responses = nil }
        let model = MonitoringWorkspaceModel(makeClient: { baseURL in
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: baseURL, session: URLSession(configuration: configuration))
        })
        model.configure("https://example.test", "session")
        let first = #"{"id":"p1","name":"First","site":"Site","state":"ready"}"#
        let second = #"{"id":"p2","name":"Second","site":"Site","state":"ready"}"#
        responses.set("{\"items\":[\(first),\(second)]}")
        await model.call()
        _ = try XCTUnwrap(responses.lastRequest, "Initial project request must reach the stub: \(model.details)")
        XCTAssertEqual(model.projects.map { $0["id"].text }, ["p1", "p2"])
        XCTAssertEqual(model.selected, "p1")
        model.selected = "p2"
        let original = model.projects
        for json in [#"{"items":[{"id":"event-1","kind":"new_signal"}]}"#, #"{"items":[]}"#] {
            responses.set(json)
            await model.call("/" + model.selected + "/events")
            XCTAssertEqual(responses.lastRequest?.url?.path, "/api/v1/monitoring-projects/p2/events")
            XCTAssertEqual(model.projects, original)
            XCTAssertEqual(model.selected, "p2")
            let detail = try JSONDecoder().decode(MonitoringValue.self, from: Data(model.details.utf8))
            XCTAssertEqual(detail, try JSONDecoder().decode(MonitoringValue.self, from: Data(json.utf8)))
            responses.set(#"{"ready":true}"#)
            await model.call("/" + model.selected + "/preflight")
            XCTAssertEqual(responses.lastRequest?.url?.path, "/api/v1/monitoring-projects/p2/preflight")
        }
        responses.set(#"{"items":[]}"#)
        await model.call(body: [:])
        XCTAssertEqual(responses.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(model.projects, original)
        XCTAssertEqual(model.selected, "p2")
        responses.set("{\"items\":[\(second),\(first)]}")
        await model.call()
        XCTAssertEqual(model.projects.map { $0["id"].text }, ["p2", "p1"])
        XCTAssertEqual(model.selected, "p2")
        responses.set("{\"items\":[\(first)]}")
        await model.call()
        XCTAssertEqual(model.selected, "p1")
        responses.set(#"{"items":[]}"#)
        await model.call()
        XCTAssertTrue(model.projects.isEmpty)
        XCTAssertEqual(model.selected, "")
    }
}
