import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum PlatformError: Error, Equatable {
    case invalidURL(String)
    case connection(String)
    case http(status: Int, code: String, message: String)
    case responseTooLarge
    case invalidResponse(String)
    case invalidCommand(String)
}

extension PlatformError: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .invalidURL(let message), .invalidResponse(let message),
             .invalidCommand(let message):
            return message
        case .connection(let message):
            return "Сервер недоступен: \(message)"
        case .http(_, _, let message):
            return message
        case .responseTooLarge:
            return "Ответ сервера превышает допустимый размер"
        }
    }
}

public struct PlatformBaseURL: Equatable {
    public let root: URL
    private let apiRootPath: String

    public init(_ text: String) throws {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: value),
              let scheme = components.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              components.host != nil else {
            throw PlatformError.invalidURL("Адрес сервера должен начинаться с http:// или https://")
        }
        guard components.user == nil, components.password == nil else {
            throw PlatformError.invalidURL("Учётные данные в адресе сервера не поддерживаются")
        }
        if scheme == "http", !platformLoopbackHost(components.host!) {
            throw PlatformError.invalidURL("Для не-loopback сервера требуется https://")
        }
        guard components.query == nil, components.fragment == nil else {
            throw PlatformError.invalidURL("Адрес сервера не должен содержать query или fragment")
        }
        var path = components.percentEncodedPath
        while path.count > 1 && path.hasSuffix("/") { path.removeLast() }
        if path.hasSuffix("/api/v1") { path.removeLast("/api/v1".count) }
        if path == "/" { path = "" }
        components.percentEncodedPath = path
        guard let url = components.url else {
            throw PlatformError.invalidURL("Некорректный адрес сервера")
        }
        root = url
        apiRootPath = path
    }

    func endpoint(path: String, query: [URLQueryItem] = []) throws -> URL {
        var components = URLComponents(url: root, resolvingAgainstBaseURL: false)
        components?.percentEncodedPath = apiRootPath + "/api/v1" + path
        if query.isEmpty {
            components?.percentEncodedQuery = nil
        } else {
            var allowed = CharacterSet.alphanumerics
            allowed.insert(charactersIn: "-._~")
            func escaped(_ value: String) -> String {
                value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
            }
            components?.percentEncodedQuery = query.map {
                "\(escaped($0.name))=\(escaped($0.value ?? ""))"
            }.joined(separator: "&")
        }
        guard let url = components?.url else {
            throw PlatformError.invalidURL("Не удалось сформировать адрес API")
        }
        return url
    }
}

private func platformLoopbackHost(_ host: String) -> Bool {
    var normalized = host.lowercased()
    if normalized.hasPrefix("["), normalized.hasSuffix("]") {
        normalized.removeFirst(); normalized.removeLast()
    }
    if normalized == "localhost" || normalized == "::1" { return true }
    let octets = normalized.split(separator: ".", omittingEmptySubsequences: false)
    guard octets.count == 4, octets[0] == "127" else { return false }
    return octets.allSatisfy { part in
        guard let value = Int(part), (0...255).contains(value) else { return false }
        return true
    }
}

private func platformSameOrigin(_ first: URL, _ second: URL) -> Bool {
    first.scheme?.lowercased() == second.scheme?.lowercased()
        && first.host?.lowercased() == second.host?.lowercased()
        && first.port == second.port
}

class PlatformRedirectGuard: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

final class PlatformResponseDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let maximumBytes: Int
    private var response: URLResponse?
    private var body = Data()
    private let completionLock = NSLock()
    private var finished = false
    private var pendingResult: Result<(Data, URLResponse), Error>?
    private var callback: ((Result<(Data, URLResponse), Error>) -> Void)?
    var completion: ((Result<(Data, URLResponse), Error>) -> Void)? {
        get {
            completionLock.lock(); defer { completionLock.unlock() }
            return callback
        }
        set {
            completionLock.lock()
            if let pending = pendingResult, let installed = newValue {
                pendingResult = nil
                completionLock.unlock()
                installed(pending)
                return
            }
            if !finished { callback = newValue }
            completionLock.unlock()
        }
    }

    init(maximumBytes: Int) {
        self.maximumBytes = maximumBytes
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }

    private func finish(_ result: Result<(Data, URLResponse), Error>) {
        completionLock.lock()
        guard !finished else { completionLock.unlock(); return }
        finished = true
        let installed = callback
        callback = nil
        if installed == nil { pendingResult = result }
        completionLock.unlock()
        // A continuation or UI callback may re-enter; never call it under lock.
        installed?(result)
    }

    func cancel() {
        finish(.failure(CancellationError()))
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        self.response = response
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        body.append(data)
        if body.count > maximumBytes {
            finish(.failure(PlatformError.responseTooLarge))
            dataTask.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if let error {
            finish(.failure(PlatformError.connection(error.localizedDescription)))
            return
        }
        guard let response else {
            finish(.failure(PlatformError.connection("Ответ сервера не получен")))
            return
        }
        finish(.success((body, response)))
    }
}

