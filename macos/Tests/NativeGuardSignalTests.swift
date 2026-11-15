import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeGuardSignalTests: XCTestCase {
    final class Stub: URLProtocol {
        static var handle: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handle?(self) }
        override func stopLoading() {}

        func submittedBody() -> Data? {
            if let body = request.httpBody { return body }
            guard let stream = request.httpBodyStream else { return nil }
            stream.open()
            defer { stream.close() }
            var body = Data()
            var buffer = [UInt8](repeating: 0, count: 512)
            while true {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count < 0 { return nil }
                if count == 0 { return body }
                body.append(contentsOf: buffer.prefix(count))
            }
        }

        func reply(_ value: Any, status: Int = 200) {
            let url = request.url!
            let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": "application/json"])!
            let data = try! JSONSerialization.data(withJSONObject: ["contract": "openradioinspector-platform",
                "contract_version": 1, "data": value])
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    override func tearDown() {
        Stub.handle = nil
        super.tearDown()
    }

    @MainActor
    private func makeModel(handler: @escaping (Stub) -> Void) throws -> MonitoringWorkspaceModel {
        Stub.handle = handler
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: configuration))
        let model = MonitoringWorkspaceModel()
        model.configure("https://example.test", client: client)
        model.selected = "p"
        return model
    }

    @MainActor
    func testLegacySignalsUseExistingRoutesAndDoNotSelectGuardEvent() async throws {
        let event: [String: Any] = ["id": "guard-event", "project_id": "p", "analysis": ["state": "observed"]]
        let model = try makeModel { stub in
            switch stub.request.url!.path {
            case "/api/v1/capabilities":
                stub.reply(["features": ["monitoring_scan_plans": true]])
            case "/api/v1/monitoring-projects/p/events":
                let query = URLComponents(url: stub.request.url!, resolvingAgainstBaseURL: false)?.queryItems
                XCTAssertTrue(query?.contains(.init(name: "recent", value: "true")) == true)
                if query?.contains(.init(name: "after", value: "guard-event")) == true {
                    stub.reply(["items": [["id": "older-event"]]])
                } else {
                    stub.reply(["items": [event], "next_cursor": "guard-event"])
                }
            case "/api/v1/monitoring-projects/p/events/guard-event":
                stub.reply(event)
            default:
                XCTFail("Unexpected legacy request \(stub.request.url!.path)")
                stub.reply([:], status: 404)
            }
        }
        model.selectedEvent = "other-guard-selection"
        await model.loadMonitoringCapabilities()
        XCTAssertFalse(model.signalCatalogAvailable)
        XCTAssertTrue(model.scanPlansAvailable)
        await model.pollSignalEvents()
        let row = try XCTUnwrap(model.signalQueue.rows.first)
        XCTAssertEqual(row.recognition, "Нет классификации")
        XCTAssertEqual(row.receiver, "Есть наблюдение AR-DV1")
        XCTAssertTrue(model.signalQueue.unread.isEmpty)
        XCTAssertNotNil(model.signalQueue.refreshedAt)
        model.selectSignalEvent(row.id)
        await model.loadSignalEvent(row.id)
        await model.loadKnownSignals()
        XCTAssertEqual(model.signalEvent.exactID, row.id)
        XCTAssertEqual(model.selectedEvent, "other-guard-selection")
        await model.olderSignalEvents()
        XCTAssertEqual(model.signalQueue.rows.first?.id, "older-event")
    }

    @MainActor
    func testOlderInboxPageAndItsSelectionSurviveLivePolling() async throws {
        let lock = NSLock()
        var latestReads = 0
        let model = try makeModel { stub in
            let url = stub.request.url!
            switch url.path {
            case "/api/v1/capabilities":
                stub.reply(["features": ["monitoring_signal_catalog": true]])
            case "/api/v1/monitoring-projects/p/signal-inbox":
                let after = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "after" }?.value
                switch after {
                case "cursor+1/?": stub.reply(["items": [["id": "old-1"]], "next_cursor": "cursor-2"])
                case "cursor-2": stub.reply(["items": [["id": "old-2"]], "next_cursor": "last-record"])
                case "last-record": stub.reply(["items": []])
                case nil:
                    let ids = lock.withLock {
                        latestReads += 1
                        return latestReads == 1 ? ["live"] : ["new-live", "live"]
                    }
                    stub.reply(["items": ids.map { ["id": $0] }, "next_cursor": "cursor+1/?"])
                default:
                    XCTFail("Unexpected inbox cursor \(after ?? "")")
                    stub.reply([:], status: 400)
                }
            case "/api/v1/monitoring-projects/p/events/old-1/recognition":
                stub.reply(["id": "old-1"])
            default:
                XCTFail("Unexpected inbox request \(url.path)")
                stub.reply([:], status: 404)
            }
        }
        await model.loadMonitoringCapabilities()
        await model.pollSignalEvents()
        XCTAssertTrue(model.signalQueue.isLatest)
        await model.olderSignalEvents()
        XCTAssertEqual(model.signalQueue.position?.page, 2)
        model.selectSignalEvent("old-1")
        await model.loadSignalEvent("old-1")

        await model.pollSignalEvents()
        XCTAssertEqual(model.signalQueue.rows.map(\.id), ["old-1"])
        XCTAssertEqual(model.signalQueue.selectedID, "old-1")
        XCTAssertEqual(model.signalEvent.exactID, "old-1")
        XCTAssertEqual(model.signalQueue.unread, ["new-live"])

        await model.olderSignalEvents()
        XCTAssertEqual(model.signalQueue.position?.page, 3)
        await model.olderSignalEvents()
        XCTAssertEqual(model.signalQueue.rows.map(\.id), ["old-2"])
        XCTAssertNil(model.signalQueue.olderPosition)
        XCTAssertEqual(model.signalQueue.unread, ["new-live"])
        await model.newerSignalEvents()
        XCTAssertEqual(model.signalQueue.rows.map(\.id), ["old-1"])
        model.showLatestSignalEvents()
        XCTAssertEqual(model.signalQueue.rows.map(\.id), ["new-live", "live"])
        XCTAssertEqual(model.signalQueue.unread, ["new-live"])
        XCTAssertNil(model.signalQueue.selectedID)
    }

    @MainActor
    func testDetailRefreshesWhenRecognitionOrReceiverEvidenceChanges() async throws {
        let lock = NSLock()
        var recognition = "new", reason = "missing_receiver_readback", match = "Первое название"
        var detailReads = 0
        let model = try makeModel { stub in
            let path = stub.request.url!.path
            let event: [String: Any] = lock.withLock {
                let detailed = path.hasSuffix("/recognition")
                if detailed { detailReads += 1 }
                var recognized: [String: Any] = ["state": recognition]
                if detailed { recognized["known_matches"] = [["name": match]] }
                return ["id": "event-1", "last_seen": "2026-11-15T10:00:00Z", "recognition": recognized,
                    "analysis": ["state": "observed", "verification": "inconclusive", "verification_evidence": ["reason": reason]]]
            }
            switch path {
            case "/api/v1/capabilities": stub.reply(["features": ["monitoring_signal_catalog": true]])
            case "/api/v1/monitoring-projects/p/signal-inbox": stub.reply(["items": [event]])
            case "/api/v1/monitoring-projects/p/events/event-1/recognition": stub.reply(event)
            default:
                XCTFail("Unexpected refresh request \(path)")
                stub.reply([:], status: 404)
            }
        }
        await model.loadMonitoringCapabilities()
        await model.pollSignalEvents()
        model.selectSignalEvent("event-1")
        await model.loadSignalEvent("event-1")
        await model.pollSignalEvents()
        XCTAssertEqual(lock.withLock { detailReads }, 1)

        lock.withLock { recognition = "similar" }
        await model.pollSignalEvents()
        XCTAssertEqual(model.signalEvent["recognition"]["state"].text, "similar")
        lock.withLock { reason = "no_coincident_tinysa_source" }
        await model.pollSignalEvents()
        XCTAssertEqual(model.signalEvent["analysis"]["verification_evidence"]["reason"].text, "no_coincident_tinysa_source")
        XCTAssertEqual(lock.withLock { detailReads }, 3)

        lock.withLock { match = "Название другого оператора" }
        await model.refreshSignalPage()
        XCTAssertEqual(model.signalEvent["recognition"]["known_matches"].items.first?["name"].text, "Название другого оператора")
    }

    @MainActor
    func testTransientCapabilitiesFailureRetries() async throws {
        let lock = NSLock()
        var reads = 0
        let model = try makeModel { stub in
            XCTAssertEqual(stub.request.url?.path, "/api/v1/capabilities")
            let count = lock.withLock { reads += 1; return reads }
            if count == 1 { stub.reply([:], status: 503) }
            else { stub.reply(["features": ["monitoring_signal_catalog": true]]) }
        }
        await model.loadMonitoringCapabilities()
        XCTAssertTrue(model.signalCatalogAvailable)
        XCTAssertEqual(lock.withLock { reads }, 2)
    }

    @MainActor
    func testReadyCatalogUsesRecognitionRoutesAndExactPaginationCursor() async throws {
        let event: [String: Any] = ["id": "event-1", "project_id": "p", "recognition": ["state": "similar"],
            "analysis": ["verification": "observed_both"]]
        let catalogRow: [String: Any] = ["id": "known-1", "project_id": "p", "event_id": "event-1",
            "name": "Учебная запись", "kind": "short_range"]
        let cursor = "known+cursor/?"
        let model = try makeModel { stub in
            let url = stub.request.url!
            let parameters = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            switch url.path {
            case "/api/v1/capabilities":
                stub.reply(["features": ["monitoring_signal_catalog": true]])
            case "/api/v1/monitoring-projects/p/signal-inbox":
                XCTAssertEqual(parameters, [.init(name: "limit", value: "100")])
                stub.reply(["items": [event]])
            case "/api/v1/monitoring-projects/p/events/event-1/recognition":
                stub.reply(event)
            case "/api/v1/monitoring-projects/p/known-signals" where stub.request.httpMethod == "POST":
                let submitted = stub.submittedBody().flatMap { try? JSONDecoder().decode(MonitoringValue.self, from: $0) }
                XCTAssertEqual(submitted, .object(["event_id": .string("event-1"),
                    "name": .string("Учебная запись"), "kind": .string("short_range")]))
                stub.reply(catalogRow, status: 201)
            case "/api/v1/monitoring-projects/p/known-signals":
                if let before = parameters.first(where: { $0.name == "before" }) {
                    XCTAssertEqual(before.value, cursor)
                    stub.reply(["items": [["id": "known-older"]]])
                } else {
                    stub.reply(["items": [catalogRow], "next_cursor": cursor])
                }
            default:
                XCTFail("Unexpected catalog request \(url.path)")
                stub.reply([:], status: 404)
            }
        }
        await model.loadMonitoringCapabilities()
        XCTAssertTrue(model.signalCatalogAvailable)
        await model.pollSignalEvents()
        let row = try XCTUnwrap(model.signalQueue.rows.first)
        XCTAssertEqual(row.recognition, "Похож на известный")
        XCTAssertEqual(row.receiver, "Отмечен обоими приборами")
        model.selectSignalEvent(row.id)
        await model.loadSignalEvent(row.id)
        XCTAssertEqual(model.signalEvent.exactID, row.id)

        await model.loadKnownSignals()
        XCTAssertTrue(model.knownSignalsHasNext)
        await model.loadKnownSignals(page: 1)
        XCTAssertEqual(model.knownSignalsPage, 1)
        XCTAssertEqual(model.knownSignals.first?.exactID, "known-older")
        XCTAssertFalse(model.knownSignalsHasNext)
        let saved = await model.saveKnownSignal(eventID: row.id, name: " Учебная запись ", kind: .shortRange)
        XCTAssertTrue(saved)
        XCTAssertEqual(model.knownSignalsPage, 0)
        XCTAssertEqual(model.knownSignals.first?.exactID, "known-1")
        XCTAssertTrue(model.knownSignalNotice.contains("Учебная запись"))
    }

    @MainActor
    func testArchiveResetRejectsHeldSignalDetailForSameProject() async throws {
        let held = expectation(description: "detail request has started")
        let lock = NSLock()
        var pending: Stub?
        let model = try makeModel { stub in
            switch stub.request.url!.path {
            case "/api/v1/monitoring-projects/p/events":
                stub.reply(["items": [["id": "event-1"]]])
            case "/api/v1/monitoring-projects/p/events/event-1":
                lock.withLock { pending = stub }
                held.fulfill()
            default:
                XCTFail("Unexpected detail request \(stub.request.url!.path)")
                stub.reply([:], status: 404)
            }
        }
        await model.pollSignalEvents()
        model.selectSignalEvent("event-1")
        let request = Task { await model.loadSignalEvent("event-1") }
        await fulfillment(of: [held], timeout: 2)
        XCTAssertTrue(model.signalPending.contains(.detail))
        model.resetArchiveContext(watermark: 0)
        lock.withLock { pending }?.reply(["id": "event-1"])
        await request.value
        XCTAssertEqual(model.selected, "p")
        XCTAssertNil(model.signalQueue.selectedID)
        XCTAssertNil(model.signalQueue.refreshedAt)
        XCTAssertEqual(model.signalEvent, .null)
        XCTAssertTrue(model.signalPending.isEmpty)
        XCTAssertTrue(model.signalErrors.isEmpty)
    }

    @MainActor
    func testBandPlanReadIsCachedForClientAndFilteredByDisplayedRange() async throws {
        let lock = NSLock()
        var reads = 0
        let bands = [
            ["start_hz": 88_000_000, "end_hz": 108_000_000, "label": "Учебное вещание"],
            ["start_hz": 108_000_001, "end_hz": 137_000_000, "label": "Учебный участок"],
        ] as [[String: Any]]
        let model = try makeModel { stub in
            switch stub.request.url!.path {
            case "/api/v1/capabilities":
                stub.reply(["features": ["monitoring_signal_catalog": true]])
            case "/api/v1/band-plan":
                lock.withLock { reads += 1 }
                XCTAssertNil(stub.request.url?.query)
                stub.reply(["profile": "ru", "version": 1, "items": bands])
            default:
                XCTFail("Unexpected band plan request \(stub.request.url!.path)")
                stub.reply([:], status: 404)
            }
        }
        await model.loadMonitoringCapabilities()
        await model.loadBandPlan()
        model.selected = "another-project"
        await model.loadBandPlan()
        XCTAssertEqual(lock.withLock { reads }, 1)
        let visible = NativeBandPlanBandRow.overlapping(model.bandPlan["items"].items, range: 90_000_000...108_000_000)
        XCTAssertEqual(visible.map { $0["label"].text }, ["Учебное вещание"])
        model.configure("", client: nil)
        XCTAssertEqual(model.bandPlan, .null)
    }

    func testInboxArrivalAndOriginalPreserveExactIDs() throws {
        let old = PlatformExactID("event-\u{00e9}"), new = PlatformExactID("event-e\u{0301}")
        let first: MonitoringValue = .object(["id": .string(old.rawValue)])
        let second: MonitoringValue = .object(["id": .string(new.rawValue)])
        let date = Date(timeIntervalSince1970: 100)
        var inbox = NativeGuardSignalQueue()
        inbox.update(.object(["items": .array([first])]), at: date)
        XCTAssertTrue(inbox.unread.isEmpty)
        inbox.update(.object(["items": .array([second, first])]), at: date.addingTimeInterval(5))
        XCTAssertEqual(inbox.unread, [new])
        inbox.select(old)
        XCTAssertEqual(inbox.unread, [new])
        inbox.select(new)
        XCTAssertTrue(inbox.unread.isEmpty)
        XCTAssertEqual(inbox.selected?.id, new)

        let sourceSweep = PlatformExactID("sweep-e\u{0301}")
        let sources: MonitoringValue = .array([
            .object(["session_id": .string("archived"), "stream_id": .string("tiny"), "sweep_id": .string(sourceSweep.rawValue)]),
            .object(["session_id": .string("wrong"), "stream_id": .string("tiny"), "sweep_id": .string("sweep-\u{00e9}")]),
        ])
        let detail = NativeGuardSignal(value: .object(["run_session_id": .string("archived"), "source_refs": sources]))
        XCTAssertEqual(detail.source?.sweepID, sourceSweep)

        let peakSource: MonitoringValue = .object(["session_id": .string("archived"), "stream_id": .string("tiny"), "sweep_id": .string("sweep-peak")])
        let withPeak = NativeGuardSignal(value: .object(["run_session_id": .string("archived"), "source_refs": sources,
            "peak_source": peakSource]))
        XCTAssertEqual(withPeak.source?.sweepID, "sweep-peak")

        let check: MonitoringValue = .object(["reason": .string("coincident_receipt_bound_observations"), "simulated": .boolean(true),
            "analyzer": .object(["session_id": .string("archived"), "stream_id": .string("tiny"), "sweep_id": .string("sweep-overlap")])])
        let correlated = NativeGuardSignal(value: .object(["run_session_id": .string("archived"), "source_refs": sources,
            "peak_source": peakSource, "analysis": .object(["verification": .string("observed_both"), "verification_evidence": check])]))
        XCTAssertEqual(correlated.source?.sweepID, "sweep-overlap")
        XCTAssertEqual(correlated.receiver, "Оба наблюдения · симуляция")
        XCTAssertNotNil(correlated.receiverReason)
    }

    func testReceiverReviewExpiryAndSimulatedInconclusiveLabels() {
        func result(state: String, reason: String, simulated: Bool = false) -> NativeGuardSignal {
            NativeGuardSignal(value: .object(["analysis": .object(["state": .string(state), "verification": .string("inconclusive"),
                "verification_evidence": .object(["reason": .string(reason), "simulated": .boolean(simulated)])])]))
        }
        XCTAssertEqual(result(state: "expired", reason: "event_expired").receiver, "Проверка просрочена")
        XCTAssertEqual(result(state: "interrupted", reason: "event_reviewed").receiver, "Проверка отменена оператором")
        XCTAssertEqual(result(state: "observed", reason: "missing_receiver_readback", simulated: true).receiver,
            "Приём не подтверждён · симуляция")
        let legacy = NativeGuardSignal(value: .object(["analysis": .object([
            "state": .string("expired"), "reason": .string("Сигнал исчез до перестройки"),
        ])]))
        XCTAssertEqual(legacy.receiver, "Проверка просрочена")
        XCTAssertEqual(legacy.receiverReason, "Возраст события превысил окно сопоставления.")
    }
}
