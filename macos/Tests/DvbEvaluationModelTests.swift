import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class DvbEvaluationModelTests: XCTestCase {
    @MainActor final class Deferred {
        var requests: [(String, PlatformDvbEvaluationRequest)] = []
        var continuations: [CheckedContinuation<PlatformDvbEvaluation, Error>] = []
        func evaluate(_ session: String, _ request: PlatformDvbEvaluationRequest) async throws -> PlatformDvbEvaluation {
            requests.append((session, request))
            return try await withCheckedThrowingContinuation { continuations.append($0) }
        }
        func release(_ value: PlatformDvbEvaluation) { continuations.removeFirst().resume(returning: value) }
        func fail(_ error: Error) { continuations.removeFirst().resume(throwing: error) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "dvb_evaluation_error", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func resultObject(resultCode: String = "norm") -> [String: Any] {
        ["session_id": "s", "methodology": "dvb_t_parameters", "result_code": resultCode,
         "guard_interval": "1/4", "code_rate": "2/3",
         "parameters": ["mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0],
         "thresholds": ["min_mer_db": 15.0, "max_ber_after_viterbi": 1e-3, "max_ber_after_rs": 1e-9],
         "verdicts": ["mer_db": resultCode == "norm", "ber_after_viterbi": true, "ber_after_rs": true],
         "values": ["mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0,
                    "guard_interval_ratio": 0.25, "code_rate_ratio": 0.6666666666666666]]
    }
    @MainActor private var scope: DvbEvaluationModel.Scope { .init(server: "local", session: "s") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for DVB state")
    }
    @MainActor private func model(_ deferred: Deferred) throws -> DvbEvaluationModel {
        let model = DvbEvaluationModel(
            timeoutNanoseconds: 60_000_000_000,
            evaluate: { _, session, request in try await deferred.evaluate(session, request) })
        model.configure(client: try api(), scope: scope)
        return model
    }

    @MainActor func testDraftValidationGatesTheButton() throws {
        let model = try model(Deferred())
        XCTAssertNotNil(model.draftRequest)
        model.draft.merText = ""
        XCTAssertNil(model.draftRequest, "empty fields never measure zero")
        model.draft.merText = "abc"
        XCTAssertNil(model.draftRequest)
        model.draft.merText = "20"
        model.draft.guardInterval = "1/3"
        XCTAssertNil(model.draftRequest, "unknown guard interval is refused")
    }

    @MainActor func testEvaluateEchoesRequestAndKeepsResult() async throws {
        let deferred = Deferred()
        let client = try api()
        let model = DvbEvaluationModel(
            timeoutNanoseconds: 60_000_000_000,
            evaluate: { _, session, request in try await deferred.evaluate(session, request) })
        model.configure(client: client, scope: scope)
        model.analyze()
        XCTAssertTrue(model.pending)
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        XCTAssertEqual(deferred.requests.count, 1, "single flight")
        XCTAssertEqual(deferred.requests[0].1.guardInterval, "1/4")
        deferred.release(try decode(resultObject(), as: PlatformDvbEvaluation.self))
        try await waitFor { model.result != nil }
        XCTAssertEqual(model.result?.resultCode, "norm")
        XCTAssertNotNil(model.infoText)
        model.draft.minMerText = "30"
        XCTAssertNil(model.infoText)
        XCTAssertNil(model.result)
    }

    @MainActor func testFailureNeverFreezes() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        model.analyze()
        try await waitFor { deferred.continuations.count >= 1 }
        deferred.fail(PlatformError.http(status: 409, code: "dvb_evaluation_error", message: "synthetic"))
        try await waitFor { !model.pending }
        XCTAssertNil(model.result)
        XCTAssertNotNil(model.errorText)
    }
}
