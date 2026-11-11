import Foundation

/// Names are NFC, case-sensitive, and bounded in Unicode code points, not graphemes.
public func normalizedSweepReferenceName(_ text: String) throws -> String {
    let normalized = text.precomposedStringWithCanonicalMapping
    guard !normalized.unicodeScalars.contains(where: {
        switch $0.properties.generalCategory {
        case .control, .format, .surrogate, .privateUse, .unassigned: return true
        default: return false
        }
    }) else {
        throw PlatformError.invalidCommand("Имя: 1–80 символов Unicode, без управляющих символов")
    }
    // Python str.strip's non-control whitespace; controls were rejected above.
    let whitespace = CharacterSet(charactersIn: " \u{00A0}\u{1680}\u{2000}\u{2001}\u{2002}\u{2003}\u{2004}\u{2005}\u{2006}\u{2007}\u{2008}\u{2009}\u{200A}\u{2028}\u{2029}\u{202F}\u{205F}\u{3000}")
    let name = normalized.trimmingCharacters(in: whitespace)
    guard (1...80).contains(name.unicodeScalars.count) else {
        throw PlatformError.invalidCommand("Имя: 1–80 символов Unicode")
    }
    return name
}

public struct PlatformSweepReference: Decodable, Equatable, Identifiable, Sendable {
    public let id: String
    public let sessionId: String
    public let streamId: String
    public let sweepId: String
    public let name: String
    public let createdAt: String
    public let interval: PlatformInterval
    public let levelUnit: String
    public let points: Int

    private enum CodingKeys: String, CodingKey, Sendable {
        case id, sessionId, streamId, sweepId, name, createdAt, interval, levelUnit, points
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        streamId = try values.decode(String.self, forKey: .streamId)
        sweepId = try values.decode(String.self, forKey: .sweepId)
        name = try values.decode(String.self, forKey: .name)
        createdAt = try values.decode(String.self, forKey: .createdAt)
        interval = try values.decode(PlatformInterval.self, forKey: .interval)
        levelUnit = try values.decode(String.self, forKey: .levelUnit)
        points = try values.decode(Int.self, forKey: .points)
        guard [id, sessionId, streamId, sweepId].allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              (try? normalizedSweepReferenceName(name))?.unicodeScalars.elementsEqual(name.unicodeScalars) == true,
              referenceUTCDate(createdAt) != nil,
              let start = referenceUTCDate(interval.startedAt), let end = referenceUTCDate(interval.endedAt), start <= end,
              levelUnit == "dBm", (1...16_384).contains(points) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: values, debugDescription: "Некорректный сохранённый опорный свип")
        }
    }
}

public struct PlatformSweepReferenceCatalog: Decodable, Equatable, Sendable {
    public let sessionId: String
    public let streamId: String?
    public let storageMode: String
    public let persistent: Bool
    public let limit: Int
    public let items: [PlatformSweepReference]

    private enum CodingKeys: String, CodingKey, Sendable { case sessionId, streamId, storageMode, persistent, limit, items }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        // The contract requires an explicit null for an unfiltered catalog.
        guard values.contains(.streamId) else {
            throw DecodingError.keyNotFound(CodingKeys.streamId, .init(codingPath: decoder.codingPath, debugDescription: "Missing stream_id"))
        }
        streamId = try values.decodeIfPresent(String.self, forKey: .streamId)
        storageMode = try values.decode(String.self, forKey: .storageMode)
        persistent = try values.decode(Bool.self, forKey: .persistent)
        limit = try values.decode(Int.self, forKey: .limit)
        items = try values.decode([PlatformSweepReference].self, forKey: .items)
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              streamId == nil || !streamId!.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              ["sqlite", "memory"].contains(storageMode), (storageMode == "sqlite" || !persistent),
              limit == 100, items.count <= limit,
              Set(items.map(\.exactID)).count == items.count, Set(items.map(\.name)).count == items.count,
              items.allSatisfy({ PlatformExactID.same($0.sessionId, sessionId) && (streamId == nil || PlatformExactID.same($0.streamId, streamId)) }) else {
            throw DecodingError.dataCorruptedError(forKey: .items, in: values, debugDescription: "Некорректный каталог опорных свипов")
        }
    }
}

private func referenceUTCDate(_ text: String) -> Date? {
    guard text.hasSuffix("Z") || text.hasSuffix("+00:00") else { return nil }
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = format.date(from: text) { return date }
    format.formatOptions = [.withInternetDateTime]
    return format.date(from: text)
}

extension PlatformClient {
    public func sweepReferences(sessionID: String, streamID: String? = nil) async throws -> PlatformSweepReferenceCatalog {
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              streamID == nil || !streamID!.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужны ID сеанса и выбранного потока")
        }
        try Task.checkCancellation()
        let catalog: PlatformSweepReferenceCatalog = try await get(
            "/sessions/\(Self.segment(sessionID))/sweep-references",
            query: streamID.map { [URLQueryItem(name: "stream_id", value: $0)] } ?? [], expectedStatuses: [200]
        )
        try Task.checkCancellation()
        guard PlatformExactID.same(catalog.sessionId, sessionID), PlatformExactID.same(catalog.streamId, streamID) else {
            throw PlatformError.invalidResponse("Сервер вернул каталог другого сеанса или потока")
        }
        return catalog
    }

    /// Never automatically retries a write. A canceled/failed transport can have committed on the server.
    public func saveSweepReference(sessionID: String, sweepID: String, name: String) async throws -> PlatformSweepReference {
        struct Body: Encodable, Sendable { let name: String; let sweepId: String }
        guard [sessionID, sweepID].allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }) else {
            throw PlatformError.invalidCommand("Нужны ID сеанса и архивного свипа")
        }
        let normalized = try normalizedSweepReferenceName(name)
        try Task.checkCancellation()
        let record: PlatformSweepReference = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-references", body: Body(name: normalized, sweepId: sweepID), expectedStatuses: [200, 201]
        )
        try Task.checkCancellation()
        guard PlatformExactID.same(record.sessionId, sessionID), PlatformExactID.same(record.sweepId, sweepID), record.name == normalized else {
            throw PlatformError.invalidResponse("Сервер вернул другой сохранённый опорный свип")
        }
        return record
    }
}
