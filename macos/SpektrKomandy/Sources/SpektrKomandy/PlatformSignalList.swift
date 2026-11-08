import Foundation

private struct SigListKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func sigListExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: SigListKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected signal-list fields"))
    }
}

/// One exported signal list: exactly the importable fields, nothing else.
public struct PlatformSignalList: Codable, Equatable {
    public static let maximumEntries = 256
    public let format: String
    public let formatVersion: Int
    public let name: String
    public let frequencyUnit: String
    public let entries: [PlatformSignalListEntry]
    private enum CodingKeys: String, CodingKey {
        case format, formatVersion, name, frequencyUnit, entries
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        format = try v.decode(String.self, forKey: .format)
        formatVersion = try v.decode(Int.self, forKey: .formatVersion)
        name = try v.decode(String.self, forKey: .name)
        frequencyUnit = try v.decode(String.self, forKey: .frequencyUnit)
        entries = try v.decode([PlatformSignalListEntry].self, forKey: .entries)
        guard format == "openradioinspector-signal-list", formatVersion == 1,
              frequencyUnit == "Hz",
              (1...Self.maximumEntries).contains(entries.count) else {
            throw DecodingError.dataCorruptedError(forKey: .entries, in: v,
                                                   debugDescription: "Некорректный список сигналов")
        }
    }
}

/// One observed signal: frequency, bandwidth, level and the interval it was seen in.
public struct PlatformSignalListEntry: Codable, Equatable {
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public let frequencyHz: Int64
    public let bandwidthHz: Int64?
    public let interval: PlatformInterval
    public let levelUnit: String
    public let peakLevel: Double?
    private enum CodingKeys: String, CodingKey {
        case frequencyHz, bandwidthHz, interval, levelUnit, peakLevel
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        frequencyHz = try v.decode(Int64.self, forKey: .frequencyHz)
        bandwidthHz = try v.decodeIfPresent(Int64.self, forKey: .bandwidthHz)
        interval = try v.decode(PlatformInterval.self, forKey: .interval)
        levelUnit = try v.decode(String.self, forKey: .levelUnit)
        peakLevel = try v.decodeIfPresent(Double.self, forKey: .peakLevel)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .frequencyHz, in: v, debugDescription: "Некорректная запись списка сигналов")
        }
        guard (0...Self.maximumFrequencyHz).contains(frequencyHz),
              bandwidthHz == nil || ((0...Self.maximumFrequencyHz).contains(bandwidthHz!) ),
              levelUnit == "dBm" || levelUnit == "s_meter_count",
              peakLevel == nil || peakLevel!.isFinite,
              !interval.startedAt.isEmpty, !interval.endedAt.isEmpty else { throw invalid() }
    }
}

/// Export result; the list itself carries only importable fields.
public struct PlatformSignalListExport: Decodable, Equatable {
    public let sessionId: String
    public let exported: Int
    public let list: PlatformSignalList
}

/// Import result; imported candidates always start as ``new``.
public struct PlatformSignalListImport: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String
    public let imported: Int
    public let candidateIds: [String]
    public let name: String
    private enum CodingKeys: String, CodingKey {
        case sessionId, streamId, imported, candidateIds, name
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        imported = try v.decode(Int.self, forKey: .imported)
        candidateIds = try v.decode([String].self, forKey: .candidateIds)
        name = try v.decode(String.self, forKey: .name)
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              imported == candidateIds.count else {
            throw DecodingError.dataCorruptedError(forKey: .imported, in: v,
                                                   debugDescription: "Некорректный ответ импорта")
        }
    }
}

extension PlatformClient {
    public func signalListExport(sessionID: String, name: String) async throws -> PlatformSignalListExport {
        return try await get("/sessions/\(Self.segment(sessionID))/signal-list/export",
                             query: [.init(name: "name", value: name)], expectedStatuses: [200])
    }
    /// Import creates new candidates through the normal lifecycle; units are never converted.
    public func signalListImport(sessionID: String, streamID: String, listJSON: String) async throws -> PlatformSignalListImport {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !listJSON.isEmpty, listJSON.utf8.count <= 262_144 else {
            throw PlatformError.invalidCommand("Некорректный запрос импорта списка сигналов")
        }
        struct Body: Encodable {
            let streamId: String
            let listJson: String
        }
        let body = Body(streamId: streamID, listJson: listJSON)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 524_288 else {
            throw PlatformError.invalidCommand("Запрос превышает 524 288 байт")
        }
        try Task.checkCancellation()
        let result: PlatformSignalListImport = try await post(
            "/sessions/\(Self.segment(sessionID))/signal-list/import", body: body,
            expectedStatuses: [201], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformPhonogram.sameID(result.sessionId, sessionID),
              PlatformPhonogram.sameID(result.streamId, streamID) else {
            throw PlatformError.invalidResponse("Сервер вернул другой импорт списка сигналов")
        }
        return result
    }
}
