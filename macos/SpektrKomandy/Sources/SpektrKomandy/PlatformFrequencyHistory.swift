import Foundation
import ORIContracts

public struct PlatformFrequencyHistorySample: Decodable, Sendable {
    public let requestedHz: Int64
    public let frequencyHz: Int64?
    public let level: Double?
}
public struct PlatformFrequencyHistoryRow: Decodable, Identifiable, Sendable {
    public var id: PlatformExactID { PlatformExactID(sweepId) }
    public let sweepId: String
    public let interval: PlatformInterval
    public let levelUnit: String
    public let segmentId: String
    public let samples: [PlatformFrequencyHistorySample]
}
public struct PlatformFrequencyHistoryPage: Decodable, Sendable {
    public let sessionId: String
    public let streamId: String
    public let frequenciesHz: [Int64]
    public let start: String
    public let end: String
    public let items: [PlatformFrequencyHistoryRow]
    public let nextCursor: String?
}
public struct PlatformFrequencyBookmarkEntry: Codable, Equatable, Sendable {
    public var label: String
    public var frequencyHz: Int64
    public init(label: String, frequencyHz: Int64) { self.label = label; self.frequencyHz = frequencyHz }
}
public struct PlatformFrequencyBookmarkSet: Decodable, Identifiable, Sendable {
    public let id: String
    public let sessionId: String
    public let schemaVersion: Int
    public let name: String
    public let revision: Int
    public let createdAt: String
    public let entries: [PlatformFrequencyBookmarkEntry]
}
public struct PlatformFrequencyBookmarkPage: Decodable, Sendable {
    public let sessionId: String
    public let items: [PlatformFrequencyBookmarkSet]
    public let limit: Int
}

/// Portable metadata only. Import rejects all additional fields, including device actions.
public struct PlatformFrequencyBookmarkDocument: Codable, Sendable {
    public let schemaVersion: Int
    public var name: String
    public var entries: [PlatformFrequencyBookmarkEntry]
    public init(name: String, entries: [PlatformFrequencyBookmarkEntry]) {
        schemaVersion = 1; self.name = name; self.entries = entries
    }
    public func validate() throws {
        func validName(_ value: String) -> Bool {
            (1...80).contains(value.unicodeScalars.count) && value == value.trimmingCharacters(in: .whitespacesAndNewlines)
                && !value.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
        }
        guard schemaVersion == 1, validName(name), (1...4).contains(entries.count),
              Set(entries.map(\.frequencyHz)).count == entries.count,
              entries.allSatisfy({ validName($0.label) && (0...9_007_199_254_740_991).contains($0.frequencyHz) }) else {
            throw PlatformError.invalidCommand("Нужны имя и 1–4 разные частоты в целых Гц; подписи — до 80 символов")
        }
    }
    public static func importData(_ bytes: Data) throws -> Self {
        guard bytes.count <= 8192 else { throw PlatformError.responseTooLarge }
        let object = try StrictJSON.parse(bytes).requireObject()
        guard Set(object.keys) == ["schema_version","name","entries"],
              let entries = try object["entries"]?.requireArray(), entries.count <= 4 else {
            throw PlatformError.invalidCommand("Неизвестные поля файла частот")
        }
        for entry in entries {
            let value = try entry.requireObject()
            guard Set(value.keys) == ["label","frequency_hz"] else {
                throw PlatformError.invalidCommand("Метка должна содержать только подпись и частоту")
            }
            _ = try value["frequency_hz"]?.requireUInt64()
        }
        guard try object["schema_version"]?.requireUInt64() == 1 else { throw PlatformError.invalidCommand("Неизвестная версия файла частот") }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let document = try decoder.decode(Self.self, from: bytes); try document.validate(); return document
    }
    public func exportData() throws -> Data {
        try validate()
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted,.sortedKeys]; return try encoder.encode(self)
    }
}