/// Collects at most `limit` bytes of an endless delivery stream, then cancels.
final class PlatformPrefixDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let limit: Int
    private var response: URLResponse?
    private var body = Data()
    private var finished = false
    var completion: ((Result<(Data, URLResponse), Error>) -> Void)?

    init(limit: Int) {
        self.limit = limit
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }

    private func finish(_ result: Result<(Data, URLResponse), Error>) {
        guard !finished else { return }
        finished = true
        completion?(result)
    }

    func cancel() {
        finish(.failure(CancellationError()))
    }

    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive response: URLResponse,
        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void
    ) {
        self.response = response
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard !finished else { return }
        body.append(data)
        if body.count >= limit, let response {
            body = Data(body.prefix(limit))
            finish(.success((body, response)))
            dataTask.cancel()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard !finished else { return }
        if let error {
            finish(.failure(PlatformError.connection(error.localizedDescription)))
            return
        }
        guard let response else {
            finish(.failure(PlatformError.connection("Ответ сервера не получен")))
            return
        }
        finish(.success((body, response)))
    }
}

final class PlatformDownloadDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    private let stagingURL: URL
    private var finished = false
    var completion: ((URLResponse?, URL?, Error?) -> Void)?

    init(stagingURL: URL) { self.stagingURL = stagingURL }

    private func finish(_ response: URLResponse?, _ location: URL?, _ error: Error?) {
        guard !finished else { return }
        finished = true
        completion?(response, location, error)
        completion = nil
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        do {
            // URLSession removes its temporary file when this callback returns.
            try FileManager.default.moveItem(at: location, to: stagingURL)
            finish(downloadTask.response, stagingURL, nil)
        } catch {
            finish(downloadTask.response, nil, error)
        }
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        finish(task.response, nil, error)
    }
}

public struct PlatformInterval: Codable, Equatable {
    public let startedAt: String
    public let endedAt: String
    public init(startedAt: String, endedAt: String) {
        self.startedAt = startedAt; self.endedAt = endedAt
    }
}

public struct PlatformStream: Decodable, Identifiable, Equatable {
    public let id: String
    public let device: String
    public let role: String
    public let levelUnit: String
    public let state: String
    public let capabilities: [String]

    public var isReceiver: Bool { role == "receiver" }
    public var isDbmAnalyzer: Bool {
        role == "spectrum_analyzer" && levelUnit == "dBm"
    }
    public var tuneModes: [String] {
        capabilities.contains("hardware_decode") ? ["FM", "AM", "DMR", "TETRA"] : ["FM", "AM"]
    }
}

public struct PlatformSession: Decodable, Identifiable, Equatable {
    public let id: String
    public let name: String
    public let state: String
    public let streams: [PlatformStream]
}

public struct PlatformWorker: Decodable, Equatable {
    public let streamId: String
    public let state: String
    public let alive: Bool
    public let lastError: String?
}

public struct PlatformSweep: Decodable, Identifiable, Equatable {
    public let id: String
    public let sessionId: String
    public let streamId: String
    public let interval: PlatformInterval
    public let frequenciesHz: [Int64]
    public let levels: [Double]
    public let levelUnit: String
    public let settings: [String: PlatformComparisonSetting]?

    private enum CodingKeys: String, CodingKey {
        case id, sessionId, streamId, interval, frequenciesHz, levels, levelUnit, settings
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionId = try container.decode(String.self, forKey: .sessionId)
        streamId = try container.decode(String.self, forKey: .streamId)
        interval = try container.decode(PlatformInterval.self, forKey: .interval)
        frequenciesHz = try container.decode([Int64].self, forKey: .frequenciesHz)
        levels = try container.decode([Double].self, forKey: .levels)
        levelUnit = try container.decode(String.self, forKey: .levelUnit)
        settings = try container.decodeIfPresent([String: PlatformComparisonSetting].self, forKey: .settings)
        guard !frequenciesHz.isEmpty, frequenciesHz.count == levels.count,
              frequenciesHz.allSatisfy({ $0 >= 0 }),
              levels.allSatisfy({ $0.isFinite }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }) else {
            throw DecodingError.dataCorruptedError(
                forKey: .levels, in: container,
                debugDescription: "Некорректные массивы частот и уровней"
            )
        }
    }
}

public struct PlatformObservation: Decodable, Identifiable, Equatable {
    public let id: String
    public let sessionId: String
    public let streamId: String
    public let interval: PlatformInterval
    public let frequencyHz: Int64
    public let mode: String
    public let levelUnit: String
    public let level: Double?
    public let squelchOpen: Bool?
    public let decodedMode: String?
}

