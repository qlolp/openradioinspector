import Foundation

public struct PlatformContextCounts: Decodable, Equatable {
    public let before: Int
    public let after: Int
    public static func valid(_ value: Int) -> Bool { (0...16).contains(value) }
}

/// Every item preserves its full original spectrum and recorded provenance.
public struct PlatformContextItem: Decodable, Equatable {
    public let offset: Int
    public let id: String
    public let interval: PlatformInterval
    public let levels: [Double]
    public let levelUnit: String
    public let settings: [String: PlatformComparisonSetting]
}

/// Read-only nearest archived neighbours, never a rolling capture or candidate association.
public struct PlatformSweepContext: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String
    public let anchorId: String
    public let order: String
    public let requested: PlatformContextCounts
    public let available: PlatformContextCounts
    public let missing: PlatformContextCounts
    public let complete: Bool
    public let frequenciesHz: [Int64]
    public let levelUnit: String
    public let points: Int
    public let items: [PlatformContextItem]

    private enum CodingKeys: String, CodingKey {
        case sessionId, streamId, anchorId, order, requested, available, missing, complete, frequenciesHz, levelUnit, points, items
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        anchorId = try v.decode(String.self, forKey: .anchorId)
        order = try v.decode(String.self, forKey: .order)
        requested = try v.decode(PlatformContextCounts.self, forKey: .requested)
        available = try v.decode(PlatformContextCounts.self, forKey: .available)
        missing = try v.decode(PlatformContextCounts.self, forKey: .missing)
        complete = try v.decode(Bool.self, forKey: .complete)
        frequenciesHz = try v.decode([Int64].self, forKey: .frequenciesHz)
        levelUnit = try v.decode(String.self, forKey: .levelUnit)
        points = try v.decode(Int.self, forKey: .points)
        items = try v.decode([PlatformContextItem].self, forKey: .items)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .items, in: v, debugDescription: "Несогласованное окно архивных свипов")
        }
        guard contextScopeID(sessionId), contextScopeID(streamId), contextSweepID(anchorId),
              order == "started_at_then_id", levelUnit == "dBm",
              [requested.before, requested.after, available.before, available.after, missing.before, missing.after].allSatisfy(PlatformContextCounts.valid),
              available.before <= requested.before, available.after <= requested.after,
              missing.before == requested.before - available.before, missing.after == requested.after - available.after,
              complete == (missing.before == 0 && missing.after == 0),
              (1...16_384).contains(points), frequenciesHz.count == points,
              frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              items.count == available.before + 1 + available.after,
              items.count * points <= 131_072,
              items.map(\.offset) == Array(-available.before...available.after),
              Set(items.map { Data($0.id.utf8) }).count == items.count,
              Self.sameID(items[available.before].id, anchorId),
              let known = averageKnown(items[available.before].settings) else { throw invalid() }
        for item in items {
            guard contextSweepID(item.id), item.levelUnit == "dBm", item.levels.count == points,
                  item.levels.allSatisfy(\.isFinite), averageInterval(item.interval) != nil,
                  let settings = averageKnown(item.settings), exactRecordedSettings(settings, known) else { throw invalid() }
        }
        for (a, b) in zip(items, items.dropFirst()) {
            let first = averageInterval(a.interval)!.0, second = averageInterval(b.interval)!.0
            guard first < second || (first == second && a.id.utf8.lexicographicallyPrecedes(b.id.utf8)) else { throw invalid() }
        }
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool { PlatformThreeSweepThresholds.sameID(a, b) }
    public static func validSource(_ source: PlatformSweep) -> Bool { PlatformThreeSweepThresholds.validSource(source) }
    public static func sourcePrecedes(_ a: PlatformSweep, _ b: PlatformSweep) -> Bool {
        guard let first = averageInterval(a.interval)?.0, let second = averageInterval(b.interval)?.0 else { return false }
        return first < second || (first == second && a.id.utf8.lexicographicallyPrecedes(b.id.utf8))
    }
    /// Full frozen anchor validation includes sequence, exact Unicode strings and typed settings.
    public func validate(anchor: PlatformSweep, before: Int, after: Int) throws {
        let item = items[available.before]
        guard Self.validSource(anchor), Self.sameID(sessionId, anchor.sessionId), Self.sameID(streamId, anchor.streamId),
              Self.sameID(anchorId, anchor.id), requested.before == before, requested.after == after,
              frequenciesHz == anchor.frequenciesHz, item.levels == anchor.levels, item.levelUnit == anchor.levelUnit,
              item.interval == anchor.interval, anchor.settings.map({ exactRecordedSettings(item.settings, $0) }) == true else {
            throw PlatformError.invalidResponse("Ответ не соответствует зафиксированному свипу и запрошенному окну N1/N2")
        }
    }
}

private func contextScopeID(_ id: String) -> Bool { !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
private func contextSweepID(_ id: String) -> Bool { contextScopeID(id) && id.unicodeScalars.count <= 128 }

extension PlatformClient {
    public func sweepContext(sessionID: String, sweepID: String, before: Int, after: Int) async throws -> PlatformSweepContext {
        struct Body: Encodable { let sweepId: String; let before: Int; let after: Int }
        guard contextScopeID(sessionID), contextSweepID(sweepID), PlatformContextCounts.valid(before), PlatformContextCounts.valid(after) else {
            throw PlatformError.invalidCommand("Нужны ID архивного свипа до 128 символов и целые N1/N2 от 0 до 16")
        }
        let body = Body(sweepId: sweepID, before: before, after: after)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        let result: PlatformSweepContext = try await post("/sessions/\(Self.segment(sessionID))/sweep-context", body: body, expectedStatuses: [200], validateResponse: { bytes in
            var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        })
        try Task.checkCancellation()
        guard PlatformSweepContext.sameID(result.sessionId, sessionID), PlatformSweepContext.sameID(result.anchorId, sweepID),
              result.requested.before == before, result.requested.after == after else {
            throw PlatformError.invalidResponse("Сервер вернул другое архивное окно")
        }
        return result
    }
}
