import Darwin
import Foundation
import SpektrKomandy

final class SerialPort {
    private var fd: Int32 = -1
    private var stash = Data()

    var isOpen: Bool {
        fd >= 0
    }

    init() {}

    init(ownedDescriptor: Int32) {
        self.fd = ownedDescriptor
    }

    deinit {
        close()
    }

    static func availablePaths() -> [String] {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: "/dev") else {
            return []
        }
        return names.filter { $0.hasPrefix("cu.") }.sorted().map { "/dev/\($0)" }
    }

    func open(path: String, baud: Int) throws {
        let trimmed = path.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            throw SessionProblem.emptyPath
        }
        guard let speed = Self.speed(baud) else {
            throw SessionProblem.unsupportedBaud(baud)
        }
        if fd >= 0 {
            close()
        }
        let handle = Darwin.open(trimmed, O_RDWR | O_NOCTTY | O_NONBLOCK)
        if handle < 0 {
            throw Self.openProblem(path: trimmed, code: errno)
        }
        var settings = termios()
        if tcgetattr(handle, &settings) != 0 {
            let detail = String(cString: strerror(errno))
            Darwin.close(handle)
            throw SessionProblem.cannotOpen(path: trimmed, detail: detail)
        }
        cfmakeraw(&settings)
        settings.c_cflag &= ~tcflag_t(CSTOPB)
        settings.c_cflag &= ~tcflag_t(PARENB)
        settings.c_cflag &= ~tcflag_t(CRTSCTS)
        settings.c_cflag |= tcflag_t(CS8)
        settings.c_cflag |= tcflag_t(CLOCAL)
        settings.c_cflag |= tcflag_t(CREAD)
        if cfsetispeed(&settings, speed) != 0 || cfsetospeed(&settings, speed) != 0 {
            Darwin.close(handle)
            throw SessionProblem.cannotOpen(path: trimmed, detail: "не удалось задать скорость")
        }
        if tcsetattr(handle, TCSANOW, &settings) != 0 {
            let detail = String(cString: strerror(errno))
            Darwin.close(handle)
            throw SessionProblem.cannotOpen(path: trimmed, detail: detail)
        }
        tcflush(handle, TCIOFLUSH)
        fd = handle
        stash.removeAll()
    }

    func close() {
        let handle = fd
        fd = -1
        stash.removeAll()
        if handle >= 0 {
            Darwin.close(handle)
        }
    }

    func send(_ data: Data) throws {
        if fd < 0 {
            throw SessionProblem.notConnected
        }
        stash.removeAll()
        try writeAll(data)
    }

    func readLine(timeout: TimeInterval, label: String) throws -> String {
        var complete = false
        defer { if !complete { close() } }
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let line = takeLine() {
                complete = true
                return line
            }
            let wait = min(0.2, max(0.01, deadline.timeIntervalSinceNow))
            let chunk = try readOnce(maxLength: 512, timeout: wait)
            if !chunk.isEmpty {
                stash.append(chunk)
            }
        }
        if let line = takeLine() {
            complete = true
            return line
        }
        if stash.isEmpty {
            throw SessionProblem.timedOut(label)
        }
        let partial = String(decoding: stash, as: UTF8.self)
        stash.removeAll()
        throw SessionProblem.incompleteReply(partial)
    }

    func readUntilQuiet(quiet: TimeInterval, maximum: TimeInterval, label: String) throws -> String {
        var complete = false
        defer { if !complete { close() } }
        let deadline = Date().addingTimeInterval(maximum)
        var lastByte = Date()
        var received = false
        while Date() < deadline {
            let wait = min(quiet, max(0.01, deadline.timeIntervalSinceNow))
            let chunk = try readOnce(maxLength: 4096, timeout: wait)
            if !chunk.isEmpty {
                stash.append(chunk)
                received = true
                lastByte = Date()

                if let text = String(data: stash, encoding: .utf8) {
                    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
                    if trimmed.hasSuffix("ch>") {
                        complete = true
                        break
                    }
                }
                continue
            }

            if received, Date().timeIntervalSince(lastByte) >= quiet {
                if label.hasPrefix("scan") {
                    continue
                }
                complete = true
                break
            }
        }
        let data = stash
        stash.removeAll()
        if data.isEmpty {
            throw SessionProblem.timedOut(label)
        }
        guard complete else {
            throw SessionProblem.incompleteReply(String(decoding: data, as: UTF8.self))
        }
        return String(decoding: data, as: UTF8.self)
    }

    private func takeLine() -> String? {
        guard let lineFeed = stash.firstIndex(of: 0x0A) else {
            return nil
        }
        var end = lineFeed
        if end > stash.startIndex {
            let before = stash.index(before: end)
            if stash[before] == 0x0D {
                end = before
            }
        }
        let line = String(decoding: stash.prefix(upTo: end), as: UTF8.self)
        stash.removeSubrange(..<stash.index(after: lineFeed))
        return line
    }

    private func readOnce(maxLength: Int, timeout: TimeInterval) throws -> Data {
        if fd < 0 {
            throw SessionProblem.notConnected
        }
        var descriptor = pollfd()
        descriptor.fd = fd
        descriptor.events = Int16(POLLIN)
        let milliseconds = Int32((timeout * 1000).rounded(.up))
        let waited = poll(&descriptor, nfds_t(1), max(milliseconds, 0))
        if waited < 0 {
            if errno == EINTR {
                return Data()
            }
            throw SessionProblem.cannotOpen(path: "", detail: String(cString: strerror(errno)))
        }
        if waited == 0 {
            return Data()
        }
        var buffer = [UInt8](repeating: 0, count: maxLength)
        let count = buffer.withUnsafeMutableBytes { raw -> Int in
            guard let base = raw.baseAddress else { return 0 }
            return Darwin.read(fd, base, maxLength)
        }
        if count < 0 {
            if errno == EAGAIN {
                return Data()
            }
            throw SessionProblem.cannotOpen(path: "", detail: String(cString: strerror(errno)))
        }
        return Data(buffer.prefix(max(count, 0)))
    }

    private func writeAll(_ data: Data) throws {
        try data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            var sent = 0
            while sent < raw.count {
                let wrote = Darwin.write(fd, base.advanced(by: sent), raw.count - sent)
                if wrote > 0 {
                    sent += wrote
                    continue
                }
                if wrote < 0 && errno == EINTR {
                    continue
                }
                throw SessionProblem.cannotOpen(path: "", detail: String(cString: strerror(errno)))
            }
        }
    }

    private static func speed(_ baud: Int) -> speed_t? {
        switch baud {
        case 9600:
            return speed_t(B9600)
        case 19200:
            return speed_t(B19200)
        case 38400:
            return speed_t(B38400)
        case 57600:
            return speed_t(B57600)
        case 115200:
            return speed_t(B115200)
        default:
            return nil
        }
    }

    private static func openProblem(path: String, code: Int32) -> SessionProblem {
        switch code {
        case ENOENT, ENOTDIR:
            return .missingPort(path)
        case EACCES, EPERM:
            return .accessDenied(path)
        case EBUSY:
            return .portBusy(path)
        default:
            return .cannotOpen(path: path, detail: String(cString: strerror(code)))
        }
    }
}

protocol InstrumentConnection: AnyObject, Sendable {
    var isOpen: Bool { get async }
    func open(path: String, baud: Int) async throws
    func close() async
    func send(_ data: Data) async throws
    func readLine(timeout: TimeInterval, label: String) async throws -> String
    func readUntilQuiet(quiet: TimeInterval, maximum: TimeInterval, label: String) async throws -> String
}

actor InstrumentPort: InstrumentConnection {
    private let serial: SerialPort

    var isOpen: Bool {
        serial.isOpen
    }

    init() {
        self.serial = SerialPort()
    }

    init(ownedDescriptor: Int32) {
        self.serial = SerialPort(ownedDescriptor: ownedDescriptor)
    }

    func open(path: String, baud: Int) throws {
        try serial.open(path: path, baud: baud)
    }

    func close() {
        serial.close()
    }

    func send(_ data: Data) throws {
        try serial.send(data)
    }

    func readLine(timeout: TimeInterval, label: String) throws -> String {
        try serial.readLine(timeout: timeout, label: label)
    }

    func readUntilQuiet(quiet: TimeInterval, maximum: TimeInterval, label: String) throws -> String {
        try serial.readUntilQuiet(quiet: quiet, maximum: maximum, label: label)
    }
}
