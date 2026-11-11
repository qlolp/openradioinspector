import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum PlatformEventError: Error, Equatable, Sendable {
    case oversizedEvent, malformedEvent, consumerOverrun
    case sequenceGap(expected: Int64, received: Int64)
}

/// An event is emitted by the core only after its archive transaction commits.
public struct PlatformCommittedEvent: Decodable, Equatable, Sendable {
    public struct Payload: Decodable, Equatable, Sendable {
        public let sweep: PlatformSweep?
        public let observation: PlatformObservation?
    }
    public let sequence: Int64
    public let eventType: String
    public let occurredAt: String
    public let sessionId: String?
    public let payload: Payload
    /// Original SSE data document, including unknown fields and exact numeric
    /// tokens. Do not reconstruct opaque JSON through floating-point values.
    public fileprivate(set) var sourceJSON: Data? = nil
    private enum CodingKeys: String, CodingKey { case sequence, eventType, occurredAt, sessionId, payload }

    public static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.sequence == rhs.sequence && lhs.eventType.utf8.elementsEqual(rhs.eventType.utf8) && lhs.occurredAt == rhs.occurredAt
            && PlatformExactID.same(lhs.sessionId, rhs.sessionId) && lhs.payload == rhs.payload
    }
}

private struct PlatformEventDocument: Decodable {
    let event: PlatformCommittedEvent
    private enum CodingKeys: String, CodingKey { case contract, contractVersion, data }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        if values.contains(.contract) || values.contains(.contractVersion) || values.contains(.data) {
            // A malformed/unsupported envelope must never be retried as a bare event.
            event = try PlatformEnvelope<PlatformCommittedEvent>(from: decoder).data
        } else {
            // Compatibility for existing public parser callers. Real /api/v1
            // cores use the versioned envelope; all envelope markers are strict.
            event = try PlatformCommittedEvent(from: decoder)
        }
    }
}

/// Incremental UTF-8 SSE framing. Bounded even when a peer never sends a newline.
public struct PlatformEventParser: Sendable {
    private var line = Data()
    private var data = Data()
    private var id: String?
    private var type: String?
    private var eventBytes = 0
    private let maximumEventBytes: Int
    public var bufferedBytes: Int { eventBytes }
    public init(maximumEventBytes: Int = 1_048_576) { self.maximumEventBytes = max(1, maximumEventBytes) }

    public mutating func append(_ bytes: Data) throws -> [PlatformCommittedEvent] {
        var events: [PlatformCommittedEvent] = []
        try append(bytes) { event in
            guard events.count < 64 else { throw PlatformEventError.consumerOverrun }
            events.append(event)
        }
        return events
    }

    /// Deliver each complete event before decoding the next. A large network
    /// chunk cannot discard an already validated prefix when the queue fills.
    mutating func append(_ bytes: Data, receive: (PlatformCommittedEvent) throws -> Void) throws {
        for byte in bytes {
            eventBytes += 1
            guard eventBytes <= maximumEventBytes else { throw PlatformEventError.oversizedEvent }
            if byte != 10 { line.append(byte); continue }
            if line.last == 13 { line.removeLast() }
            if line.isEmpty {
                if !data.isEmpty {
                    data.removeLast() // SSE joins multiple data lines with a newline.
                    let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
                    var event: PlatformCommittedEvent
                    do { event = try decoder.decode(PlatformEventDocument.self, from: data).event }
                    catch { throw PlatformEventError.malformedEvent }
                    guard let id, Int64(id) == event.sequence, event.sequence > 0,
                          let type, type.utf8.elementsEqual(event.eventType.utf8),
                          (try? PlatformTimestamp(event.occurredAt)) != nil,
                          event.eventType != "sweep.received" || event.payload.sweep != nil,
                          event.eventType != "observation.received" || event.payload.observation != nil else {
                        throw PlatformEventError.malformedEvent
                    }
                    if let sweep = event.payload.sweep, event.eventType == "sweep.received" {
                        guard event.sessionId?.utf8.elementsEqual(sweep.sessionId.utf8) == true else { throw PlatformEventError.malformedEvent }
                    }
                    if let observation = event.payload.observation, event.eventType == "observation.received" {
                        guard event.sessionId?.utf8.elementsEqual(observation.sessionId.utf8) == true else { throw PlatformEventError.malformedEvent }
                    }
                    event.sourceJSON = data
                    try receive(event)
                }
                data.removeAll(keepingCapacity: true); id = nil; type = nil; eventBytes = 0
            } else if line.first != 58 {
                guard let text = String(data: line, encoding: .utf8) else { throw PlatformEventError.malformedEvent }
                let parts = text.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
                var value = parts.count > 1 ? String(parts[1]) : ""
                if value.first == " " { value.removeFirst() }
                switch parts[0] {
                case "id": id = value
                case "event": type = value
                case "data": data.append(contentsOf: value.utf8); data.append(10)
                default: break // Forward-compatible SSE extensions.
                }
            }
            line.removeAll(keepingCapacity: true)
        }
    }
}

/// Per-client bounded cache; the global cursor advances across events for every session.
public struct PlatformLiveMeasurements: Sendable {
    public enum Change: Equatable, Sendable { case measurement, metadata, duplicate }
    public private(set) var cursor: Int64
    public private(set) var sweeps: [PlatformSweep]
    public private(set) var observation: PlatformObservation?
    private let sessionID: String
    private let streamID: String
    private let capacity: Int
    private var sweepTimes: [Int64]
    private let receiverStreamID: String?

