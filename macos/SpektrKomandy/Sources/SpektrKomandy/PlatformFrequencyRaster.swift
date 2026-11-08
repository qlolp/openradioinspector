import Foundation

private struct RasterKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func rasterExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: RasterKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected frequency raster fields"))
    }
}

/// Portable artifact only: a channel grid per frequency range. No calibration or normative claim.
public struct PlatformFrequencyRaster: Codable, Equatable {
    public static let maximumBytes = 65_536
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public static let maximumEntries = 64
    public static let maximumCandidates = 256
    public struct Entry: Codable, Equatable {
        public let name: String
        public let startHz: Int64
        public let stopHz: Int64
        public let gridStepHz: Int64
        private enum CodingKeys: String, CodingKey { case name, startHz, stopHz, gridStepHz }
        public init(name: String, startHz: Int64, stopHz: Int64, gridStepHz: Int64) {
            self.name = name; self.startHz = startHz; self.stopHz = stopHz; self.gridStepHz = gridStepHz
        }
        public init(from decoder: Decoder) throws {
            try rasterExactKeys(decoder, ["name", "startHz", "stopHz", "gridStepHz"])
            let values = try decoder.container(keyedBy: CodingKeys.self)
            name = try values.decode(String.self, forKey: .name)
            startHz = try values.decode(Int64.self, forKey: .startHz)
            stopHz = try values.decode(Int64.self, forKey: .stopHz)
            gridStepHz = try values.decode(Int64.self, forKey: .gridStepHz)
            guard PlatformFrequencyRaster.validEntryName(name),
                  (0..<stopHz).contains(startHz), stopHz <= PlatformFrequencyRaster.maximumFrequencyHz,
                  (1...(stopHz - startHz)).contains(gridStepHz) else {
                throw DecodingError.dataCorruptedError(forKey: .name, in: values, debugDescription: "Invalid frequency raster entry")
            }
        }
    }
    public let format: String
    public let formatVersion: Int
    public let name: String
    public let frequencyUnit: String
    public let entries: [Entry]
    private enum CodingKeys: String, CodingKey {
        case format, formatVersion, name, frequencyUnit, entries
    }
    public init(name: String, entries: [Entry]) {
        self.format = "openradioinspector-frequency-raster"; self.formatVersion = 1
        self.name = name; self.frequencyUnit = "Hz"; self.entries = entries
    }
    public init(from decoder: Decoder) throws {
        try rasterExactKeys(decoder, ["format", "formatVersion", "name", "frequencyUnit", "entries"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        format = try values.decode(String.self, forKey: .format)
        formatVersion = try values.decode(Int.self, forKey: .formatVersion)
        name = try values.decode(String.self, forKey: .name)
        frequencyUnit = try values.decode(String.self, forKey: .frequencyUnit)
        entries = try values.decode([Entry].self, forKey: .entries)
        guard format == "openradioinspector-frequency-raster", formatVersion == 1,
              frequencyUnit == "Hz", Self.validEntryName(name),
              (1...Self.maximumEntries).contains(entries.count),
              zip(entries, entries.dropFirst()).allSatisfy({ $0.stopHz < $1.startHz }) else {
            throw DecodingError.dataCorruptedError(forKey: .entries, in: values, debugDescription: "Invalid canonical frequency raster")
        }
    }
    public static func validEntryName(_ value: String) -> Bool {
        guard let normalized = try? normalizedSweepReferenceName(value),
              normalized.unicodeScalars.elementsEqual(value.unicodeScalars) else { return false }
        return true
    }
    public func canonicalData() throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(self)
        guard data.count <= Self.maximumBytes else { throw PlatformError.invalidResponse("Таблица превышает 65 536 байт") }
        return data
    }
    /// Compare only after the core has accepted the original raw text. Never rewrites a request.
    static func submitted(_ text: String) throws -> Self {
        let bytes = Data(text.utf8)
        var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        guard bytes.count <= maximumBytes,
              var object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              let name = object["name"] as? String else { throw PlatformError.invalidResponse("Некорректная исходная таблица") }
        guard let entries = object["entries"] as? [[String: Any]] else {
            throw PlatformError.invalidResponse("Некорректная исходная таблица")
        }
        var previousStop: Int64 = -1
        var names = Set<String>()
        for entry in entries {
            guard let entryName = entry["name"] as? String, validEntryName(entryName), names.insert(entryName).inserted,
                  let start = entry["start_hz"] as? Int64, let stop = entry["stop_hz"] as? Int64,
                  let step = entry["grid_step_hz"] as? Int64, start > previousStop,
                  (0..<stop).contains(start), stop <= maximumFrequencyHz, (1...(stop - start)).contains(step) else {
                throw PlatformError.invalidResponse("Некорректная исходная таблица")
            }
            previousStop = stop
        }
        object["name"] = try normalizedSweepReferenceName(name)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Self.self, from: JSONSerialization.data(withJSONObject: object))
    }
}

