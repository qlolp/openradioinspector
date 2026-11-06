import AppKit
import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

actor ScriptedPort: InstrumentConnection {
    let replies: [String]
    let failureIndex: Int?
    var reads = 0
    var commands: [Data] = []
    var closed = false
    var isOpen: Bool { !closed }
    init(failureIndex: Int? = nil) {
        self.failureIndex = failureIndex
        replies = ["tinySA Ultra", "tinySA4_test", "test-id"]
    }
    func open(path: String, baud: Int) {}
    func close() { closed = true }
    func send(_ data: Data) { commands.append(data) }
    func readLine(timeout: TimeInterval, label: String) throws -> String { "OK" }
    func readUntilQuiet(quiet: TimeInterval, maximum: TimeInterval, label: String) throws -> String {
        defer { reads += 1 }
        if reads == failureIndex { throw SessionProblem.timedOut(label) }
        return replies[reads % replies.count]
    }
}

final class SessionTests: XCTestCase {
    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<200 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("Timed out waiting for model state")
    }

    @MainActor func testFailedReconnectCannotInheritIdentityOrOverride() async throws {
        for index in 0..<3 {
            let first = ScriptedPort()
            let second = ScriptedPort(failureIndex: index)
            var count = 0
            let model = UltraModel(makePort: {
                count += 1
                return count == 1 ? first : second
            })
            model.connect()
            try await waitFor { !model.connecting }
            XCTAssertTrue(model.identityMatches)
            XCTAssertEqual(model.versionText, "tinySA4_test")
            model.forceDespiteIdentity = true
            model.disconnect()
            try await waitFor { !model.connected }
            model.connect()
            XCTAssertFalse(model.identityMatches)
            XCTAssertFalse(model.forceDespiteIdentity)
            XCTAssertEqual(model.versionText, "—")
            try await waitFor { !model.connecting }
            XCTAssertFalse(model.connected)
            XCTAssertFalse(model.identityMatches)
            XCTAssertEqual(model.infoText, "—")
            XCTAssertEqual(model.userNumber, "—")
            let before = await second.commands
            model.startOverview()
            await Task.yield()
            let after = await second.commands
            let closed = await second.closed
            XCTAssertEqual(before, after)
            XCTAssertTrue(closed)
            XCTAssertFalse(model.scanning)
        }
    }

    @MainActor func testWindowCloseDisconnectsBothModels() async throws {
        let receiverPort = ScriptedPort()
        let analyzerPort = ScriptedPort()
        let receiver = ArModel(makePort: { receiverPort })
        let analyzer = UltraModel(makePort: { analyzerPort })
        receiver.connect()
        analyzer.connect()
        try await waitFor { !receiver.connecting && !analyzer.connecting }
        XCTAssertTrue(receiver.connected)
        XCTAssertTrue(analyzer.connected)
        let window = NSWindow(contentRect: .zero, styleMask: [.titled], backing: .buffered, defer: false)
        let view = WindowCloseView()
        view.onClose = { receiver.disconnect(); analyzer.disconnect() }
        window.contentView = view
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: window)
        try await waitFor { !receiver.connected && !analyzer.connected }
        let receiverClosed = await receiverPort.closed
        let analyzerClosed = await analyzerPort.closed
        XCTAssertTrue(receiverClosed)
        XCTAssertTrue(analyzerClosed)
        window.contentView = nil
    }

    @MainActor func testRemoteWindowCloseOnlyDisconnectsUI() {
        let model = RemoteDualModel()
        model.connected = true
        model.status = "Сеанс ядра продолжается"
        let window = NSWindow(contentRect: .zero, styleMask: [.titled], backing: .buffered, defer: false)
        let view = WindowCloseView()
        view.onClose = { model.disconnect() }
        window.contentView = view
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: window)
        XCTAssertFalse(model.connected)
        XCTAssertNil(model.session)
        XCTAssertEqual(model.status, "Отключено от сервера; сеанс ядра не остановлен")
        window.contentView = nil
    }

    @MainActor func testRemoteTuneDoesNotInventReceiverOrMeasurement() {
        let model = RemoteDualModel()
        model.tuneLatestPeak()
        XCTAssertEqual(model.errorText, "Перестройка доступна только receiver")
        XCTAssertNil(model.latestObservation)
        XCTAssertTrue(model.current.isEmpty)
    }

    @MainActor func testRemoteCandidateDoesNotInventMeasurementOrQueue() {
        let model = RemoteDualModel()
        model.createCandidateFromLatestPeak()
        XCTAssertEqual(model.errorText, "Нет фактического dBm sweep для создания кандидата")
        XCTAssertTrue(model.candidates.isEmpty)
        model.startNextInvestigation()
        XCTAssertEqual(model.errorText, "Очередь исследований пуста")
        XCTAssertNil(model.nextInvestigation)
    }

    @MainActor func testWindowObserverOnlyCleansUpItsOwnWindow() {
        let own = NSWindow(contentRect: .zero, styleMask: [.titled], backing: .buffered, defer: false)
        let other = NSWindow(contentRect: .zero, styleMask: [.titled], backing: .buffered, defer: false)
        let view = WindowCloseView()
        var receiverStops = 0
        var analyzerStops = 0
        view.onClose = { receiverStops += 1; analyzerStops += 1 }
        own.contentView = view
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: other)
        XCTAssertEqual(receiverStops, 0)
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: own)
        XCTAssertEqual(receiverStops, 1)
        XCTAssertEqual(analyzerStops, 1)
        own.contentView = nil
        NotificationCenter.default.post(name: NSWindow.willCloseNotification, object: own)
        XCTAssertEqual(receiverStops, 1)
    }
}
