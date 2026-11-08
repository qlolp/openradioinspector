import Foundation

/// One measurement-map request: the portable artifact text alone.
public struct PlatformMeasurementMapRequest: Encodable {
    public let mapJson: String
    public init(mapJson: String) { self.mapJson = mapJson }
}

/// The built map: GeoJSON FeatureCollection plus a summary of the supplied points.
public struct PlatformMeasurementMap: Decodable, Equatable {
    public struct Summary: Decodable, Equatable {
        public let pointCount: Int
        public let sectorCount: Int
        public let bbox: [String: Double]
    }
    public let sessionId: String
    public let geojson: [String: AnyCodable]
    public let summary: Summary
}

/// Minimal type-erased JSON value for GeoJSON passthrough without re-encoding rules.
public struct AnyCodable: Codable, Equatable {
    public let value: Any
    public init(_ value: Any) { self.value = value }
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { value = NSNull() }
        else if let number = try? container.decode(Bool.self) { value = number }
        else if let number = try? container.decode(Int.self) { value = number }
        else if let number = try? container.decode(Double.self) { value = number }
        else if let text = try? container.decode(String.self) { value = text }
        else if let array = try? container.decode([AnyCodable].self) { value = array.map(\.value) }
        else if let object = try? container.decode([String: AnyCodable].self) {
            value = object.mapValues(\.value)
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Неподдерживаемое значение GeoJSON")
        }
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch value {
        case is NSNull: try container.encodeNil()
        case let number as Bool: try container.encode(number)
        case let number as Int: try container.encode(number)
        case let number as Double: try container.encode(number)
        case let text as String: try container.encode(text)
        case let array as [Any]: try container.encode(array.map { AnyCodable($0) })
        case let object as [String: Any]: try container.encode(object.mapValues { AnyCodable($0) })
        default:
            throw EncodingError.invalidValue(value, .init(codingPath: encoder.codingPath, debugDescription: "Неподдерживаемое значение GeoJSON"))
        }
    }
    public static func == (lhs: AnyCodable, rhs: AnyCodable) -> Bool {
        switch (lhs.value, rhs.value) {
        case (is NSNull, is NSNull): return true
        case let (a as Int, b as Int): return a == b
        case let (a as Double, b as Double): return a == b
        case let (a as String, b as String): return a == b
        case let (a as Bool, b as Bool): return a == b
        default: return false
        }
    }
}

extension PlatformClient {
    /// POST is explicitly read-only: the map is derived from supplied coordinates.
    public func measurementMap(sessionID: String, mapJSON: String) async throws -> PlatformMeasurementMap {
        guard !mapJSON.isEmpty, mapJSON.utf8.count <= 131_072 else {
            throw PlatformError.invalidCommand("Карта должна занимать не более 131 072 байт UTF-8")
        }
        let body = PlatformMeasurementMapRequest(mapJson: mapJSON)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 262_144 else {
            throw PlatformError.invalidCommand("Запрос превышает 262 144 байт")
        }
        try Task.checkCancellation()
        let result: PlatformMeasurementMap = try await post(
            "/sessions/\(Self.segment(sessionID))/measurement-map", body: body,
            expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformPhonogram.sameID(result.sessionId, sessionID) else {
            throw PlatformError.invalidResponse("Сервер вернул другую карту измерений")
        }
        return result
    }
}