public struct PlatformTuned: Decodable, Equatable {
    public let frequencyHz: Int64
    public let mode: String
}

public struct PlatformTuneResult: Decodable, Equatable {
    public let tuned: PlatformTuned
    public let worker: PlatformWorker
}

public struct PlatformPeak: Decodable, Equatable {
    public let sweepId: String
    public let frequencyHz: Int64
    public let level: Double
    public let levelUnit: String
}

public struct PlatformSweepSummary: Decodable, Equatable {
    public let count: Int
    public let firstStartedAt: String?
    public let lastStartedAt: String?
    public let levelUnits: [String]
    public let latest: PlatformSweep?
    public let latestPeak: PlatformPeak?
}

public struct PlatformObservationSummary: Decodable, Equatable {
    public let count: Int
    public let firstStartedAt: String?
    public let lastStartedAt: String?
    public let levelUnits: [String]
    public let latest: PlatformObservation?
    public let squelchKnown: Int
    public let squelchOpen: Int
    public let squelchOpenRatio: Double?
}

public struct PlatformAnalyticsSummary: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String?
    public let archiveWide: Bool
    public let sweeps: PlatformSweepSummary
    public let observations: PlatformObservationSummary
}

/// Global archive status. Byte counts describe the logical SQLite database, not disk free space.
public struct PlatformArchiveCapacity: Decodable, Equatable {
    public enum StorageMode: String, Decodable { case sqlite, memory }
    public enum State: String, Decodable { case unlimited, ok, warning, full, unavailable }

    public let storageMode: StorageMode
    public let databaseBytes: Int64?
    public let maxDatabaseBytes: Int64?
    public let remainingBytes: Int64?
    public let state: State
    public let warningThreshold: Double
    public let measurementWritesBlocked: Bool
    public let automaticDeletion: Bool

    private enum CodingKeys: String, CodingKey {
        case storageMode, databaseBytes, maxDatabaseBytes, remainingBytes, state
        case warningThreshold, measurementWritesBlocked, automaticDeletion
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        storageMode = try values.decode(StorageMode.self, forKey: .storageMode)
        databaseBytes = try values.decode(Int64?.self, forKey: .databaseBytes)
        maxDatabaseBytes = try values.decode(Int64?.self, forKey: .maxDatabaseBytes)
        remainingBytes = try values.decode(Int64?.self, forKey: .remainingBytes)
        state = try values.decode(State.self, forKey: .state)
        warningThreshold = try values.decode(Double.self, forKey: .warningThreshold)
        measurementWritesBlocked = try values.decode(Bool.self, forKey: .measurementWritesBlocked)
        automaticDeletion = try values.decode(Bool.self, forKey: .automaticDeletion)
        guard [databaseBytes, maxDatabaseBytes, remainingBytes].compactMap({ $0 }).allSatisfy({ $0 >= 0 }) else {
            throw DecodingError.dataCorruptedError(
                forKey: .databaseBytes, in: values, debugDescription: "Некорректная ёмкость архива"
            )
        }
        guard !automaticDeletion, warningThreshold == 0.9 else {
            throw DecodingError.dataCorruptedError(
                forKey: .automaticDeletion, in: values,
                debugDescription: "Неподдерживаемая политика ёмкости архива"
            )
        }
        if [.ok, .warning, .full].contains(state) {
            guard storageMode == .sqlite, databaseBytes != nil,
                  let limit = maxDatabaseBytes, limit > 0, remainingBytes != nil else {
                throw DecodingError.dataCorruptedError(
                    forKey: .databaseBytes, in: values,
                    debugDescription: "Неполные данные ёмкости SQLite"
                )
            }
        }
    }
}

public struct PlatformArchiveExport: Equatable {
    public let sessionId: String
    public let fileURL: URL
    public let sweeps: Int
    public let observations: Int
}

public struct PlatformSnapshot: Equatable {
    public var session: PlatformSession
    public var workers: [PlatformWorker]
    public var sweeps: [PlatformSweep]
    public var observations: [PlatformObservation]
    public var candidates: [PlatformCandidate] = []
    public var investigations: [PlatformInvestigation] = []
    public var nextInvestigation: PlatformInvestigation?
    public var automaticHandoff: PlatformAutomaticStatus?
    public var automaticDetection: PlatformAutomaticStatus?
    public var automaticCompletion: PlatformAutomaticStatus?
    public var taskScheduler: PlatformTaskSchedulerStatus?
    public var taskSchedulerError: String?
    public var taskSchedulerTasks: [PlatformControlTask] = []
    public var taskSchedulerTasksError: String?
    public var archiveCapacity: PlatformArchiveCapacity?
    public var archiveCapacityError: String?
    public var analytics: PlatformAnalyticsSummary?
    public var analyticsError: String?
    public var workflowErrors: [String] = []
}

