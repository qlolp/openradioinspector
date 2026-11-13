import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class NativeLocalServiceTests: XCTestCase {
    private func reply(_ changes: [String: Any] = [:]) throws -> Data {
        var status: [String: Any] = ["version": 1, "instance_id": "owned-fixture", "pid": 42,
            "ready": true, "preflight_complete": true, "guard_armed": false, "writer_owned": true]
        status.merge(changes) { _, new in new }
        return try JSONSerialization.data(withJSONObject: ["ok": true, "status": status])
    }
    func testReadinessRequiresTheCompletePrivateOwnerStatus() throws {
        XCTAssertEqual(try NativeServiceReply.decode(reply()).readyStatus?.instanceID, "owned-fixture")
        for change: [String: Any] in [["version": 2], ["instance_id": ""], ["pid": 0],
                                     ["ready": false], ["preflight_complete": false], ["writer_owned": false]] {
            XCTAssertThrowsError(try NativeServiceReply.decode(reply(change)))
        }
        XCTAssertThrowsError(try NativeServiceReply.decode(Data("{\"ok\":true}".utf8)))
    }
    func testStoppedAfterSleepNeverBecomesReady() throws {
        let value = try NativeServiceReply.decode(Data("{\"ok\":true,\"service_state\":\"stopped\",\"reason\":\"system_sleep_preflight_required\"}".utf8))
        XCTAssertNil(value.readyStatus)
        XCTAssertEqual(value.reason, "system_sleep_preflight_required")
    }
    func testRCPathAndSocketAreIndependentOfTheCheckoutAndLegacyArchive() {
        let home = URL(fileURLWithPath: "/Users/Оператор", isDirectory: true)
        let data = NativeLocalServicePaths.dataDirectory(home: home)
        XCTAssertTrue(data.path.hasSuffix("/OpenRadioInspector/macOS-RC"))
        let socket = NativeLocalServicePaths.socket(data: data, temporary: URL(fileURLWithPath: "/private/var/folders/aa/fixture/T/"))
        XCTAssertTrue(socket.lastPathComponent == "s")
        XCTAssertLessThan(socket.path.utf8.count, 104)
        XCTAssertEqual(socket.deletingLastPathComponent().lastPathComponent.count, 20)
        XCTAssertNotEqual(socket, NativeLocalServicePaths.socket(data: data.appendingPathComponent("other"), temporary: socket.deletingLastPathComponent()))
    }
    @MainActor
    func testInspectDoesNotRegisterOrStartAnything() async throws {
        let ready = try reply()
        var registrations = 0
        let model = NativeLocalService(exchange: { command, _ in
            XCTAssertEqual(command, "status"); return ready
        }, registration: { registrations += 1; return .enabled }, unregister: {})
        XCTAssertEqual(registrations, 0)
        await model.inspect()
        XCTAssertEqual(registrations, 0)
        XCTAssertEqual(model.phase, .ready)
    }
    @MainActor
    func testMissingBackgroundApprovalNeverStartsCore() async {
        let model = NativeLocalService(exchange: { _, _ in
            XCTFail("An unapproved service must not receive commands"); return Data()
        }, registration: { .approval }, unregister: {})
        await model.start()
        XCTAssertEqual(model.phase, .approval)
        XCTAssertNil(model.status)
    }
    @MainActor
    func testRefusedShutdownCannotUnregisterOrClaimResourcesReleased() async {
        var unregistered = false
        let model = NativeLocalService(exchange: { command, _ in
            XCTAssertEqual(command, "shutdown"); return Data("{\"ok\":false}".utf8)
        }, registration: { .enabled }, unregister: { unregistered = true })
        await model.stop()
        XCTAssertFalse(unregistered)
        XCTAssertEqual(model.phase, .failed)
        XCTAssertTrue(model.message.contains("Обновление заблокировано"))
    }
}
