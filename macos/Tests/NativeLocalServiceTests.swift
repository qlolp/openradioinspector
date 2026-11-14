import Foundation
import XCTest
import Darwin
@testable import DvoynoyObzorSession

final class NativeLocalServiceTests: XCTestCase {
    func testActualPrivateUnixSocketRoundTripUsesACompleteCommandAndReply() async throws {
        let directory = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("orc-" + String(UUID().uuidString.prefix(8)))
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("s")
        var address = sockaddr_un()
        let bytes = Array(url.path.utf8) + [0]
        XCTAssertLessThanOrEqual(bytes.count, MemoryLayout.size(ofValue: address.sun_path))
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw POSIXError(.ENAMETOOLONG) }
        address.sun_family = sa_family_t(AF_UNIX); address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        let listener = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard listener >= 0 else { throw POSIXError(.EIO) }
        defer { Darwin.close(listener) }
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        guard bound == 0, chmod(url.path, 0o600) == 0, Darwin.listen(listener, 1) == 0 else { throw POSIXError(.EIO) }
        let peer = Task.detached { () throws -> Data in
            var wait = pollfd(fd: listener, events: Int16(POLLIN), revents: 0)
            guard Darwin.poll(&wait, 1, 2000) > 0 else { throw URLError(.timedOut) }
            let fd = Darwin.accept(listener, nil, nil)
            guard fd >= 0 else { throw POSIXError(.EIO) }
            defer { Darwin.close(fd) }
            var timeout = timeval(tv_sec: 2, tv_usec: 0), noSignal: Int32 = 1
            guard setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size)) == 0,
                  setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size)) == 0 else { throw POSIXError(.EIO) }
            var command = Data(), buffer = [UInt8](repeating: 0, count: 256)
            while true {
                let count = buffer.withUnsafeMutableBytes { Darwin.recv(fd, $0.baseAddress, $0.count, 0) }
                if count == 0 { break }
                guard count > 0, command.count + count <= 1024 else { throw POSIXError(.EIO) }
                command.append(contentsOf: buffer.prefix(count))
            }
            let reply = Data("{\"ok\":true,\"service_state\":\"stopped\",\"reason\":\"operator_start_required\"}".utf8)
            var offset = 0
            while offset < reply.count {
                let count = reply.withUnsafeBytes { Darwin.send(fd, $0.baseAddress!.advanced(by: offset), reply.count - offset, 0) }
                guard count > 0 else { throw POSIXError(.EIO) }; offset += count
            }
            return command
        }
        let reply: Result<Data, Error>
        do { reply = .success(try await NativeServiceSocket.request("status", socketURL: url)) }
        catch { reply = .failure(error) }
        let command = try await peer.value
        XCTAssertEqual((try JSONSerialization.jsonObject(with: command) as? [String: String])?["command"], "status")
        XCTAssertEqual(try NativeServiceReply.decode(reply.get()).reason, "operator_start_required")
    }
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
    func testRefusedStopCannotUnregisterOrClaimResourcesReleased() async {
        var unregistered = false
        let model = NativeLocalService(exchange: { command, _ in
            XCTAssertEqual(command, "stop"); return Data("{\"ok\":false}".utf8)
        }, registration: { .enabled }, unregister: { unregistered = true })
        await model.stop()
        XCTAssertFalse(unregistered)
        XCTAssertEqual(model.phase, .failed)
        XCTAssertTrue(model.message.contains("Обновление заблокировано"))
    }
    @MainActor
    func testRegistrationRecoveryNeverClaimsReleaseOrStartsAnything() async {
        var unregistered = false
        let model = NativeLocalService(exchange: { _, _ in XCTFail("recovery must not start a backend"); return Data() },
            registration: { XCTFail("recovery must not re-register automatically"); return .absent }, unregister: { unregistered = true })
        await model.removeRegistrationForRecovery()
        XCTAssertTrue(unregistered)
        XCTAssertNil(model.status)
        XCTAssertTrue(model.message.contains("Освобождение ресурсов не подтверждено"))
    }
}