private struct PlatformEnvelope<Value: Decodable>: Decodable {
    let data: Value

    private enum CodingKeys: String, CodingKey {
        case contract, contractVersion, data
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let contract = try container.decode(String.self, forKey: .contract)
        let version = try container.decode(Int.self, forKey: .contractVersion)
        guard contract == "openradioinspector-platform", version == 1 else {
            throw PlatformError.invalidResponse("Неподдерживаемый контракт ответа сервера")
        }
        data = try container.decode(Value.self, forKey: .data)
    }
}

private struct PlatformErrorEnvelope: Decodable {
    struct Detail: Decodable { let code: String; let message: String }
    let error: Detail
}

private struct EmptyBody: Encodable {}

private struct SessionDraft: Encodable {
    let name: String
    let streams: [StreamDraft]
}

public enum PlatformSettingValue: Encodable, Equatable {
    case string(String)
    case integer(Int)
    case floating(Double)
    case boolean(Bool)

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .integer(let value): try container.encode(value)
        case .floating(let value): try container.encode(value)
        case .boolean(let value): try container.encode(value)
        }
    }
}

private struct StreamDraft: Encodable {
    let device: String
    let role: String
    let levelUnit: String
    let port: String?
    let capabilities: [String]
    let settings: [String: PlatformSettingValue]?
}

private func platformArchiveCount(_ response: URLResponse, name: String) throws -> Int {
    guard let text = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: name),
          let value = Int(text), value >= 0, String(value) == text else {
        throw PlatformError.invalidResponse("Ответ не содержит корректный \(name)")
    }
    return value
}

private struct PlatformArchiveHeader: Decodable {
    let format: String
    let formatVersion: Int
    let sessionID: String

    private enum CodingKeys: String, CodingKey {
        case format
        case formatVersion = "format_version"
        case sessionID = "session_id"
    }
}

private func platformValidateNDJSON(
    at fileURL: URL, sessionID: String, sweeps: Int, observations: Int
) throws {
    let handle = try FileHandle(forReadingFrom: fileURL)
    defer { try? handle.close() }
    // Keep only a chunk plus the current record in memory; the archive remains on disk.
    var hasHeader = false
    var tail = Data()
    var sweepCount = 0
    var observationCount = 0
    while true {
        let chunk = try handle.read(upToCount: 256 * 1024) ?? Data()
        if chunk.isEmpty { break }
        var lines = (tail + chunk).split(separator: 0x0A, omittingEmptySubsequences: false)
        tail = lines.removeLast()
        for line in lines {
            // Blank JSON whitespace lines are accepted by the archive-stream importer.
            if line.allSatisfy({ $0 == 0x20 || $0 == 0x09 || $0 == 0x0D }) { continue }
            let record: [String: Any]
            do {
                guard let object = try JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                    throw PlatformError.invalidResponse("Строка NDJSON должна быть JSON-объектом")
                }
                record = object
            } catch {
                throw PlatformError.invalidResponse("Некорректная JSON-строка NDJSON")
            }
            if !hasHeader {
                // Typed decoding rejects JSON booleans that NSNumber can bridge to Int.
                guard Set(record.keys) == Set(["format", "format_version", "session_id"]),
                      let header = try? JSONDecoder().decode(PlatformArchiveHeader.self, from: Data(line)),
                      header.format == "openradioinspector-archive-ndjson",
                      header.formatVersion == 1, header.sessionID == sessionID else {
                    throw PlatformError.invalidResponse("Некорректный header NDJSON snapshot")
                }
                hasHeader = true
                continue
            }
            guard Set(record.keys) == Set(["kind", "measurement"]),
                  let measurement = record["measurement"] as? [String: Any],
                  let id = measurement["id"] as? String,
                  !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                throw PlatformError.invalidResponse("Некорректная запись NDJSON")
            }
            // The stream contract permits an omitted/null session_id, but not a foreign one.
            if let supplied = measurement["session_id"], !(supplied is NSNull),
               supplied as? String != sessionID {
                throw PlatformError.invalidResponse("Сеанс измерения NDJSON не совпадает с выбранным")
            }
            switch record["kind"] as? String {
            case "sweep": sweepCount += 1
            case "observation": observationCount += 1
            default: throw PlatformError.invalidResponse("Неизвестный kind записи NDJSON")
            }
        }
    }
    guard tail.isEmpty, hasHeader else {
        throw PlatformError.invalidResponse("Некорректный NDJSON snapshot")
    }
    guard sweepCount == sweeps, observationCount == observations else {
        throw PlatformError.invalidResponse("Число записей не совпадает с headers snapshot")
    }
}

public final class PlatformClient {
    public let baseURL: PlatformBaseURL
    private let session: URLSession
    private let usesInjectedSession: Bool
    private let maximumResponseBytes = 2_000_000

