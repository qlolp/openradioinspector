import Foundation
import ORIContracts
import ORIMeasurementStream
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum PlatformBinaryStreamError: Error, Equatable, Sendable {
    case closed(code: Int), invalidSubprotocol, unexpectedText, exhaustedCursor
    case sequenceGap(expected: UInt64, received: UInt64)
}

extension PlatformClient {
    public func measurementTransport() async throws -> PlatformMeasurementTransport {
        var request = URLRequest(url: try baseURL.endpoint(path: "/capabilities"))
        request.setValue("application/json", forHTTPHeaderField: "Accept"); request.timeoutInterval = 5
        let (bytes, response) = try await receivePrefix(request, maxBytes: 2_000_001)
        guard bytes.count <= 2_000_000 else { throw PlatformError.responseTooLarge }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard status == 200 else { throw PlatformError.http(status: status, code: "capabilities", message: "Возможности узла недоступны: HTTP \(status)") }
        do {
            let root = try StrictJSON.parse(bytes).requireObject()
            guard try root["contract"]?.requireString() == "openradioinspector-platform",
                  try root["contract_version"]?.requireUInt64() == 1,
                  let data = try root["data"]?.requireObject(), let features = try data["features"]?.requireObject() else {
                throw PlatformError.invalidResponse("Некорректный контракт возможностей узла")
            }
            return try PlatformMeasurementTransport.negotiate(features: features)
        } catch let error as PlatformError { throw error }
        catch { throw PlatformError.invalidResponse("Некорректное описание возможностей узла") }
    }

    func binaryMeasurementRequest(after cursor: UInt64) throws -> URLRequest {
        var request = URLRequest(url: try baseURL.endpoint(path: "/events/ws", query: [.init(name: "after", value: String(cursor))]))
        try authorize(&request) // Exact original HTTP origin, explicit Bearer, no cookies.
        guard var target = URLComponents(url: request.url!, resolvingAgainstBaseURL: false) else { throw PlatformError.invalidURL("Некорректный адрес потока") }
        if target.scheme == "https" { target.scheme = "wss" }
        else if target.scheme == "http", baseURL.isLoopback { target.scheme = "ws" }
        else { throw PlatformError.invalidURL("Для бинарного LAN-потока требуется WSS") }
        request.url = target.url; request.timeoutInterval = 30
        request.setValue(MeasurementStream.subprotocolName, forHTTPHeaderField: "Sec-WebSocket-Protocol")
        return request
    }

    /// @concurrent keeps protobuf/JSON decoding off a calling MainActor. There
    /// is no binary AsyncStream queue: receive, consume, ACK, then receive again.
    @concurrent public func consumeMeasurements(after cursor: UInt64, using transport: PlatformMeasurementTransport,
        receive: @escaping @Sendable (PlatformMeasurementEvent) async throws -> Void) async throws {
        if transport == .serverSentEvents {
            guard let cursor = Int64(exactly: cursor) else { throw PlatformMeasurementDisplayIssue.unsupportedCursor }
            for try await event in try committedEvents(after: cursor) {
                try Task.checkCancellation(); try await receive(PlatformMeasurementEvent.serverSent(event)); try Task.checkCancellation()
            }
            return
        }
        let request = try binaryMeasurementRequest(after: cursor)
        let delegate = PlatformBinarySocketDelegate()
        let configuration = eventSessionConfiguration
        configuration.urlCredentialStorage = nil
        let queue = OperationQueue(); queue.maxConcurrentOperationCount = 1
        let connection = URLSession(configuration: configuration, delegate: delegate, delegateQueue: queue)
        let socket = connection.webSocketTask(with: request)
        socket.maximumMessageSize = MeasurementStream.maxFrameBytes
        defer { socket.cancel(with: .goingAway, reason: nil); connection.invalidateAndCancel() }
        try await withTaskCancellationHandler {
            do {
                try Task.checkCancellation(); socket.resume(); try await delegate.opened()
                var applied = cursor
                while true {
                    try Task.checkCancellation()
                    let message = try await socket.receive()
                    guard case let .data(bytes) = message else { throw PlatformBinaryStreamError.unexpectedText }
                    let event = try PlatformMeasurementEvent.decodeBinary(bytes)
                    guard applied < UInt64.max else { throw PlatformBinaryStreamError.exhaustedCursor }
                    guard event.sequence == applied + 1 else { throw PlatformBinaryStreamError.sequenceGap(expected: applied + 1, received: event.sequence) }
                    try await receive(event)
                    try Task.checkCancellation()
                    // String(UInt64) is canonical positive ASCII decimal, <=20
                    // bytes. Never ACK an enqueued or rejected consumer event.
                    try await socket.send(.string(String(event.sequence)))
                    applied = event.sequence
                }
            } catch {
                if Task.isCancelled { throw CancellationError() }
                let status = (socket.response as? HTTPURLResponse)?.statusCode ?? 0
                if status != 0 && status != 101 { throw PlatformError.http(status: status, code: "event_stream", message: "Бинарный поток недоступен: HTTP \(status)") }
                if socket.closeCode != .invalid { throw PlatformBinaryStreamError.closed(code: socket.closeCode.rawValue) }
                throw error
            }
        } onCancel: {
            delegate.fail(CancellationError()); socket.cancel(with: .goingAway, reason: nil); connection.invalidateAndCancel()
        }
    }
}

/// URLSession owns default system/local-CA trust handling. No TLS challenge is
/// overridden, and redirects are rejected before credentials can be forwarded.
private final class PlatformBinarySocketDelegate: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<Void, Error>?
    private var waiter: CheckedContinuation<Void, Error>?
    func opened() async throws {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if let result { lock.unlock(); continuation.resume(with: result) }
            else { waiter = continuation; lock.unlock() }
        }
    }
    func fail(_ error: Error) { finish(.failure(error)) }
    private func finish(_ value: Result<Void, Error>) {
        lock.lock(); guard result == nil else { lock.unlock(); return }
        result = value; let waiting = waiter; waiter = nil; lock.unlock(); waiting?.resume(with: value)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocolName: String?) {
        guard protocolName == MeasurementStream.subprotocolName else { fail(PlatformBinaryStreamError.invalidSubprotocol); webSocketTask.cancel(with: .policyViolation, reason: nil); return }
        finish(.success(()))
    }
    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        fail(PlatformBinaryStreamError.closed(code: closeCode.rawValue))
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        fail(error ?? PlatformBinaryStreamError.closed(code: 1006))
    }
}
