import Darwin
import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class TransportTests: XCTestCase {
    private func pair() throws -> [Int32] {
        var fds: [Int32] = [-1, -1]
        guard socketpair(AF_UNIX, SOCK_STREAM, 0, &fds) == 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
        }
        return fds
    }

    private func write(_ string: String, to fd: Int32) {
        let bytes = Array(string.utf8)
        XCTAssertEqual(bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress!, $0.count) }, bytes.count)
    }

    private func waitForCommand(on fd: Int32) async throws {
        for _ in 0..<200 {
            var descriptor = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            if poll(&descriptor, 1, 0) > 0 {
                var byte: UInt8 = 0
                XCTAssertEqual(Darwin.read(fd, &byte, 1), 1)
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("No synthetic command received")
    }

    func testCancellationDrainsLateReplyBeforeNextCommand() async throws {
        let fds = try pair()
        defer { Darwin.close(fds[1]) }
        let port = InstrumentPort(ownedDescriptor: fds[0])
        let pending = Task {
            try await port.send(Data([0x31]))
            return try await port.readLine(timeout: 1, label: "old")
        }
        try await waitForCommand(on: fds[1])
        pending.cancel()
        write("old-reply\r\n", to: fds[1])
        let old = try await pending.value
        XCTAssertEqual(old, "old-reply")
        try await port.send(Data([0x32]))
        write("new-reply\r\n", to: fds[1])
        let next = try await port.readLine(timeout: 1, label: "new")
        XCTAssertEqual(next, "new-reply")
        await port.close()
    }

    func testIncompleteCanceledReplyClosesPort() async throws {
        let fds = try pair()
        defer { Darwin.close(fds[1]) }
        let port = InstrumentPort(ownedDescriptor: fds[0])
        let pending = Task {
            try await port.send(Data([0x31]))
            return try await port.readLine(timeout: 0.1, label: "old")
        }
        try await waitForCommand(on: fds[1])
        pending.cancel()
        do { _ = try await pending.value; XCTFail("Expected timeout") } catch {}
        do { try await port.send(Data([0x32])); XCTFail("Unsafe reuse after incomplete reply") } catch {}
        XCTAssertEqual(fcntl(fds[0], F_GETFD), -1)
    }

    func testQuietReadDrainsAfterCancellation() async throws {
        let fds = try pair()
        defer { Darwin.close(fds[1]) }
        let port = InstrumentPort(ownedDescriptor: fds[0])
        let pending = Task {
            try await port.send(Data([0x31]))
            return try await port.readUntilQuiet(quiet: 0.03, maximum: 1, label: "old")
        }
        try await waitForCommand(on: fds[1])
        pending.cancel()
        write("old", to: fds[1])
        let old = try await pending.value
        XCTAssertEqual(old, "old")
        try await port.send(Data([0x32]))
        write("new", to: fds[1])
        let next = try await port.readUntilQuiet(quiet: 0.03, maximum: 1, label: "new")
        XCTAssertEqual(next, "new")
        await port.close()
    }

    func testDeinitClosesOwnedDescriptor() throws {
        let fds = try pair()
        defer { Darwin.close(fds[1]) }
        var port: SerialPort? = SerialPort(ownedDescriptor: fds[0])
        XCTAssertNotNil(port)
        port = nil
        XCTAssertEqual(fcntl(fds[0], F_GETFD), -1)
        XCTAssertEqual(errno, EBADF)
    }
}
