import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum MonitoringValue: Codable, Equatable, Sendable {
    case object([String: MonitoringValue]), array([MonitoringValue]), string(String)
    case integer(Int64), number(Double), boolean(Bool), null
    public init(from decoder: Decoder) throws {
        let box = try decoder.singleValueContainer()
        if box.decodeNil() { self = .null }
        else if let x = try? box.decode(Bool.self) { self = .boolean(x) }
        else if let x = try? box.decode(Int64.self) { self = .integer(x) }
        else if let x = try? box.decode(Double.self) { self = .number(x) }
        else if let x = try? box.decode(String.self) { self = .string(x) }
        else if let x = try? box.decode([String: MonitoringValue].self) { self = .object(x) }
        else { self = .array(try box.decode([MonitoringValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var box = encoder.singleValueContainer()
        switch self {
        case .object(let x): try box.encode(x)
        case .array(let x): try box.encode(x)
        case .string(let x): try box.encode(x)
        case .integer(let x): try box.encode(x)
        case .number(let x): try box.encode(x)
        case .boolean(let x): try box.encode(x)
        case .null: try box.encodeNil()
        }
    }
    public subscript(_ key: String) -> MonitoringValue { if case .object(let x) = self { return x[key] ?? .null }; return .null }
    public var text: String { if case .string(let x) = self { return x }; return "" }
    public var items: [MonitoringValue] { if case .array(let x) = self { return x }; return [] }
    public var bool: Bool? { if case .boolean(let x) = self { return x }; return nil }
    public var integer: Int64? { if case .integer(let x) = self { return x }; return nil }
    public var scalar: String {
        switch self {
        case .string(let x): return x
        case .integer(let x): return String(x)
        case .number(let x): return String(x)
        case .boolean(let x): return x ? "да" : "нет"
        default: return "—"
        }
    }
    public var pretty: String { let e = JSONEncoder(); e.outputFormatting = [.prettyPrinted, .sortedKeys]; return (try? String(decoding: e.encode(self), as: UTF8.self)) ?? "" }
}

extension PlatformClient {
    public func monitoring(_ path: String = "", body: [String: MonitoringValue]? = nil) async throws -> MonitoringValue {
        guard path.isEmpty || (path.hasPrefix("/") && !path.contains("?") && !path.contains("..")) else { throw PlatformError.connection("Некорректный путь проекта") }
        if let body { return try await post("/monitoring-projects" + path, body: body) }
        return try await get("/monitoring-projects" + path)
    }

    public func monitoringProject(
        _ projectID: String, resource: String = "", body: [String: MonitoringValue]? = nil
    ) async throws -> MonitoringValue {
        guard !projectID.isEmpty,
              ["", "preflight", "baselines", "analyze", "allowed", "events", "configure", "arm", "disarm", "status"].contains(resource) else {
            throw PlatformError.invalidCommand("Нужны проект и известный ресурс наблюдения")
        }
        let path = "/monitoring-projects/" + Self.segment(projectID) + (resource.isEmpty ? "" : "/" + resource)
        if let body {
            return try await post(path, body: body, timeoutInterval: resource == "disarm" ? 45 : 10)
        }
        return try await get(path)
    }

    public func monitoringEvent(projectID: String, eventID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !eventID.isEmpty else {
            throw PlatformError.invalidCommand("Нужны проект и событие")
        }
        return try await get("/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))")
    }

    /// A complete, bounded WAV artifact from the core, never a local capture device.
    public func monitoringEventAudio(
        projectID: String, eventID: String, maximumBytes: Int = 16 * 1024 * 1024
    ) async throws -> Data {
        guard !projectID.isEmpty, !eventID.isEmpty, (44...(32 * 1024 * 1024)).contains(maximumBytes) else {
            throw PlatformError.invalidCommand("Нужны проект, событие и допустимый предел WAV")
        }
        var request = URLRequest(url: try baseURL.endpoint(
            path: "/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))/audio"))
        request.httpMethod = "GET"
        request.timeoutInterval = 15
        request.setValue("audio/wav", forHTTPHeaderField: "Accept")
        // The extra byte distinguishes a complete artifact from a truncated prefix.
        let (data, response) = try await receivePrefix(request, maxBytes: maximumBytes + 1)
        try Task.checkCancellation()
        guard data.count <= maximumBytes else { throw PlatformError.responseTooLarge }
        guard let http = response as? HTTPURLResponse else {
            throw PlatformError.invalidResponse("Нет ответа на запрос WAV")
        }
        guard http.statusCode == 200 else {
            if let error = try? JSONDecoder().decode(MonitoringValue.self, from: data),
               !error["error"]["message"].text.isEmpty {
                throw PlatformError.http(status: http.statusCode, code: error["error"]["code"].text,
                                         message: error["error"]["message"].text)
            }
            throw PlatformError.http(status: http.statusCode, code: "http_error", message: "HTTP \(http.statusCode)")
        }
        let mime = (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased().split(separator: ";").first.map(String.init) ?? ""
        guard ["audio/wav", "audio/x-wav", "audio/wave"].contains(mime),
              data.count >= 44, data.prefix(4) == Data("RIFF".utf8),
              data.subdata(in: 8..<12) == Data("WAVE".utf8) else {
            throw PlatformError.invalidResponse("Сервер не вернул WAV RIFF/WAVE")
        }
        let declaredSize = data[4..<8].enumerated().reduce(UInt64(0)) { $0 | (UInt64($1.element) << ($1.offset * 8)) } + 8
        guard declaredSize == UInt64(data.count) else {
            throw PlatformError.invalidResponse("Неполный WAV: размер RIFF не совпадает")
        }
        return data
    }
}