    public init(baseURL: String, session: URLSession? = nil) throws {
        self.baseURL = try PlatformBaseURL(baseURL)
        if let session {
            self.session = session
            self.usesInjectedSession = true
        } else {
            self.session = URLSession(
                configuration: .ephemeral,
                delegate: PlatformRedirectGuard(),
                delegateQueue: nil
            )
            self.usesInjectedSession = false
        }
    }

    deinit {
        session.invalidateAndCancel()
    }

    public func listSessions() async throws -> [PlatformSession] {
        try await get("/sessions")
    }

    public func getSession(_ id: String) async throws -> PlatformSession {
        try await get("/sessions/\(Self.segment(id))")
    }

    public func createSession(name: String, simulated: Bool) async throws -> PlatformSession {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw PlatformError.invalidCommand("Название сеанса не должно быть пустым")
        }
        let analyzer = StreamDraft(
            device: "tinySA Ultra", role: "spectrum_analyzer", levelUnit: "dBm",
            port: simulated ? "sim://tinysa" : nil,
            capabilities: ["sweep", "waterfall"],
            settings: simulated ? ["adapter": .string("tinysa-sim")] : nil
        )
        let receiver = StreamDraft(
            device: "AR-DV1", role: "receiver", levelUnit: "s_meter_count",
            port: simulated ? "sim://ardv1" : nil,
            capabilities: ["tune", "audio", "hardware_decode"],
            settings: simulated ? ["adapter": .string("ardv1-sim")] : nil
        )
        return try await post("/sessions", body: SessionDraft(name: trimmed, streams: [analyzer, receiver]))
    }

    public func createSession(
        name: String, tinySAPort: String, receiverPort: String
    ) async throws -> PlatformSession {
        try await createSerialSession(name: name, tinySAPort: tinySAPort, ardv1Port: receiverPort)
    }

    public func createSerialSession(
        name: String, tinySAPort: String, ardv1Port: String
    ) async throws -> PlatformSession {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let spectrumPort = tinySAPort.trimmingCharacters(in: .whitespacesAndNewlines)
        let targetPort = ardv1Port.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw PlatformError.invalidCommand("Название сеанса не должно быть пустым")
        }
        guard !spectrumPort.isEmpty, !targetPort.isEmpty else {
            throw PlatformError.invalidCommand("Последовательные порты не должны быть пустыми")
        }
        guard spectrumPort != targetPort else {
            throw PlatformError.invalidCommand("Порты tinySA и AR-DV1 должны различаться")
        }
        guard !spectrumPort.hasPrefix("sim://"), !targetPort.hasPrefix("sim://") else {
            throw PlatformError.invalidCommand("Serial-профиль не должен использовать sim://")
        }
        let analyzer = StreamDraft(
            device: "tinySA Ultra", role: "spectrum_analyzer", levelUnit: "dBm",
            port: spectrumPort, capabilities: ["sweep", "waterfall"],
            settings: [
                "adapter": .string("tinysa-serial"),
                "start_hz": .integer(88_000_000),
                "stop_hz": .integer(108_000_000),
                "points": .integer(201),
                "rbw_hz": .integer(10_000),
                "period_s": .floating(0.25),
            ]
        )
        let receiver = StreamDraft(
            device: "AR-DV1", role: "receiver", levelUnit: "s_meter_count",
            port: targetPort, capabilities: ["tune", "audio"],
            settings: [
                "adapter": .string("ardv1-serial"),
                "frequency_hz": .integer(145_500_000),
                "mode": .string("FM"),
                "squelch": .integer(10),
                "period_s": .floating(0.25),
            ]
        )
        return try await post("/sessions", body: SessionDraft(name: trimmed, streams: [analyzer, receiver]))
    }

    public func startSession(_ id: String) async throws -> PlatformSession {
        try await post("/sessions/\(Self.segment(id))/start", body: EmptyBody())
    }

    public func stopSession(_ id: String) async throws -> PlatformSession {
        try await post("/sessions/\(Self.segment(id))/stop", body: EmptyBody())
    }

    public func startWorkers(_ id: String) async throws -> [PlatformWorker] {
        try await post("/sessions/\(Self.segment(id))/workers/start", body: EmptyBody())
    }

    public func stopWorkers(_ id: String) async throws -> [PlatformWorker] {
        try await post("/sessions/\(Self.segment(id))/workers/stop", body: EmptyBody())
    }

    public func startStream(sessionID: String, streamID: String) async throws -> PlatformWorker {
        try await post(Self.streamPath(sessionID: sessionID, streamID: streamID, action: "start"), body: EmptyBody())
    }

    public func stopStream(sessionID: String, streamID: String) async throws -> PlatformWorker {
        try await post(Self.streamPath(sessionID: sessionID, streamID: streamID, action: "stop"), body: EmptyBody())
    }

    public func tune(
        sessionID: String, streamID: String, frequencyHz: Int64, mode: String
    ) async throws -> PlatformTuneResult {
        guard frequencyHz > 0 else {
            throw PlatformError.invalidCommand("Частота должна быть положительным целым числом Гц")
        }
        let selectedMode = mode.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !selectedMode.isEmpty else {
            throw PlatformError.invalidCommand("Режим не должен быть пустым")
        }
        struct Body: Encodable { let frequencyHz: Int64; let mode: String }
        return try await post(
            Self.streamPath(sessionID: sessionID, streamID: streamID, action: "tune"),
            body: Body(frequencyHz: frequencyHz, mode: selectedMode)
        )
    }

    public func listSweeps(sessionID: String, streamID: String) async throws -> [PlatformSweep] {
        try await measurements(kind: "sweeps", sessionID: sessionID, streamID: streamID)
    }

    public func listObservations(sessionID: String, streamID: String) async throws -> [PlatformObservation] {
        try await measurements(kind: "observations", sessionID: sessionID, streamID: streamID)
    }

    public func archiveCapacity() async throws -> PlatformArchiveCapacity {
        try await get("/archive/capacity")
    }

    public func analyticsSummary(sessionID: String) async throws -> PlatformAnalyticsSummary {
        try await get("/sessions/\(Self.segment(sessionID))/analytics/summary")
    }

    public func exportArchive(sessionID: String, destination: URL) async throws -> PlatformArchiveExport {
        let endpoint = try baseURL.endpoint(
            path: "/sessions/\(Self.segment(sessionID))/archive/export.ndjson"
        )
        var request = URLRequest(url: endpoint)
        request.httpMethod = "GET"
        request.timeoutInterval = 30
        request.setValue("application/x-ndjson", forHTTPHeaderField: "Accept")
        let temporary = FileManager.default.temporaryDirectory
            .appendingPathComponent("ori-export-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(
            at: temporary, withIntermediateDirectories: true
        )
        defer { try? FileManager.default.removeItem(at: temporary) }
        let delegate = PlatformDownloadDelegate(stagingURL: temporary.appendingPathComponent("archive.ndjson"))
        let downloadConfiguration: URLSessionConfiguration
        if usesInjectedSession {
            downloadConfiguration = session.configuration
        } else {
            downloadConfiguration = .ephemeral
        }
        let downloadSession = URLSession(
            configuration: downloadConfiguration, delegate: delegate, delegateQueue: nil
        )
        defer { downloadSession.finishTasksAndInvalidate() }
        let response: URLResponse?
        let location: URL?
        let failure: Error?
        do {
            (response, location, failure) = try await withCheckedThrowingContinuation { continuation in
                delegate.completion = { response, location, error in
                    continuation.resume(returning: (response, location, error))
                }
                downloadSession.downloadTask(with: request).resume()
            }
        } catch {
            throw PlatformError.connection(error.localizedDescription)
        }
        if let failure { throw PlatformError.connection(failure.localizedDescription) }
        guard let response, let location else {
            throw PlatformError.connection("Ответ сервера не получен")
        }
        if let original = request.url, let final = (response as? HTTPURLResponse)?.url,
           !platformSameOrigin(original, final) {
            throw PlatformError.connection("Перенаправление на другой origin отклонено")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200...299).contains(status) else {
            throw PlatformError.http(status: status, code: "http_error", message: "HTTP \(status)")
        }
        let contentType = (response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Type") ?? ""
        guard contentType.hasPrefix("application/x-ndjson") else {
            throw PlatformError.invalidResponse("Сервер вернул не NDJSON")
        }
        let sweeps = try platformArchiveCount(response, name: "X-Archive-Sweeps")
        let observations = try platformArchiveCount(response, name: "X-Archive-Observations")
        let fileManager = FileManager.default
        do {
            try platformValidateNDJSON(
                at: location, sessionID: sessionID,
                sweeps: sweeps, observations: observations
            )
            if fileManager.fileExists(atPath: destination.path) {
                _ = try fileManager.replaceItemAt(destination, withItemAt: location)
            } else {
                try fileManager.moveItem(at: location, to: destination)
            }
        } catch {
            throw error
        }
        return PlatformArchiveExport(
            sessionId: sessionID, fileURL: destination,
            sweeps: sweeps, observations: observations
        )
    }

    // UI clients that poll global capacity independently can avoid a duplicate request.
    public func snapshot(
        sessionID: String, includeArchiveCapacity: Bool = true
    ) async throws -> PlatformSnapshot {
        let session = try await getSession(sessionID)
        async let workersRequest: [PlatformWorker] = get(
            "/sessions/\(Self.segment(sessionID))/workers"
        )
        async let sweepsRequest: [PlatformSweep] = measurementsWithResponseFallback(
            kind: "sweeps", sessionID: sessionID,
            streamID: session.streams.first(where: { $0.isDbmAnalyzer })?.id
        )
        async let observationsRequest: [PlatformObservation] = measurements(
            kind: "observations", sessionID: sessionID,
            streamID: session.streams.first(where: { $0.isReceiver })?.id
        )
        var workflowErrors: [String] = []
        let candidates: [PlatformCandidate]
        do {
            candidates = try await listCandidates(sessionID: sessionID)
        } catch {
            candidates = []
            workflowErrors.append("candidates: \(error.localizedDescription)")
        }
        let investigations: [PlatformInvestigation]
        do {
            investigations = try await listInvestigations(sessionID: sessionID)
        } catch {
            investigations = []
            workflowErrors.append("investigations: \(error.localizedDescription)")
        }
        let next: PlatformInvestigation?
        do {
            next = try await nextInvestigation(sessionID: sessionID)
        } catch {
            next = nil
            workflowErrors.append("next investigation: \(error.localizedDescription)")
        }
        let handoff: PlatformAutomaticStatus?
        do {
            handoff = try await automaticHandoff(sessionID: sessionID)
        } catch {
            handoff = nil
            workflowErrors.append("automatic handoff: \(error.localizedDescription)")
        }
        let detection: PlatformAutomaticStatus?
        do {
            detection = try await automaticDetection(sessionID: sessionID)
        } catch {
            detection = nil
            workflowErrors.append("automatic detection: \(error.localizedDescription)")
        }
        let completion: PlatformAutomaticStatus?
        do {
            completion = try await automaticCompletion(sessionID: sessionID)
        } catch {
            completion = nil
            workflowErrors.append("automatic completion: \(error.localizedDescription)")
        }
        let scheduler: PlatformTaskSchedulerStatus?
        let schedulerError: String?
        do {
            scheduler = try await taskScheduler()
            schedulerError = nil
        } catch {
            scheduler = nil
            schedulerError = error.localizedDescription
        }
        let schedulerTasks: [PlatformControlTask]
        let schedulerTasksError: String?
        do {
            schedulerTasks = try await taskSchedulerTasks()
            schedulerTasksError = nil
        } catch {
            schedulerTasks = []
            schedulerTasksError = error.localizedDescription
        }
        var capacity: PlatformArchiveCapacity?
        var capacityError: String?
        if includeArchiveCapacity {
            do {
                capacity = try await archiveCapacity()
            } catch {
                capacityError = error.localizedDescription
            }
        }
        return try await PlatformSnapshot(
            session: session, workers: workersRequest,
            sweeps: sweepsRequest, observations: observationsRequest,
            candidates: candidates, investigations: investigations,
            nextInvestigation: next,
            automaticHandoff: handoff, automaticDetection: detection,
            automaticCompletion: completion,
            taskScheduler: scheduler, taskSchedulerError: schedulerError,
            taskSchedulerTasks: schedulerTasks, taskSchedulerTasksError: schedulerTasksError,
            archiveCapacity: capacity, archiveCapacityError: capacityError,
            workflowErrors: workflowErrors
        )
    }

    private func measurementsWithResponseFallback<Value: Decodable>(
        kind: String, sessionID: String, streamID: String?
    ) async throws -> [Value] {
        var limit = 40
        while true {
            do {
                return try await measurements(
                    kind: kind, sessionID: sessionID, streamID: streamID, limit: limit
                )
            } catch PlatformError.responseTooLarge {
                guard limit > 1 else { throw PlatformError.responseTooLarge }
                limit = max(1, limit / 2)
            }
        }
    }

    private func measurements<Value: Decodable>(
        kind: String, sessionID: String, streamID: String?, limit: Int = 40
    ) async throws -> [Value] {
        guard let streamID else { return [] }
        return try await get(
            "/sessions/\(Self.segment(sessionID))/\(kind)",
            query: [URLQueryItem(name: "stream_id", value: streamID), URLQueryItem(name: "limit", value: String(limit))],
            validateResponse: kind == "sweeps" ? { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            } : nil
        )
    }

    func get<Value: Decodable>(
        _ path: String, query: [URLQueryItem] = [], expectedStatuses: [Int]? = nil,
        validateResponse: ((Data) throws -> Void)? = nil
    ) async throws -> Value {
        try await request(method: "GET", path: path, query: query, body: nil as EmptyBody?, expectedStatuses: expectedStatuses, validateResponse: validateResponse)
    }

    func post<Value: Decodable, Body: Encodable>(
        _ path: String, body: Body, expectedStatuses: [Int]? = nil, timeoutInterval: TimeInterval = 5, validateResponse: ((Data) throws -> Void)? = nil
    ) async throws -> Value {
        try await request(method: "POST", path: path, query: [], body: body, expectedStatuses: expectedStatuses, timeoutInterval: timeoutInterval, validateResponse: validateResponse)
    }

    private func request<Value: Decodable, Body: Encodable>(
        method: String, path: String, query: [URLQueryItem], body: Body?, expectedStatuses: [Int]?,
        timeoutInterval: TimeInterval = 5, validateResponse: ((Data) throws -> Void)? = nil
    ) async throws -> Value {
        let responseLimit = method == "GET" && path.hasSuffix("/sweeps")
            ? 16 * 1024 * 1024 : maximumResponseBytes
        var request = URLRequest(url: try baseURL.endpoint(path: path, query: query))
        request.httpMethod = method
        request.timeoutInterval = timeoutInterval
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            request.httpBody = try encoder.encode(body)
            request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }
        let data: Data
        let response: URLResponse
        do {
            if usesInjectedSession {
                (data, response) = try await session.data(for: request)
            } else {
                (data, response) = try await receiveWithDelegate(request, maximumBytes: responseLimit)
            }
            if let original = request.url, let final = (response as? HTTPURLResponse)?.url,
               !platformSameOrigin(original, final) {
                throw PlatformError.connection("Перенаправление на другой origin отклонено")
            }
        } catch let error as PlatformError {
            if Task.isCancelled { throw CancellationError() }
            throw error
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                throw CancellationError()
            }
            throw PlatformError.connection(error.localizedDescription)
        }
        try Task.checkCancellation()
        guard data.count <= responseLimit else { throw PlatformError.responseTooLarge }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard (200...299).contains(status) else {
            if let envelope = try? decoder.decode(PlatformErrorEnvelope.self, from: data) {
                throw PlatformError.http(
                    status: status, code: envelope.error.code, message: envelope.error.message
                )
            }
            throw PlatformError.http(status: status, code: "http_error", message: "HTTP \(status)")
        }
        if let expectedStatuses, !expectedStatuses.contains(status) {
            throw PlatformError.invalidResponse("Неожиданный статус API: HTTP \(status)")
        }
        do {
            try validateResponse?(data)
            return try decoder.decode(PlatformEnvelope<Value>.self, from: data).data
        } catch let error as PlatformError {
            throw error
        } catch {
            throw PlatformError.invalidResponse("Некорректный ответ API: \(error.localizedDescription)")
        }
    }

    private func receiveWithDelegate(_ request: URLRequest, maximumBytes: Int) async throws -> (Data, URLResponse) {
        if Task.isCancelled { throw CancellationError() }
        let delegate = PlatformResponseDelegate(maximumBytes: maximumBytes)
        let session = URLSession(
            configuration: .ephemeral, delegate: delegate, delegateQueue: nil
        )
        defer { session.invalidateAndCancel() }
        return try await withTaskCancellationHandler {
            if Task.isCancelled { throw CancellationError() }
            return try await withCheckedThrowingContinuation { continuation in
                let task = session.dataTask(with: request)
                delegate.completion = { result in
                    continuation.resume(with: result)
                }
                if Task.isCancelled {
                    task.cancel()
                    delegate.cancel()
                } else {
                    task.resume()
                }
            }
        } onCancel: {
            session.invalidateAndCancel()
        }
    }

    /// Bounded raw GET for endless delivery streams: collects at most `maxBytes`, then drops the connection.
    func receivePrefix(_ request: URLRequest, maxBytes: Int) async throws -> (Data, URLResponse) {
        if Task.isCancelled { throw CancellationError() }
        let data: Data
        let response: URLResponse
        if usesInjectedSession {
            (data, response) = try await session.data(for: request)
        } else {
            let delegate = PlatformPrefixDelegate(limit: maxBytes)
            let session = URLSession(
                configuration: .ephemeral, delegate: delegate, delegateQueue: nil
            )
            defer { session.invalidateAndCancel() }
            (data, response) = try await withTaskCancellationHandler {
                if Task.isCancelled { throw CancellationError() }
                return try await withCheckedThrowingContinuation { continuation in
                    let task = session.dataTask(with: request)
                    delegate.completion = { result in
                        continuation.resume(with: result)
                    }
                    if Task.isCancelled {
                        task.cancel()
                        delegate.cancel()
                    } else {
                        task.resume()
                    }
                }
            } onCancel: {
                session.invalidateAndCancel()
            }
        }
        if let original = request.url, let final = (response as? HTTPURLResponse)?.url,
           !platformSameOrigin(original, final) {
            throw PlatformError.connection("Перенаправление на другой origin отклонено")
        }
        return (data, response)
    }

    private static func streamPath(sessionID: String, streamID: String, action: String) -> String {
        "/sessions/\(segment(sessionID))/streams/\(segment(streamID))/\(action)"
    }

    static func segment(_ value: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }
}
