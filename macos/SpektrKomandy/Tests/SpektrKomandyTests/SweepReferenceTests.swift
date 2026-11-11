import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Foundation
import XCTest
@testable import SpektrKomandy

final class SweepReferenceTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        static var onStop: (() -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() { Self.onStop?() }
        func respond(_ object: Any, status: Int = 200, contract: String = "openradioinspector-platform") {
            let payload: [String: Any] = (200...299).contains(status)
                ? ["contract": contract, "contract_version": 1, "data": object] : ["error": object]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
            client?.urlProtocolDidFinishLoading(self)
        }
    }
    override func tearDown() { Stub.handler = nil; Stub.onStop = nil; super.tearDown() }
    private func client() throws -> PlatformClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [Stub.self]
        return try PlatformClient(baseURL: "https://example.test/root/api/v1/", session: URLSession(configuration: config))
    }
    private func record(session: String = "s", stream: String = "tiny", sweep: String = "old", name: String = "Étalon") -> [String: Any] {
        ["id": "ref", "session_id": session, "stream_id": stream, "sweep_id": sweep, "name": name,
         "created_at": "2026-11-08T01:02:00.123456+00:00",
         "interval": ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"],
         "level_unit": "dBm", "points": 2]
    }
    private func catalog(session: String = "s", stream: String? = "tiny") -> [String: Any] {
        ["session_id": session, "stream_id": stream as Any? ?? NSNull(), "storage_mode": "sqlite",
         "persistent": true, "limit": 100, "items": [record(session: session, stream: stream ?? "tiny")]]
    }
    private func decode<T: Decodable>(_ type: T.Type, _ object: Any) throws -> T {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(type, from: JSONSerialization.data(withJSONObject: object))
    }

    func testExactURLsBodiesNormalizationAndReplayStatuses() async throws {
        let api = try client()
        for status in [201, 200] {
            Stub.handler = { stub in
                XCTAssertEqual(stub.request.httpMethod, "POST")
                XCTAssertEqual(stub.request.timeoutInterval, 5)
                XCTAssertEqual(stub.request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s%2Fa%3F/sweep-references")
                let body = try! JSONSerialization.jsonObject(with: bufferedTestRequest(stub.request).httpBody!) as! [String: String]
                XCTAssertEqual(body, ["name": "Étalon", "sweep_id": "old /&+"])
                stub.respond(self.record(session: "s/a?", sweep: "old /&+"), status: status)
            }
            let result = try await api.saveSweepReference(sessionID: "s/a?", sweepID: "old /&+", name: " E\u{301}talon ")
            XCTAssertEqual(result.name, "Étalon")
        }
        Stub.handler = { stub in
            XCTAssertEqual(stub.request.httpMethod, "GET")
            XCTAssertNil(stub.request.httpBody)
            XCTAssertEqual(stub.request.url?.absoluteString, "https://example.test/root/api/v1/sessions/s%2Fa%3F/sweep-references?stream_id=t%2F%26%2B%C3%A9")
            stub.respond(self.catalog(session: "s/a?", stream: "t/&+é"))
        }
        let filtered = try await api.sweepReferences(sessionID: "s/a?", streamID: "t/&+é")
        XCTAssertEqual(filtered.items.first?.sweepId, "old")
        Stub.handler = { stub in
            XCTAssertNil(stub.request.url?.query)
            stub.respond(self.catalog(stream: nil))
        }
        let unfiltered = try await api.sweepReferences(sessionID: "s")
        XCTAssertNil(unfiltered.streamId)
    }

    func testNamesUseCodePointsRejectAllCategoryCBeforeTrim() throws {
        XCTAssertEqual(try normalizedSweepReferenceName("\u{A0} E\u{301}talon \u{3000}"), "Étalon")
        XCTAssertEqual(try normalizedSweepReferenceName(String(repeating: "😀", count: 80)).unicodeScalars.count, 80)
        XCTAssertNoThrow(try normalizedSweepReferenceName("é"))
        XCTAssertNoThrow(try normalizedSweepReferenceName("É"))
        for name in ["", " ", String(repeating: "😀", count: 81), String(repeating: "👍🏽", count: 41),
                     "x\n", "\tx", "x\u{200D}", "x\u{202E}", "x\u{E000}", "x\u{0378}", "x\u{7F}", "x\u{0}"] {
            XCTAssertThrowsError(try normalizedSweepReferenceName(name), name.debugDescription)
        }
    }

    func testStrictRecordValidation() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["id"] = " " }, { $0["session_id"] = "" }, { $0["stream_id"] = NSNull() },
            { $0["sweep_id"] = 1 }, { $0["name"] = " Étalon " }, { $0["name"] = "E\u{301}talon" },
            { $0["name"] = "x\n" }, { $0["created_at"] = "2026-11-08T01:00:00+02:00" },
            { $0["points"] = true }, { $0["points"] = 0 }, { $0["points"] = 16385 }, { $0["points"] = 1.5 },
            { $0["level_unit"] = "dB" }, { $0["interval"] = ["started_at": "2026-11-08T02:00:00Z", "ended_at": "2026-11-08T01:00:00Z"] }
        ]
        for (index, mutation) in mutations.enumerated() {
            var object = record(); mutation(&object)
            XCTAssertThrowsError(try decode(PlatformSweepReference.self, object), "Mutation \(index)")
        }
    }

    func testStrictCatalogScopeUniquenessPersistenceAndLimit() throws {
        let mutations: [(inout [String: Any]) -> Void] = [
            { $0["persistent"] = 1 }, { $0["storage_mode"] = "memory" }, { $0["storage_mode"] = "unknown" },
            { $0["limit"] = 101 }, { $0["limit"] = true }, { $0.removeValue(forKey: "stream_id") },
            { $0["stream_id"] = "" }, { $0["session_id"] = "foreign" },
            { $0["items"] = [self.record(stream: "foreign")] },
            { $0["items"] = [self.record(), self.record()] },
            { var second = self.record(); second["id"] = "other"; $0["items"] = [self.record(), second] },
            { $0["items"] = (0...100).map { i -> [String: Any] in var r = self.record(name: "\(i)"); r["id"] = "\(i)"; return r } }
        ]
        for (index, mutation) in mutations.enumerated() {
            var object = catalog(); mutation(&object)
            XCTAssertThrowsError(try decode(PlatformSweepReferenceCatalog.self, object), "Mutation \(index)")
        }
        var temporarySQLite = catalog(); temporarySQLite["persistent"] = false
        XCTAssertFalse(try decode(PlatformSweepReferenceCatalog.self, temporarySQLite).persistent)
        var memory = catalog(); memory["persistent"] = false; memory["storage_mode"] = "memory"
        XCTAssertFalse(try decode(PlatformSweepReferenceCatalog.self, memory).persistent)
    }

    func testWrongResponseIdentitiesAndMalformedEnvelopeAreRejected() async throws {
        for key in ["session_id", "sweep_id", "name", "contract"] {
            var object = record(); if key != "contract" { object[key] = "other" }
            Stub.handler = { $0.respond(object, status: 201, contract: key == "contract" ? "other" : "openradioinspector-platform") }
            do { _ = try await client().saveSweepReference(sessionID: "s", sweepID: "old", name: "Étalon"); XCTFail(key) }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
        Stub.handler = { $0.respond(self.catalog(stream: nil)) }
        do { _ = try await client().sweepReferences(sessionID: "s", streamID: "tiny"); XCTFail() }
        catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
    }

    func testErrorsStayStructuredAndInvalidInputDoesNotSend() async throws {
        for (status, code) in [(400, "invalid_request"), (404, "sweep_not_found"), (409, "reference_name_conflict"), (409, "reference_limit_reached"), (409, "comparison_incompatible")] {
            Stub.handler = { $0.respond(["code": code, "message": "test"], status: status) }
            do { _ = try await client().saveSweepReference(sessionID: "s", sweepID: "old", name: "Étalon"); XCTFail() }
            catch let error as PlatformError { XCTAssertEqual(error, .http(status: status, code: code, message: "test")) }
        }
        Stub.handler = { _ in XCTFail("Invalid input must not reach transport") }
        for name in ["", " x\n", String(repeating: "x", count: 81)] {
            do { _ = try await client().saveSweepReference(sessionID: "s", sweepID: "old", name: name); XCTFail() }
            catch PlatformError.invalidCommand {} catch { XCTFail("\(error)") }
        }
    }

    func testUnexpectedSuccessStatusesWithValidBodiesAreRejected() async throws {
        for status in [202, 206] {
            Stub.handler = { $0.respond(self.record(), status: status) }
            do { _ = try await client().saveSweepReference(sessionID: "s", sweepID: "old", name: "Étalon"); XCTFail("Status \(status) must not claim saved") }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
        for status in [201, 202, 206] {
            Stub.handler = { $0.respond(self.catalog(), status: status) }
            do { _ = try await client().sweepReferences(sessionID: "s", streamID: "tiny"); XCTFail("Status \(status) must not load catalog") }
            catch PlatformError.invalidResponse {} catch { XCTFail("\(error)") }
        }
    }

    func testCanceledWriteStopsTransportWithoutRetry() async throws {
        let started = expectation(description: "started"), stopped = expectation(description: "stopped")
        started.assertForOverFulfill = true
        Stub.handler = { _ in started.fulfill() }; Stub.onStop = { stopped.fulfill() }
        let api = try client()
        let task = Task { try await api.saveSweepReference(sessionID: "s", sweepID: "old", name: "Étalon") }
        await fulfillment(of: [started], timeout: 2)
        task.cancel()
        do { _ = try await task.value; XCTFail() } catch is CancellationError {} catch { XCTFail("\(error)") }
        await fulfillment(of: [stopped], timeout: 2)
    }
}
