import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class ArchiveCapacityTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func respond(_ json: String, status: Int = 200) {
            let response = HTTPURLResponse(url: request.url!, statusCode: status,
                                           httpVersion: nil, headerFields: nil)!
            let body = status == 200
                ? "{\"contract\":\"openradioinspector-platform\",\"contract_version\":1,\"data\":\(json)}"
                : #"{"error":{"code":"not_found","message":"Unavailable endpoint"}}"#
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    final class Responses {
        private let lock = NSLock()
        private var capacityState = "unlimited"
        private var capacityStatus = 200
        private var hold = false
        private var pending: [Stub] = []
        var pendingCount: Int { lock.withLock { pending.count } }

        func configure(state: String = "unlimited", status: Int = 200, hold: Bool = false) {
            lock.withLock {
                capacityState = state
                capacityStatus = status
                self.hold = hold
            }
        }

        func capacity(_ state: String) -> String {
            let full = state == "full"
            return """
            {"storage_mode":"sqlite","database_bytes":1000,"max_database_bytes":\(full ? "1000" : "null"),"remaining_bytes":\(full ? "0" : "null"),"state":"\(state)","warning_threshold":0.9,"measurement_writes_blocked":\(full),"automatic_deletion":false}
            """
        }

        func release(state: String = "unlimited") {
            let held = lock.withLock { let held = pending; pending = []; return held }
            for stub in held { stub.respond(capacity(state)) }
        }

        func handle(_ stub: Stub) {
            switch stub.request.url!.path {
            case "/api/v1/archive/capacity":
                let response: (String, Int)? = lock.withLock {
                    if hold { pending.append(stub); return nil }
                    return (capacityState, capacityStatus)
                }
                if let response { stub.respond(capacity(response.0), status: response.1) }
            case "/api/v1/sessions": stub.respond("[]")
            case "/api/v1/task-scheduler":
                stub.respond(#"{"enabled":false,"scheduler_alive":false,"policy_persistent":false,"restart_requires_opt_in":true,"runtime":{},"tasks":{"total":0,"running":0,"completed":0}}"#)
            case "/api/v1/task-scheduler/tasks": stub.respond(#"{"items":[],"count":0}"#)
            default: stub.respond("{}", status: 404)
            }
        }
    }

    @MainActor private func setupModel(_ responses: Responses) -> RemoteDualModel {
        Stub.handler = { responses.handle($0) }
        let model = RemoteDualModel(makeClient: { baseURL in
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: baseURL, session: URLSession(configuration: configuration))
        })
        model.connect(startPolling: false)
        return model
    }

    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<200 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Timed out waiting for archive capacity")
    }

    @MainActor func testCapacityWithoutSessionAnd404RecoveryAreIsolated() async throws {
        let responses = Responses()
        let model = setupModel(responses)
        defer { model.disconnect() }
        try await waitFor { model.archiveCapacity != nil }
        XCTAssertEqual(model.archiveCapacity?.state, .unlimited)
        XCTAssertNil(model.session)
        XCTAssertTrue(model.selectedSessionID.isEmpty)
        XCTAssertNil(model.errorText)
        XCTAssertNil(model.taskSchedulerError)
        responses.configure(status: 404)
        await model.refresh()
        XCTAssertNil(model.archiveCapacity)
        XCTAssertTrue(model.archiveCapacityError?.contains("404") == true)
        XCTAssertNil(model.errorText)
        XCTAssertNil(model.taskSchedulerError)
        responses.configure(state: "full")
        await model.refresh()
        XCTAssertEqual(model.archiveCapacity?.state, .full)
        XCTAssertTrue(model.archiveCapacity?.measurementWritesBlocked == true)
        XCTAssertNil(model.archiveCapacityError)
    }

    @MainActor func testFailedSessionDoesNotHideGlobalCapacityOrApplyOlderResponse() async throws {
        let responses = Responses()
        let model = setupModel(responses)
        defer { model.disconnect() }
        try await waitFor { model.archiveCapacity != nil }
        responses.configure(hold: true)
        let older = Task { await model.refresh() }
        try await waitFor { responses.pendingCount == 1 }
        responses.configure(state: "full")
        model.selectSession("missing")
        // Session selection must not clear global capacity while its refresh is pending.
        XCTAssertEqual(model.archiveCapacity?.state, .unlimited)
        try await waitFor { model.archiveCapacity?.state == .full && model.errorText != nil }
        responses.release()
        await older.value
        XCTAssertEqual(model.archiveCapacity?.state, .full)
        XCTAssertNil(model.archiveCapacityError)
        XCTAssertNil(model.session)
    }

    @MainActor func testDisconnectAndReconnectRejectOldServerCapacity() async throws {
        let responses = Responses()
        let model = setupModel(responses)
        defer { model.disconnect() }
        try await waitFor { model.archiveCapacity != nil }
        responses.configure(hold: true)
        let older = Task { await model.refresh() }
        try await waitFor { responses.pendingCount == 1 }
        model.disconnect()
        XCTAssertNil(model.archiveCapacity)
        XCTAssertNil(model.archiveCapacityError)
        responses.configure(state: "full")
        model.serverURL = "https://new.example.test"
        model.connect(startPolling: false)
        try await waitFor { model.archiveCapacity?.state == .full }
        responses.release()
        await older.value
        XCTAssertEqual(model.archiveCapacity?.state, .full)
        XCTAssertNil(model.archiveCapacityError)
    }
}
