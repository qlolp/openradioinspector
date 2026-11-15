import Foundation

/// Scope of the shared instrument UI. USB and measurement logic stay in core.
public struct PlatformTinySAControlContext: Equatable, Sendable {
    public let server: PlatformBaseURL
    public let sessionID: String
    public let streamID: String

    public init(server: String, sessionID: String, streamID: String) throws {
        self.server = try PlatformBaseURL(server)
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Выберите сеанс и tinySA serial-поток")
        }
        self.sessionID = sessionID; self.streamID = streamID
    }

    public func url() throws -> URL {
        var components = URLComponents(url: server.root, resolvingAgainstBaseURL: false)
        components?.path = server.root.path + "/instrument"
        components?.queryItems = [URLQueryItem(name: "session_id", value: sessionID),
                                  URLQueryItem(name: "stream_id", value: streamID)]
        guard let value = components?.url else { throw PlatformError.invalidURL("Не удалось открыть панель tinySA") }
        return value
    }
}

extension PlatformClient {
    public func tinySASettings(sessionID: String, streamID: String) async throws -> MonitoringValue {
        let path = "/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument"
        let cached: MonitoringValue = try await get(path + "/settings")
        if cached["settings"]["start_hz"].integer != nil, cached["profile_verified"].bool == true { return cached }
        // Acquisition alone does not initialize the optional control-profile cache.
        // Inspect verifies identity/help through the same owned core actor, without changing RF settings.
        let job: MonitoringValue = try await post(path + "/operations", body: [
            "operation": MonitoringValue.string("inspect"), "parameters": .object([:]),
            "request_id": .string(UUID().uuidString)
        ])
        return try await tinySAOperationResult(path: path, job: job)
    }

    /// Uses the core's serialized actor, original revision and acknowledged operation result.
    public func setTinySASetting(sessionID: String, streamID: String, name: String,
                                 value: MonitoringValue, revision: Int64) async throws -> MonitoringValue {
        let path = "/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument"
        let job: MonitoringValue = try await post(path + "/settings", body: [
            "parameters": MonitoringValue.object(["name": .string(name), "value": value]),
            "expected_revision": .integer(revision), "request_id": .string(UUID().uuidString)
        ])
        return try await tinySAOperationResult(path: path, job: job)
    }

    public func tinySAOperation(sessionID: String, streamID: String, operation: String,
                                parameters: [String: MonitoringValue], revision: Int64? = nil) async throws -> MonitoringValue {
        let path = "/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument"
        var body: [String: MonitoringValue] = ["operation": .string(operation), "parameters": .object(parameters), "request_id": .string(UUID().uuidString)]
        if let revision { body["expected_revision"] = .integer(revision) }
        let job: MonitoringValue = try await post(path + "/operations", body: body)
        return try await tinySAOperationResult(path: path, job: job, requireStatus: false)
    }

    public func tinySACachedStatus(sessionID: String, streamID: String) async throws -> MonitoringValue {
        try await get("/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument/settings")
    }

    public func tinySAArtifacts(sessionID: String, streamID: String, offset: Int = 0) async throws -> MonitoringValue {
        try await get("/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument/artifacts",
                      query: [URLQueryItem(name: "limit", value: "50"), URLQueryItem(name: "offset", value: String(offset))])
    }

    public func tinySAArtifactJSON(sessionID: String, streamID: String, id: String) async throws -> MonitoringValue {
        try await get("/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/instrument/artifacts/\(Self.segment(id))/json")
    }

    private func tinySAOperationResult(path: String, job: MonitoringValue, requireStatus: Bool = true) async throws -> MonitoringValue {
        let id = job["operation_id"].text
        guard !id.isEmpty else { throw PlatformError.invalidResponse("Нет идентификатора операции tinySA") }
        for _ in 0..<600 {
            try Task.checkCancellation()
            let result: MonitoringValue = try await get(path + "/operations/\(Self.segment(id))")
            guard result["context_current"].bool != false else { throw PlatformError.invalidResponse("Соединение tinySA изменилось") }
            switch result["state"].text {
            case "succeeded":
                if !requireStatus { return result["result"] }
                let status = result["result"]["status"] == .null ? result["result"] : result["result"]["status"]
                guard status["revision"].integer != nil else {
                    throw PlatformError.invalidResponse("Ядро не вернуло ревизию подтверждённой настройки")
                }
                return status
            case "failed", "cancelled", "rejected", "expired", "unknown":
                throw PlatformError.invalidCommand(result["error"].text.isEmpty ? "Настройка tinySA не подтверждена" : result["error"].text)
            default: try await Task.sleep(for: .milliseconds(250))
            }
        }
        throw PlatformError.connection("Истекло ожидание настройки. Операция может ещё выполняться; проверьте состояние ядра перед повтором.")
    }
}
