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
            return "Ответ сервера превышает 2 МБ"
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
    let normalized = host.lowercased()
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
    private var finished = false
    var completion: ((Result<(Data, URLResponse), Error>) -> Void)?

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
        guard !finished else { return }
        finished = true
        completion?(result)
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

public struct PlatformInterval: Codable, Equatable {
    public let startedAt: String
    public let endedAt: String
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

    private enum CodingKeys: String, CodingKey {
        case id, sessionId, streamId, interval, frequenciesHz, levels, levelUnit
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

    public func analyticsSummary(sessionID: String) async throws -> PlatformAnalyticsSummary {
        try await get("/sessions/\(Self.segment(sessionID))/analytics/summary")
    }

    public func snapshot(sessionID: String) async throws -> PlatformSnapshot {
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
        let analytics: PlatformAnalyticsSummary?
        let analyticsError: String?
        do {
            analytics = try await analyticsSummary(sessionID: sessionID)
            analyticsError = nil
        } catch {
            analytics = nil
            analyticsError = error.localizedDescription
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
            analytics: analytics, analyticsError: analyticsError,
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
            query: [URLQueryItem(name: "stream_id", value: streamID), URLQueryItem(name: "limit", value: String(limit))]
        )
    }

    func get<Value: Decodable>(
        _ path: String, query: [URLQueryItem] = []
    ) async throws -> Value {
        try await request(method: "GET", path: path, query: query, body: nil as EmptyBody?)
    }

    func post<Value: Decodable, Body: Encodable>(
        _ path: String, body: Body
    ) async throws -> Value {
        try await request(method: "POST", path: path, query: [], body: body)
    }

    private func request<Value: Decodable, Body: Encodable>(
        method: String, path: String, query: [URLQueryItem], body: Body?
    ) async throws -> Value {
        var request = URLRequest(url: try baseURL.endpoint(path: path, query: query))
        request.httpMethod = method
        request.timeoutInterval = 5
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
                (data, response) = try await receiveWithDelegate(request)
            }
            if let original = request.url, let final = (response as? HTTPURLResponse)?.url,
               !platformSameOrigin(original, final) {
                throw PlatformError.connection("Перенаправление на другой origin отклонено")
            }
        } catch let error as PlatformError {
            throw error
        } catch {
            throw PlatformError.connection(error.localizedDescription)
        }
        guard data.count <= maximumResponseBytes else { throw PlatformError.responseTooLarge }
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
        do {
            return try decoder.decode(PlatformEnvelope<Value>.self, from: data).data
        } catch let error as PlatformError {
            throw error
        } catch {
            throw PlatformError.invalidResponse("Некорректный ответ API: \(error.localizedDescription)")
        }
    }

    private func receiveWithDelegate(_ request: URLRequest) async throws -> (Data, URLResponse) {
        let delegate = PlatformResponseDelegate(maximumBytes: maximumResponseBytes)
        let session = URLSession(
            configuration: .ephemeral, delegate: delegate, delegateQueue: nil
        )
        defer { session.invalidateAndCancel() }
        return try await withCheckedThrowingContinuation { continuation in
            delegate.completion = { result in
                continuation.resume(with: result)
            }
            session.dataTask(with: request).resume()
        }
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
