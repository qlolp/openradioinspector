import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class DvbEvaluationTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let envelope: [String: Any] = (200...299).contains(status)
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "dvb_evaluation_error", "message": "synthetic"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: envelope)); client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; super.tearDown() }
    private func api() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: config))
    }
    private func decode<T: Decodable>(_ object: Any, as: T.Type = T.self) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(T.self, from: JSONSerialization.data(withJSONObject: object))
    }
    func resultObject(session: String = "s", resultCode: String = "norm") -> [String: Any] {
        ["session_id": session, "methodology": "dvb_t_parameters", "result_code": resultCode,
         "guard_interval": "1/4", "code_rate": "2/3",
         "parameters": ["mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0],
         "thresholds": ["min_mer_db": 15.0, "max_ber_after_viterbi": 1e-3, "max_ber_after_rs": 1e-9],
         "verdicts": ["mer_db": resultCode == "norm", "ber_after_viterbi": true, "ber_after_rs": true],
         "values": ["mer_db": 20.0, "ber_after_viterbi": 1e-6, "ber_after_rs": 0.0,
                    "guard_interval_ratio": 0.25, "code_rate_ratio": 0.6666666666666666]]
    }
    func request() -> PlatformDvbEvaluationRequest {
        PlatformDvbEvaluationRequest(
            parameters: ["mer_db": 20, "ber_after_viterbi": 1e-6, "ber_after_rs": 0],
            thresholds: ["min_mer_db": 15, "max_ber_after_viterbi": 1e-3, "max_ber_after_rs": 1e-9],
            guardInterval: "1/4", codeRate: "2/3")
    }

    func testResultDecodingGuards() throws {
        XCTAssertEqual(try decode(resultObject(), as: PlatformDvbEvaluation.self).resultCode, "norm")
        for changes in [["result_code": "ok"], ["methodology": "x"], ["guard_interval": "1/3"],
                        ["code_rate": "4/5"], ["session_id": ""]] as [[String: Any]] {
            var object = resultObject()
            object.merge(changes) { _, new in new }
            XCTAssertThrowsError(try decode(object, as: PlatformDvbEvaluation.self), "\(changes)")
        }
        var wrongValues = resultObject()
        wrongValues["values"] = ["mer_db": 1.0]
        XCTAssertThrowsError(try decode(wrongValues, as: PlatformDvbEvaluation.self))
    }

    func testClientRoundTripAndInputGuards() async throws {
        var sent: [(String, Data)] = []
        Stub.handler = { stub in
            var request = stub.request
            if request.httpBody == nil, let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var data = Data(); var buffer = [UInt8](repeating: 0, count: 4096)
                while true {
                    let count = stream.read(&buffer, maxLength: buffer.count)
                    if count <= 0 { break }
                    data.append(contentsOf: buffer.prefix(count))
                }
                request.httpBody = data
            }
            sent.append((request.url!.path, request.httpBody ?? Data()))
            stub.respond(self.resultObject())
        }
        let value = try await api().dvbEvaluation(sessionID: "s", request())
        XCTAssertEqual(value.resultCode, "norm")
        XCTAssertEqual(sent.count, 1)
        XCTAssertTrue(sent[0].0.hasSuffix("/sessions/s/dvb-evaluation"))
        let body = try JSONSerialization.jsonObject(with: sent[0].1) as! [String: Any]
        XCTAssertEqual(Set(body.keys), ["parameters", "thresholds", "guard_interval", "code_rate"])
        let client = try api()
        for bad in [PlatformDvbEvaluationRequest(parameters: ["mer_db": 1], thresholds: ["min_mer_db": 1],
                                                  guardInterval: "1/3", codeRate: "2/3"),
                    PlatformDvbEvaluationRequest(parameters: ["mer_db": 1], thresholds: ["min_mer_db": 1],
                                                  guardInterval: "1/4", codeRate: "4/5"),
                    PlatformDvbEvaluationRequest(parameters: ["x": 1], thresholds: ["min_mer_db": 1],
                                                  guardInterval: "1/4", codeRate: "2/3")] {
            do {
                _ = try await client.dvbEvaluation(sessionID: "s", bad)
                XCTFail("invalid request must be refused")
            } catch {}
        }
        do {
            _ = try await client.dvbEvaluation(sessionID: "", request())
            XCTFail("empty session must be refused")
        } catch {}
    }
}
