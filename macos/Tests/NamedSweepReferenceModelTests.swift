import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NamedSweepReferenceModelTests: XCTestCase {
    typealias Stub = SweepComparisonModelTests.Stub
    final class Responses {
        let snapshots = SweepComparisonModelTests.Responses()
        private let lock = NSLock()
        private var getHeld = false, postHeld = false
        private var status = 200
        private var malformed = false
        private var memory = false, temporarySQLite = false
        private var held: [(Stub, Any, Int)] = []
        private var saved: [[String: Any]] = []
        private var reads = 0, writes = 0
        var readCount: Int { lock.withLock { reads } }
        var writeCount: Int { lock.withLock { writes } }
        var stoppedCount: Int { lock.withLock { held.filter { $0.0.isStopped }.count } }
        var heldCount: Int { lock.withLock { held.count } }
        func configure(holdGet: Bool = false, holdPost: Bool = false, status: Int = 200, malformed: Bool = false, memory: Bool = false, temporarySQLite: Bool = false) {
            lock.withLock { getHeld = holdGet; postHeld = holdPost; self.status = status; self.malformed = malformed; self.memory = memory; self.temporarySQLite = temporarySQLite }
        }
        func release() {
            let previous = lock.withLock { let result = held; held = []; return result }
            for (stub, body, status) in previous { stub.respond(body, status: status) }
        }
        private func record(session: String, stream: String, name: String = "Archived baseline", sweep: String = "outside-snapshot") -> [String: Any] {
            ["id": "record-\(name)", "session_id": session, "stream_id": stream, "sweep_id": sweep, "name": name,
             "created_at": "2026-11-08T01:02:00Z", "level_unit": "dBm", "points": 2,
             "interval": ["started_at": "2026-11-08T01:00:00Z", "ended_at": "2026-11-08T01:00:01Z"]]
        }
        private func body(_ request: URLRequest) -> [String: String] {
            if let data = request.httpBody { return try! JSONSerialization.jsonObject(with: data) as! [String: String] }
            let stream = request.httpBodyStream!; stream.open(); defer { stream.close() }
            var data = Data(), buffer = [UInt8](repeating: 0, count: 1024)
            while true { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; data.append(contentsOf: buffer.prefix(n)) }
            return try! JSONSerialization.jsonObject(with: data) as! [String: String]
        }
        func handle(_ stub: Stub) {
            guard stub.request.url!.path.hasSuffix("/sweep-references") else { snapshots.handle(stub); return }
            let session = stub.request.url!.pathComponents.dropLast().last!
            let stream = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)!.queryItems?.first?.value ?? "tiny"
            let post = stub.request.httpMethod == "POST"
            let sent = post ? body(stub.request) : [:]
            let response: (Any, Int, Bool) = lock.withLock {
                if post { writes += 1 } else { reads += 1 }
                let result: Any
                if malformed { result = [:] as [String: String] }
                else if post {
                    let item = record(session: session, stream: stream, name: sent["name"]!, sweep: sent["sweep_id"]!)
                    if status < 400 && !saved.contains(where: { $0["id"] as? String == item["id"] as? String }) { saved.append(item) }
                    result = item
                } else {
                    let items = [record(session: session, stream: stream)] + saved.filter {
                        $0["session_id"] as? String == session && $0["stream_id"] as? String == stream
                    }
                    result = ["session_id": session, "stream_id": stream, "storage_mode": memory ? "memory" : "sqlite",
                              "persistent": !memory && !temporarySQLite, "limit": 100, "items": items]
                }
                let hold = post ? postHeld : getHeld
                if hold { held.append((stub, result, status)) }
                return (result, status, hold)
            }
            if !response.2 { stub.respond(response.0, status: response.1) }
        }
    }
    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<500 { if condition() { return }; try await Task.sleep(nanoseconds: 5_000_000) }
        XCTFail("Timed out waiting for named reference state")
    }
    @MainActor private func model(_ responses: Responses, timeout: UInt64 = 6_000_000_000, waitCatalog: Bool = true) async throws -> RemoteDualModel {
        Stub.handler = { responses.handle($0) }
        let model = RemoteDualModel(makeClient: { url in
            let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: url, session: URLSession(configuration: config))
        }, comparisonTimeoutNanoseconds: timeout)
        model.connect(startPolling: false)
        try await waitFor { model.comparisonSweeps.count == 2 && (!waitCatalog || !model.referenceCatalogPending) }
        return model
    }
    @MainActor private func pin(_ model: RemoteDualModel) {
        model.comparisonSelectionID = "r"; model.pinComparisonReference(); model.referenceName = "Baseline"
        XCTAssertEqual(model.comparisonReference?.id, "r")
    }

    @MainActor func testScopeCatalogLoadsOnceAndSavedReferenceComparesOutsideSnapshotWithoutChangingRaw() async throws {
        let responses = Responses(); let model = try await model(responses)
        defer { model.disconnect() }
        XCTAssertEqual(responses.readCount, 1)
        XCTAssertEqual(model.referenceCatalog?.persistent, true)
        XCTAssertEqual(model.savedSweepReferences.count, 1)
        let raw = model.current, revision = model.waterfallRevision
        model.useSavedSweepReference()
        XCTAssertEqual(model.comparisonReference?.id, "outside-snapshot")
        XCTAssertEqual(model.comparisonReference?.name, "Archived baseline")
        model.compareLatestSweep()
        try await waitFor { model.sweepComparison != nil }
        XCTAssertEqual(responses.snapshots.lastPair?.0, "outside-snapshot")
        XCTAssertEqual(responses.snapshots.lastPair?.1, "t")
        XCTAssertEqual(model.current, raw); XCTAssertEqual(model.waterfallRevision, revision)
        await model.refresh(); await model.refresh()
        XCTAssertEqual(responses.readCount, 1)
        XCTAssertEqual(model.comparisonReference?.id, "outside-snapshot")
        model.clearSweepComparison()
        XCTAssertNil(model.comparisonReference)
        XCTAssertEqual(model.savedSweepReferences.count, 1)
        pin(model)
        XCTAssertNil(model.comparisonReference?.name)
        responses.configure(memory: true)
        model.refreshSweepReferences()
        try await waitFor { !model.referenceCatalogPending }
        XCTAssertEqual(responses.readCount, 2)
        XCTAssertEqual(model.referenceCatalog?.storageMode, "memory")
        XCTAssertEqual(model.referenceCatalog?.persistent, false)
        XCTAssertTrue(model.referenceStorageDescription.hasPrefix("Memory:"))
        responses.configure(temporarySQLite: true)
        model.refreshSweepReferences()
        try await waitFor { !model.referenceCatalogPending }
        XCTAssertEqual(model.referenceCatalog?.storageMode, "sqlite")
        XCTAssertEqual(model.referenceCatalog?.persistent, false)
        XCTAssertTrue(model.referenceStorageDescription.contains("SQLite (:memory:): временный каталог"))
        XCTAssertFalse(model.referenceStorageDescription.contains("постоянный"))
    }

    @MainActor func testSaveAndRefreshAreSingleFlightAndNoAutomaticWritesOrReadsOnPoll() async throws {
        let responses = Responses(); let model = try await model(responses)
        defer { model.disconnect() }; pin(model)
        XCTAssertEqual(responses.writeCount, 0)
        responses.configure(holdPost: true)
        for _ in 0..<10 { model.savePinnedSweepReference(); model.refreshSweepReferences() }
        try await waitFor { responses.writeCount == 1 }
        XCTAssertEqual(responses.readCount, 1)
        await model.refresh()
        XCTAssertEqual(responses.writeCount, 1)
        responses.release()
        try await waitFor { !model.referenceSavePending }
        XCTAssertEqual(model.savedSweepReferences.count, 2)
        XCTAssertNil(model.referenceWriteNotice)
        XCTAssertEqual(model.comparisonReference?.id, "r")
        model.useSavedSweepReference()
        XCTAssertEqual(model.comparisonReference?.name, "Baseline")
        responses.configure(holdGet: true)
        for _ in 0..<10 { model.refreshSweepReferences(); model.savePinnedSweepReference() }
        try await waitFor { responses.readCount == 2 }
        XCTAssertEqual(responses.writeCount, 1)
        responses.release()
        try await waitFor { !model.referenceCatalogPending }
    }

    @MainActor func testWriteTimeoutAndCancelRemainUncertainUntilRefreshThenExplicitIdempotentReplay() async throws {
        for timeout in [false, true] {
            let responses = Responses(); let model = try await model(responses, timeout: timeout ? 70_000_000 : 6_000_000_000)
            pin(model); responses.configure(holdPost: true)
            model.savePinnedSweepReference()
            try await waitFor { responses.writeCount == 1 }
            if !timeout { model.cancelReferenceRequest() }
            try await waitFor { !model.referenceSavePending && responses.stoppedCount == 1 }
            XCTAssertTrue(model.referenceWriteNotice?.contains("могла сохраниться") == true)
            XCTAssertFalse(model.canSaveSweepReference)
            model.savePinnedSweepReference(); await model.refresh()
            XCTAssertEqual(responses.writeCount, 1); XCTAssertEqual(responses.readCount, 1)
            responses.release(); XCTAssertNotNil(model.referenceWriteNotice)
            responses.configure()
            model.refreshSweepReferences()
            try await waitFor { !model.referenceCatalogPending }
            XCTAssertNil(model.referenceWriteNotice)
            XCTAssertTrue(model.savedSweepReferences.contains { $0.name == "Baseline" })
            XCTAssertTrue(model.canSaveSweepReference)
            model.savePinnedSweepReference()
            try await waitFor { !model.referenceSavePending }
            XCTAssertEqual(responses.writeCount, 2)
            XCTAssertEqual(model.savedSweepReferences.filter { $0.name == "Baseline" }.count, 1)
            model.disconnect()
        }
    }

    @MainActor func testReadAndWriteErrorsAreIsolatedAndRefreshRecovers() async throws {
        let responses = Responses(); let model = try await model(responses)
        defer { model.disconnect() }; pin(model)
        let raw = model.current, revision = model.waterfallRevision, previousError = model.errorText
        for status in [404, 409, 400] {
            responses.configure(status: status)
            model.refreshSweepReferences(); try await waitFor { !model.referenceCatalogPending }
            XCTAssertNotNil(model.referenceCatalogError); XCTAssertTrue(model.savedSweepReferences.isEmpty)
            model.savePinnedSweepReference(); try await waitFor { !model.referenceSavePending }
            XCTAssertNotNil(model.referenceSaveStatus); XCTAssertNil(model.referenceWriteNotice)
            XCTAssertEqual(model.current, raw); XCTAssertEqual(model.waterfallRevision, revision)
            XCTAssertEqual(model.errorText, previousError); XCTAssertEqual(model.comparisonReference?.id, "r")
        }
        responses.configure(malformed: true)
        model.savePinnedSweepReference(); try await waitFor { !model.referenceSavePending }
        XCTAssertNotNil(model.referenceWriteNotice) // A bad success response does not prove no write.
        responses.configure()
        model.refreshSweepReferences(); try await waitFor { !model.referenceCatalogPending }
        XCTAssertNil(model.referenceCatalogError); XCTAssertNil(model.referenceWriteNotice)
        XCTAssertFalse(model.savedSweepReferences.isEmpty)
    }

    @MainActor func testUnexpectedSuccessHTTPTimeoutAndServerFailureDoNotClaimWriteFailedOrRetry() async throws {
        let responses = Responses(); let model = try await model(responses)
        defer { model.disconnect() }; pin(model)
        for status in [202, 408, 500] {
            responses.configure(status: status)
            let writes = responses.writeCount
            model.savePinnedSweepReference()
            try await waitFor { !model.referenceSavePending }
            XCTAssertNotNil(model.referenceWriteNotice)
            XCTAssertNil(model.referenceSaveStatus)
            model.savePinnedSweepReference()
            XCTAssertEqual(responses.writeCount, writes + 1)
            responses.configure()
            model.refreshSweepReferences()
            try await waitFor { !model.referenceCatalogPending }
            XCTAssertNil(model.referenceWriteNotice)
        }
    }

    @MainActor func testReadTimeoutAndCancellationClearPendingAndAllowExplicitRefresh() async throws {
        let responses = Responses(); responses.configure(holdGet: true)
        let model = try await model(responses, timeout: 500_000_000, waitCatalog: false)
        defer { model.disconnect() }
        try await waitFor { !model.referenceCatalogPending && responses.stoppedCount == 1 }
        XCTAssertNotNil(model.referenceCatalogError)
        await model.refresh(); XCTAssertEqual(responses.readCount, 1)
        model.refreshSweepReferences(); try await waitFor { responses.readCount == 2 }
        model.cancelReferenceRequest(); try await waitFor { responses.stoppedCount == 2 }
        XCTAssertNil(model.referenceWriteNotice)
        responses.release(); responses.configure()
        model.refreshSweepReferences(); try await waitFor { !model.referenceCatalogPending }
        XCTAssertEqual(model.savedSweepReferences.count, 1)
    }

    @MainActor func testScopeChangesCancelReadsWritesAndRejectLateResults() async throws {
        for saving in [false, true] {
            for change in ["session", "stream", "server", "disconnect"] {
                let responses = Responses(); let model = try await model(responses)
                pin(model); responses.configure(holdGet: !saving, holdPost: saving)
                if saving { model.savePinnedSweepReference() } else { model.refreshSweepReferences() }
                try await waitFor { responses.heldCount == 1 }
                switch change {
                case "session": model.selectSession("s2")
                case "stream": responses.snapshots.configure(stream: "tiny2"); await model.refresh()
                case "server": model.serverURL = "https://other.example.test"
                default: model.disconnect()
                }
                XCTAssertNil(model.comparisonReference)
                XCTAssertFalse(model.referenceSavePending)
                try await waitFor { responses.stoppedCount >= 1 }
                responses.release()
                XCTAssertEqual(model.referenceWriteNotice != nil, saving)
                if change == "server" || change == "disconnect" {
                    XCTAssertNil(model.referenceCatalog); XCTAssertTrue(model.savedSweepReferences.isEmpty)
                    XCTAssertFalse(model.canSaveSweepReference); XCTAssertFalse(model.canRefreshSweepReferences)
                    await model.refresh()
                    XCTAssertFalse(model.canPinComparisonReference)
                } else {
                    // A new scope may load, but can never adopt the prior scope's response.
                    try await waitFor { responses.readCount >= (saving ? 2 : 3) }
                    responses.release()
                    try await waitFor { !model.referenceCatalogPending }
                    if let catalog = model.referenceCatalog {
                        XCTAssertEqual(catalog.sessionId, change == "session" ? "s2" : "s")
                        XCTAssertEqual(catalog.streamId, change == "stream" ? "tiny2" : "tiny")
                    }
                }
                model.disconnect(); responses.release()
            }
        }
    }
}
