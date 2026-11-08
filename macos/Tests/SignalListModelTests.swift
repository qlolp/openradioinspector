import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class SignalListModelTests: XCTestCase {
    @MainActor final class Deferred {
        var exports: [(String, String)] = []
        var exportContinuations: [CheckedContinuation<PlatformSignalListExport, Error>] = []
        var imports: [(String, String, String)] = []
        var importContinuations: [CheckedContinuation<PlatformSignalListImport, Error>] = []
        func exportList(_ session: String, _ name: String) async throws -> PlatformSignalListExport {
            exports.append((session, name))
            return try await withCheckedThrowingContinuation { exportContinuations.append($0) }
        }
        func importList(_ session: String, _ stream: String, _ json: String) async throws -> PlatformSignalListImport {
            imports.append((session, stream, json))
            return try await withCheckedThrowingContinuation { importContinuations.append($0) }
        }
        func releaseExport(_ value: PlatformSignalListExport) { exportContinuations.removeFirst().resume(returning: value) }
        func releaseImport(_ value: PlatformSignalListImport) { importContinuations.removeFirst().resume(returning: value) }
    }
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ object: Any, status: Int = 200) {
            let payload: [String: Any] = status == 200 || status == 201
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": object]
                : ["error": ["code": "signal_list_error", "message": "synthetic"]]
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
    func entryObject() -> [String: Any] {
        ["frequency_hz": 101800000, "bandwidth_hz": 12500,
         "interval": ["started_at": "2026-11-08T12:00:00Z", "ended_at": "2026-11-08T12:00:01Z"],
         "level_unit": "dBm", "peak_level": -50.5]
    }
    func listObject(count: Int = 1) -> [String: Any] {
        ["format": "openradioinspector-signal-list", "format_version": 1, "name": "Test",
         "frequency_unit": "Hz",
         "entries": (0..<count).map { _ in entryObject() }]
    }
    func exportObject(count: Int = 1) -> [String: Any] {
        ["session_id": "s", "exported": count, "list": listObject(count: count)]
    }
    func importObject(count: Int = 1) -> [String: Any] {
        ["session_id": "s", "stream_id": "tiny", "imported": count,
         "candidate_ids": (0..<count).map { "candidate-\($0)" }, "name": "Test"]
    }
    @MainActor private var scope: SignalListModel.Scope { .init(server: "local", session: "s", stream: "tiny") }
    @MainActor private func waitFor(_ predicate: () -> Bool) async throws {
        for _ in 0..<200 { if predicate() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for signal list state")
    }
    @MainActor private func model(_ deferred: Deferred) throws -> SignalListModel {
        let model = SignalListModel(
            timeoutNanoseconds: 60_000_000_000,
            export: { _, session, name in try await deferred.exportList(session, name) },
            importList: { _, session, stream, json in try await deferred.importList(session, stream, json) })
        model.configure(client: try api(), scope: scope)
        return model
    }

    @MainActor func testExportFillsDraftAndImportEchoesScope() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        model.exportList()
        XCTAssertTrue(model.pending)
        try await waitFor { deferred.exportContinuations.count >= 1 }
        deferred.releaseExport(try decode(exportObject(), as: PlatformSignalListExport.self))
        try await waitFor { model.lastExport != nil }
        XCTAssertTrue(model.draft.contains("openradioinspector-signal-list"))
        XCTAssertNotNil(model.infoText)
        model.importListNow()
        try await waitFor { deferred.importContinuations.count >= 1 }
        XCTAssertEqual(deferred.imports[0].1, "tiny")
        deferred.releaseImport(try decode(importObject(), as: PlatformSignalListImport.self))
        try await waitFor { model.lastImport != nil }
        XCTAssertEqual(model.lastImport?.imported, 1)
    }

    @MainActor func testFailureNeverFreezes() async throws {
        let deferred = Deferred()
        let model = try model(deferred)
        model.exportList()
        try await waitFor { deferred.exportContinuations.count >= 1 }
        deferred.releaseExport(try decode(exportObject(), as: PlatformSignalListExport.self))
        try await waitFor { model.lastExport != nil }
        // A draft edit clears the result but keeps the artifact.
        let frozen = model.draft
        model.draft = frozen + " "
        XCTAssertNil(model.lastExport, "editing the draft clears the frozen result")
        XCTAssertTrue(model.draft.contains("openradioinspector-signal-list"))
    }

    @MainActor func testDecodingGuards() throws {
        let list = try decode(listObject(), as: PlatformSignalList.self)
        XCTAssertEqual(list.entries.count, 1)
        var empty = listObject()
        empty["entries"] = []
        XCTAssertThrowsError(try decode(empty, as: PlatformSignalList.self))
        var broken = importObject()
        broken["imported"] = 2
        XCTAssertThrowsError(try decode(broken, as: PlatformSignalListImport.self))
        var badEntry = listObject()
        badEntry["entries"] = [["frequency_hz": -1]]
        XCTAssertThrowsError(try decode(badEntry, as: PlatformSignalList.self))
    }
}
