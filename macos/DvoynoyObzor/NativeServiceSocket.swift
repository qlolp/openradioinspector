import Foundation
import CryptoKit
import Darwin

enum NativeServiceError: LocalizedError {
    case unavailable(String)
    var errorDescription: String? {
        switch self { case .unavailable(let message): message }
    }
}

enum NativeLocalServicePaths {
    static func dataDirectory(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appendingPathComponent("Library/Application Support/OpenRadioInspector/macOS-RC", isDirectory: true)
    }
    static func socket(data: URL = dataDirectory(), temporary: URL = FileManager.default.temporaryDirectory) -> URL {
        let digest = SHA256.hash(data: Data(data.resolvingSymlinksInPath().path.utf8))
        let name = digest.prefix(8).map { String(format: "%02x", $0) }.joined()
        return temporary.resolvingSymlinksInPath().appendingPathComponent("ori-" + name, isDirectory: true).appendingPathComponent("s")
    }
}

struct NativeServiceStatus: Decodable, Sendable {
    let version: Int
    let instanceID: String
    let pid: UInt32
    let ready, preflightComplete, guardArmed, writerOwned: Bool
    enum CodingKeys: String, CodingKey {
        case version, pid, ready
        case instanceID = "instance_id", preflightComplete = "preflight_complete"
        case guardArmed = "guard_armed", writerOwned = "writer_owned"
    }
}
struct NativeServiceReply: Decodable, Sendable {
    let ok: Bool
    let status: NativeServiceStatus?
    let serviceState, reason, error: String?
    enum CodingKeys: String, CodingKey { case ok, status, reason, error; case serviceState = "service_state" }
    var readyStatus: NativeServiceStatus? { status }
    static func decode(_ data: Data) throws -> Self {
        let value = try JSONDecoder().decode(Self.self, from: data)
        guard value.ok else { throw NativeServiceError.unavailable("Служба отказала: \(value.error ?? "неизвестная ошибка"). Симулятор не запускается.") }
        if let status = value.status {
            guard status.version == 1, !status.instanceID.isEmpty, status.pid > 0,
                  status.ready, status.preflightComplete, status.writerOwned else {
                throw NativeServiceError.unavailable("Служба не подтвердила готовность и владение архивом.")
            }
        } else if value.serviceState != "stopped" {
            throw NativeServiceError.unavailable("Неполный ответ службы.")
        }
        return value
    }
}

enum NativeServiceSocket {
    static func request(_ command: String, timeout: Duration = .seconds(2), socketURL: URL = NativeLocalServicePaths.socket()) async throws -> Data {
        let task = Task.detached(priority: .userInitiated) {
            try exchange(socket: socketURL, command: command, timeout: timeout)
        }
        return try await withTaskCancellationHandler { try await task.value } onCancel: { task.cancel() }
    }

    // Only this worker owns/closes its FD. Cancellation never closes another
    // thread's potentially reused descriptor, and all waits share one deadline.
    private static func exchange(socket url: URL, command: String, timeout: Duration) throws -> Data {
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: timeout)
        var metadata = stat()
        guard lstat(url.path, &metadata) == 0, metadata.st_uid == geteuid(),
              metadata.st_mode & S_IFMT == S_IFSOCK, metadata.st_mode & 0o077 == 0 else {
            throw NativeServiceError.unavailable("Приватный сокет службы недоступен. Проверьте разрешение фоновой службы в настройках macOS.")
        }
        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw NativeServiceError.unavailable("Не удалось открыть соединение со службой.") }
        defer { Darwin.close(fd) }
        guard fcntl(fd, F_SETFL, O_NONBLOCK) == 0 else { throw POSIXError(.EIO) }
        func wait(_ events: Int16) throws {
            while true {
                try Task.checkCancellation()
                guard clock.now < deadline else { throw URLError(.timedOut) }
                var item = pollfd(fd: fd, events: events, revents: 0)
                let result = Darwin.poll(&item, 1, 50)
                if result > 0 { return }
                if result < 0 && errno != EINTR { throw POSIXError(.EIO) }
            }
        }
        var address = sockaddr_un()
        let bytes = Array(url.path.utf8) + [0]
        guard bytes.count <= MemoryLayout.size(ofValue: address.sun_path) else { throw POSIXError(.ENAMETOOLONG) }
        address.sun_family = sa_family_t(AF_UNIX)
        address.sun_len = UInt8(MemoryLayout<sockaddr_un>.size)
        withUnsafeMutableBytes(of: &address.sun_path) { target in target.copyBytes(from: bytes) }
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { Darwin.connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
        }
        if connected != 0 {
            guard errno == EINPROGRESS else { throw POSIXError(.ECONNREFUSED) }
            try wait(Int16(POLLOUT))
            var error: Int32 = 0, size = socklen_t(MemoryLayout<Int32>.size)
            guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &size) == 0, error == 0 else { throw POSIXError(.ECONNREFUSED) }
        }
        var uid: uid_t = 0, gid: gid_t = 0
        guard getpeereid(fd, &uid, &gid) == 0, uid == geteuid() else { throw POSIXError(.EACCES) }
        var noSignal: Int32 = 1
        guard setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size)) == 0 else { throw POSIXError(.EIO) }
        let request = try JSONSerialization.data(withJSONObject: ["command": command])
        var offset = 0
        while offset < request.count {
            try wait(Int16(POLLOUT))
            let sent = request.withUnsafeBytes { Darwin.send(fd, $0.baseAddress!.advanced(by: offset), request.count - offset, 0) }
            if sent > 0 { offset += sent }
            else if sent < 0 && ![EAGAIN, EINTR].contains(errno) { throw POSIXError(.EIO) }
        }
        guard Darwin.shutdown(fd, SHUT_WR) == 0 else { throw POSIXError(.EIO) }
        var reply = Data(), buffer = [UInt8](repeating: 0, count: 4096)
        while true {
            try wait(Int16(POLLIN))
            let received = buffer.withUnsafeMutableBytes { Darwin.recv(fd, $0.baseAddress, $0.count, 0) }
            if received == 0 { return reply }
            if received > 0 {
                guard reply.count + received <= 1_048_576 else { throw POSIXError(.EMSGSIZE) }
                reply.append(contentsOf: buffer.prefix(received))
            } else if ![EAGAIN, EINTR].contains(errno) { throw POSIXError(.EIO) }
        }
    }
}