    public init(sessionID: String, streamID: String, after: Int64, capacity: Int = PlatformSpectrumPresentation.capacity,
                sweeps: [PlatformSweep] = [], observation: PlatformObservation? = nil, receiverStreamID: String? = nil) {
        self.sessionID = sessionID; self.streamID = streamID; cursor = after
        self.capacity = max(1, min(capacity, PlatformSpectrumPresentation.capacity))
        let ordered = sweeps.filter { $0.sessionId.utf8.elementsEqual(sessionID.utf8) && $0.streamId.utf8.elementsEqual(streamID.utf8) }
            .map { (value: $0, time: (try? PlatformTimestamp($0.interval.startedAt).utcMicroseconds) ?? Int64.min) }
            .sorted { a, b in a.time == b.time ? a.value.id.utf8.lexicographicallyPrecedes(b.value.id.utf8) : a.time < b.time }
        let visible = Array(ordered.suffix(self.capacity))
        self.sweeps = visible.map(\.value); sweepTimes = visible.map(\.time)
        self.receiverStreamID = receiverStreamID ?? observation?.streamId
        self.observation = observation
    }

    @discardableResult public mutating func apply(_ event: PlatformCommittedEvent) throws -> Change {
        guard event.sequence > cursor else { return .duplicate }
        guard cursor < Int64.max, event.sequence == cursor + 1 else {
            throw PlatformEventError.sequenceGap(expected: cursor == Int64.max ? cursor : cursor + 1, received: event.sequence)
        }
        cursor = event.sequence
        if let sweep = event.payload.sweep, event.eventType == "sweep.received",
           sweep.sessionId.utf8.elementsEqual(sessionID.utf8), sweep.streamId.utf8.elementsEqual(streamID.utf8) {
            guard !sweeps.contains(where: { $0.id.utf8.elementsEqual(sweep.id.utf8) }) else { return .duplicate }
            let time = try PlatformTimestamp(sweep.interval.startedAt).utcMicroseconds
            let index = sweeps.indices.first { sweepTimes[$0] > time || (sweepTimes[$0] == time && sweep.id.utf8.lexicographicallyPrecedes(sweeps[$0].id.utf8)) } ?? sweeps.count
            sweeps.insert(sweep, at: index); sweepTimes.insert(time, at: index)
            if sweeps.count > capacity { let extra = sweeps.count - capacity; sweeps.removeFirst(extra); sweepTimes.removeFirst(extra) }
            return .measurement
        }
        if let value = event.payload.observation, event.eventType == "observation.received",
           value.sessionId.utf8.elementsEqual(sessionID.utf8),
           receiverStreamID == nil || value.streamId.utf8.elementsEqual(receiverStreamID!.utf8) {
            let incoming = try PlatformTimestamp(value.interval.startedAt).utcMicroseconds
            let current = observation.flatMap { try? PlatformTimestamp($0.interval.startedAt).utcMicroseconds }
            if current == nil || incoming > current! || (incoming == current! && observation!.id.utf8.lexicographicallyPrecedes(value.id.utf8)) { observation = value }
            return .measurement
        }
        return .metadata
    }
}

extension PlatformClient {
    public func eventWatermark() async throws -> Int64 {
        struct Health: Decodable, Sendable { let lastEventId: Int64 }
        let value: Health = try await get("/health")
        guard value.lastEventId >= 0 else { throw PlatformEventError.malformedEvent }
        return value.lastEventId
    }

    /// Cancelling the iterator closes only this UI's HTTP subscription.
    public func committedEvents(after cursor: Int64) throws -> AsyncThrowingStream<PlatformCommittedEvent, Error> {
        guard cursor >= 0 else { throw PlatformError.invalidCommand("Курсор должен быть неотрицательным") }
        var request = URLRequest(url: try baseURL.endpoint(path: "/events", query: [.init(name: "after", value: String(cursor))]))
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue(String(cursor), forHTTPHeaderField: "Last-Event-ID")
        try authorize(&request)
        // The core sends heartbeats every 15 s. Do not extend normal request timeouts.
        return AsyncThrowingStream(bufferingPolicy: .bufferingOldest(64)) { continuation in
            let delegate = PlatformEventDelegate(continuation: continuation)
            let queue = OperationQueue(); queue.maxConcurrentOperationCount = 1
            let connection = URLSession(configuration: eventSessionConfiguration, delegate: delegate, delegateQueue: queue)
            let task = connection.dataTask(with: request)
            continuation.onTermination = { @Sendable _ in connection.invalidateAndCancel() }
            task.resume()
        }
    }
}

/// URLSession serializes this delegate on its single-operation queue. Stream termination
/// only calls URLSession's thread-safe cancellation API; it never touches parser state.
private final class PlatformEventDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let continuation: AsyncThrowingStream<PlatformCommittedEvent, Error>.Continuation
    private var parser = PlatformEventParser()
    private var finished = false
    init(continuation: AsyncThrowingStream<PlatformCommittedEvent, Error>.Continuation) { self.continuation = continuation }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased().hasPrefix("text/event-stream") else {
            finished = true
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            continuation.finish(throwing: PlatformError.http(status: status, code: "event_stream", message: "Поток событий недоступен: HTTP \(status)"))
            completionHandler(.cancel); session.invalidateAndCancel(); return
        }
        completionHandler(.allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !finished else { return }
        do {
            try parser.append(data) { event in
                switch continuation.yield(event) {
                case .enqueued: break
                case .dropped: throw PlatformEventError.consumerOverrun
                case .terminated: session.invalidateAndCancel(); throw CancellationError()
                @unknown default: throw PlatformEventError.consumerOverrun
                }
            }
        } catch { finished = true; continuation.finish(throwing: error); session.invalidateAndCancel() }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if !finished { continuation.finish(throwing: error) }
        session.finishTasksAndInvalidate()
    }
}