extension PlatformClient {
    public func workbenchFeatures() async throws -> Set<String> {
        let value: MonitoringValue = try await get("/capabilities", validateResponse: { _ = try StrictJSON.parse($0) })
        guard case .object(let features) = value["features"] else { return [] }
        return Set(features.compactMap { $0.value.bool == true ? $0.key : nil })
    }
    public func recoverStream(sessionID: String, streamID: String) async throws -> PlatformWorker {
        struct Empty: Encodable {}
        struct Result: Decodable {
            let sessionId: String
            let worker: PlatformWorker
            init(from decoder: Decoder) throws {
                enum Keys: String, CodingKey { case sessionId }
                sessionId = try decoder.container(keyedBy: Keys.self).decode(String.self, forKey: .sessionId)
                worker = try PlatformWorker(from: decoder)
            }
        }
        let result: Result = try await post("/sessions/\(Self.segment(sessionID))/streams/\(Self.segment(streamID))/recover", body: Empty(), timeoutInterval: 30)
        guard PlatformExactID.same(result.sessionId,sessionID), PlatformExactID.same(result.worker.streamId,streamID) else {
            throw PlatformError.invalidResponse("Восстановление вернуло другой поток")
        }
        return result.worker
    }
    public func frequencyHistory(sessionID: String, streamID: String, frequencies: [Int64],
                                 start: String, end: String, after: String? = nil) async throws -> PlatformFrequencyHistoryPage {
        let document = PlatformFrequencyBookmarkDocument(name:"History", entries: frequencies.map { .init(label:"Frequency",frequencyHz:$0) })
        try document.validate()
        guard !sessionID.isEmpty, !streamID.isEmpty, streamID.count <= 128,
              let from = Self.historyDate(start), let to = Self.historyDate(end), from < to,
              after == nil || (1...16384).contains(after!.utf8.count) else {
            throw PlatformError.invalidCommand("Проверьте поток, интервал UTC и курсор")
        }
        struct Query: Encodable { let streamId: String; let frequenciesHz: [Int64]; let start: String; let end: String; let after: String?; let limit = 100 }
        let page: PlatformFrequencyHistoryPage = try await post("/sessions/\(Self.segment(sessionID))/frequency-history",
            body: Query(streamId:streamID,frequenciesHz:frequencies,start:start,end:end,after:after), validateResponse: {
                _ = try ORIContracts.FrequencyHistoryPage(json: StrictJSON.parse($0).requireObject()["data"] ?? .null)
            })
        guard PlatformExactID.same(page.sessionId,sessionID), PlatformExactID.same(page.streamId,streamID),
              page.frequenciesHz == frequencies, Self.historyDate(page.start) == from, Self.historyDate(page.end) == to,
              page.items.count <= 100, Set(page.items.map { PlatformExactID($0.sweepId) }).count == page.items.count,
              page.nextCursor == nil || ((1...16384).contains(page.nextCursor!.utf8.count) && !page.items.isEmpty && page.nextCursor != after) else {
            throw PlatformError.invalidResponse("История вернула другую выборку или некорректную страницу")
        }
        var previous: (Date, [UInt8])?
        for row in page.items {
            guard !row.sweepId.isEmpty, !row.levelUnit.isEmpty, row.levelUnit.count <= 80,
                  row.segmentId.count == 64, row.segmentId.allSatisfy({ "0123456789abcdef".contains($0) }),
                  let began = Self.historyDate(row.interval.startedAt), let ended = Self.historyDate(row.interval.endedAt),
                  began <= ended, began >= from, began < to, row.samples.map(\.requestedHz) == frequencies,
                  row.samples.allSatisfy({ ($0.frequencyHz == nil && $0.level == nil) ||
                    ($0.frequencyHz.map { (0...9_007_199_254_740_991).contains($0) } == true && $0.level?.isFinite == true) }) else {
                throw PlatformError.invalidResponse("Некорректный исходник истории уровней")
            }
            let id = Array(row.sweepId.utf8)
            if let previous, began > previous.0 || (began == previous.0 && !id.lexicographicallyPrecedes(previous.1)) {
                throw PlatformError.invalidResponse("Нарушен порядок исходников истории")
            }
            previous = (began,id)
        }
        return page
    }
    public func frequencyBookmarks(sessionID: String) async throws -> PlatformFrequencyBookmarkPage {
        let page: PlatformFrequencyBookmarkPage = try await get("/sessions/\(Self.segment(sessionID))/frequency-bookmarks", validateResponse: {
            _ = try ORIContracts.FrequencyBookmarkPage(json: StrictJSON.parse($0).requireObject()["data"] ?? .null)
        })
        guard PlatformExactID.same(page.sessionId,sessionID), page.limit == 256, page.items.count <= 256,
              Set(page.items.map { PlatformExactID($0.id) }).count == page.items.count else {
            throw PlatformError.invalidResponse("Некорректный каталог частот")
        }
        for item in page.items { try Self.validateBookmark(item, sessionID:sessionID) }
        return page
    }
    public func saveFrequencyBookmarks(sessionID: String, document: PlatformFrequencyBookmarkDocument) async throws -> PlatformFrequencyBookmarkSet {
        try document.validate()
        struct Body: Encodable { let name: String; let entries: [PlatformFrequencyBookmarkEntry] }
        let item: PlatformFrequencyBookmarkSet = try await post("/sessions/\(Self.segment(sessionID))/frequency-bookmarks",
            body:Body(name:document.name,entries:document.entries),expectedStatuses:[201], validateResponse: {
                _ = try ORIContracts.FrequencyBookmarkSet(json: StrictJSON.parse($0).requireObject()["data"] ?? .null)
            })
        try Self.validateBookmark(item,sessionID:sessionID)
        guard item.name == document.name, item.entries == document.entries else { throw PlatformError.invalidResponse("Сохранён другой набор частот") }
        return item
    }
    private static func validateBookmark(_ item: PlatformFrequencyBookmarkSet, sessionID: String) throws {
        guard item.schemaVersion == 1, item.revision >= 1, !item.id.isEmpty,
              PlatformExactID.same(item.sessionId,sessionID), historyDate(item.createdAt) != nil else {
            throw PlatformError.invalidResponse("Некорректная версия набора частот")
        }
        try PlatformFrequencyBookmarkDocument(name:item.name,entries:item.entries).validate()
    }
    private static func historyDate(_ text: String) -> Date? {
        guard text.hasSuffix("Z") || text.hasSuffix("+00:00") else { return nil }
        let parser = ISO8601DateFormatter(); parser.formatOptions = [.withInternetDateTime,.withFractionalSeconds]
        if let result = parser.date(from:text) { return result }
        parser.formatOptions = [.withInternetDateTime]; return parser.date(from:text)
    }
}
