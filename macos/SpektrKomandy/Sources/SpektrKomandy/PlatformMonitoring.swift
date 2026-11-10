import Foundation

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
    public var pretty: String { let e = JSONEncoder(); e.outputFormatting = [.prettyPrinted, .sortedKeys]; return (try? String(decoding: e.encode(self), as: UTF8.self)) ?? "" }
}

extension PlatformClient {
    public func monitoring(_ path: String = "", body: [String: MonitoringValue]? = nil) async throws -> MonitoringValue {
        guard path.isEmpty || (path.hasPrefix("/") && !path.contains("?") && !path.contains("..")) else { throw PlatformError.connection("Некорректный путь проекта") }
        if let body { return try await post("/monitoring-projects" + path, body: body) }
        return try await get("/monitoring-projects" + path)
    }
}