/// One rounded candidate frequency; the archived source frequency is echoed, never changed.
public struct PlatformFrequencyRoundingItem: Decodable, Equatable {
    public let candidateId: String
    public let frequencyHz: Int64
    public let matched: Bool
    public let roundedFrequencyHz: Int64?
    public let deltaHz: Int64?
    public let gridStepHz: Int64?
    public let entryName: String?
}

/// Read-only derived rounding; candidates in the archive are never rewritten.
public struct PlatformFrequencyRounding: Decodable, Equatable {
    public let sessionId: String
    public let order: String
    public let table: PlatformFrequencyRaster
    public let count: Int
    public let matchedCount: Int
    public let unmatchedCount: Int
    public let items: [PlatformFrequencyRoundingItem]
    private enum CodingKeys: String, CodingKey {
        case sessionId, order, table, count, matchedCount, unmatchedCount, items
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        order = try v.decode(String.self, forKey: .order)
        table = try v.decode(PlatformFrequencyRaster.self, forKey: .table)
        count = try v.decode(Int.self, forKey: .count)
        matchedCount = try v.decode(Int.self, forKey: .matchedCount)
        unmatchedCount = try v.decode(Int.self, forKey: .unmatchedCount)
        items = try v.decode([PlatformFrequencyRoundingItem].self, forKey: .items)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .items, in: v, debugDescription: "Несогласованный результат округления частот")
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              order == "request", count == items.count,
              matchedCount + unmatchedCount == count,
              (0...PlatformFrequencyRaster.maximumCandidates).contains(count) else { throw invalid() }
        var matched = 0
        for item in items {
            guard !item.candidateId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  (0...PlatformFrequencyRaster.maximumFrequencyHz).contains(item.frequencyHz) else { throw invalid() }
            if item.matched {
                matched += 1
                guard let rounded = item.roundedFrequencyHz, let delta = item.deltaHz,
                      let step = item.gridStepHz, let entryName = item.entryName,
                      let entry = table.entries.first(where: { $0.name == entryName }),
                      step == entry.gridStepHz,
                      (entry.startHz...entry.stopHz).contains(rounded),
                      (rounded - entry.startHz) % entry.gridStepHz == 0,
                      rounded - item.frequencyHz == delta else { throw invalid() }
            } else {
                guard item.roundedFrequencyHz == nil, item.deltaHz == nil,
                      item.gridStepHz == nil, item.entryName == nil else { throw invalid() }
            }
        }
        guard matched == matchedCount else { throw invalid() }
    }
    /// Same-value identity as the core contract: candidate IDs compared as exact UTF-8.
    public static func sameID(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return a == b }
        return Data(a.utf8) == Data(b.utf8)
    }
    public func validate(table rawTable: String, candidateIDs: [String]) throws {
        guard items.count == candidateIDs.count,
              zip(items, candidateIDs).allSatisfy({ Self.sameID($0.candidateId, $1) }),
              let accepted = try? PlatformFrequencyRaster.submitted(rawTable), accepted == table else {
            throw PlatformError.invalidResponse("Ответ не соответствует отправленной таблице или выбранным кандидатам")
        }
    }
}

extension PlatformClient {
    /// POST is explicitly read-only: no candidate, event or measurement is written.
    public func frequencyRounding(sessionID: String, candidateIDs: [String], tableJSON: String) async throws -> PlatformFrequencyRounding {
        struct Body: Encodable { let tableJson: String; let candidateIds: [String] }
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID сеанса")
        }
        guard (1...PlatformFrequencyRaster.maximumCandidates).contains(candidateIDs.count),
              candidateIDs.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              Set(candidateIDs.map { Data($0.utf8) }).count == candidateIDs.count else {
            throw PlatformError.invalidCommand("Нужен от 1 до 256 уникальных ID кандидатов")
        }
        _ = try PlatformFrequencyRaster.submitted(tableJSON)
        let body = Body(tableJson: tableJSON, candidateIds: candidateIDs)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 524_288 else { throw PlatformError.invalidCommand("Запрос превышает 524 288 байт") }
        try Task.checkCancellation()
        let result: PlatformFrequencyRounding = try await post(
            "/sessions/\(Self.segment(sessionID))/frequency-rounding", body: body,
            expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformFrequencyRounding.sameID(result.sessionId, sessionID) else {
            throw PlatformError.invalidResponse("Сервер вернул другой сеанс")
        }
        try result.validate(table: tableJSON, candidateIDs: candidateIDs)
        return result
    }
}
