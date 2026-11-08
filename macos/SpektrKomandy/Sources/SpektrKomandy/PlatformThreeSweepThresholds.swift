import Foundation

public struct PlatformThreeThresholdLine: Decodable, Equatable, Identifiable {
    public let id: String
    public let levelDbm: Double
    public let excessLevels: [Double]
    public let regions: [PlatformSweepThresholdRegion]
    public let exceedancePoints: Int
    public let regionCount: Int
}

/// Three independent core calculations. The order and IDs are identities, not a level ranking.
public struct PlatformThreeSweepThresholds: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String
    public let sweep: PlatformComparisonSweep
    public let frequenciesHz: [Int64]
    public let method: String
    public let comparison: String
    public let levelUnit: String
    public let deltaUnit: String
    public let points: Int
    public let lines: [PlatformThreeThresholdLine]

    private enum CodingKeys: String, CodingKey {
        case sessionId, streamId, sweep, frequenciesHz, method, comparison, levelUnit, deltaUnit, points, lines
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        streamId = try values.decode(String.self, forKey: .streamId)
        sweep = try values.decode(PlatformComparisonSweep.self, forKey: .sweep)
        frequenciesHz = try values.decode([Int64].self, forKey: .frequenciesHz)
        method = try values.decode(String.self, forKey: .method)
        comparison = try values.decode(String.self, forKey: .comparison)
        levelUnit = try values.decode(String.self, forKey: .levelUnit)
        deltaUnit = try values.decode(String.self, forKey: .deltaUnit)
        points = try values.decode(Int.self, forKey: .points)
        lines = try values.decode([PlatformThreeThresholdLine].self, forKey: .lines)
        func invalid(_ text: String) -> DecodingError {
            .dataCorruptedError(forKey: .lines, in: values, debugDescription: text)
        }
        guard threeScopeID(sessionId), threeScopeID(streamId), threeSweepID(sweep.id),
              sweep.levelUnit == "dBm", sweep.levels.allSatisfy(\.isFinite),
              averageKnown(sweep.settings) != nil, averageInterval(sweep.interval) != nil,
              method == "three_horizontal", comparison == "strictly_greater",
              levelUnit == "dBm", deltaUnit == "dB", (1...16_384).contains(points),
              sweep.levels.count == points, frequenciesHz.count == points,
              frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              lines.map(\.id) == ["line-1", "line-2", "line-3"],
              Self.validLevels(lines.map(\.levelDbm)) else { throw invalid("Некорректный результат трёх порогов") }
        // Consistency checks only: rendered levels, excess and regions always come from the core.
        for line in lines {
            let excess = line.excessLevels
            guard excess.count == points, excess.allSatisfy(\.isFinite),
                  line.regionCount == line.regions.count,
                  line.exceedancePoints == excess.filter({ $0 > 0 }).count,
                  excess.indices.allSatisfy({ excess[$0] == sweep.levels[$0] - line.levelDbm }) else {
                throw invalid("Несогласованные превышения dB или счётчики линии")
            }
            var cursor = 0
            for region in line.regions {
                while cursor < points && excess[cursor] <= 0 { cursor += 1 }
                guard cursor < points, region.startIndex == cursor else { throw invalid("Неверное начало области") }
                var peak = cursor
                while cursor < points && excess[cursor] > 0 {
                    if excess[cursor] > excess[peak] { peak = cursor }
                    cursor += 1
                }
                guard region.endIndex == cursor - 1, region.points == cursor - region.startIndex,
                      region.startFrequencyHz == frequenciesHz[region.startIndex],
                      region.endFrequencyHz == frequenciesHz[cursor - 1],
                      region.peakIndex == peak, region.peakFrequencyHz == frequenciesHz[peak],
                      region.peakExcessDb.isFinite, region.peakExcessDb == excess[peak] else {
                    throw invalid("Неверные границы или первый максимум области")
                }
            }
            guard excess.dropFirst(cursor).allSatisfy({ $0 <= 0 }) else { throw invalid("Пропущена область превышения") }
        }
    }

    public static func validLevels(_ levels: [Double]) -> Bool { levels.count == 3 && levels.allSatisfy(\.isFinite) }

    /// Archive identities are exact UTF-8 strings, not Swift's canonically equivalent Unicode text.
    public static func sameID(_ left: String?, _ right: String?) -> Bool {
        switch (left, right) {
        case (.none, .none): return true
        case (.some(let a), .some(let b)): return a.utf8.elementsEqual(b.utf8)
        default: return false
        }
    }

    public static func validSource(_ source: PlatformSweep) -> Bool {
        threeScopeID(source.sessionId) && threeScopeID(source.streamId) && threeSweepID(source.id)
            && source.levelUnit == "dBm" && (1...16_384).contains(source.levels.count)
            && source.levels.count == source.frequenciesHz.count && source.levels.allSatisfy(\.isFinite)
            && source.frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) })
            && zip(source.frequenciesHz, source.frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 })
            && averageInterval(source.interval) != nil && source.settings.flatMap(averageKnown) != nil
    }

    /// Exact frozen provenance includes all settings (including sequence and JSON value types).
    public func validate(source: PlatformSweep, levelsDbm: [Double]) throws {
        guard Self.validSource(source), Self.validLevels(levelsDbm),
              Self.sameID(sessionId, source.sessionId), Self.sameID(streamId, source.streamId), Self.sameID(sweep.id, source.id),
              sweep.interval == source.interval, source.settings.map({ threeExactSettings(sweep.settings, $0) }) == true,
              sweep.levelUnit == source.levelUnit, sweep.levels == source.levels,
              frequenciesHz == source.frequenciesHz, lines.map(\.levelDbm) == levelsDbm else {
            throw PlatformError.invalidResponse("Ответ не соответствует зафиксированному свипу или трём уровням")
        }
    }
}

private func threeScopeID(_ id: String) -> Bool { !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
private func threeSweepID(_ id: String) -> Bool { threeScopeID(id) && id.unicodeScalars.count <= 128 }

private func threeExactSettings(_ left: [String: PlatformComparisonSetting], _ right: [String: PlatformComparisonSetting]) -> Bool {
    func exact(_ left: PlatformComparisonSetting, _ right: PlatformComparisonSetting) -> Bool {
        switch (left, right) {
        case (.string(let a), .string(let b)): return PlatformThreeSweepThresholds.sameID(a, b)
        case (.number(let a), .number(let b)): return a == b
        case (.boolean(let a), .boolean(let b)): return a == b
        case (.null, .null): return true
        case (.array(let a), .array(let b)): return a.count == b.count && zip(a, b).allSatisfy(exact)
        case (.object(let a), .object(let b)): return threeExactSettings(a, b)
        default: return false
        }
    }
    let a = left.sorted { $0.key.utf8.lexicographicallyPrecedes($1.key.utf8) }
    let b = right.sorted { $0.key.utf8.lexicographicallyPrecedes($1.key.utf8) }
    return a.count == b.count && zip(a, b).allSatisfy {
        PlatformThreeSweepThresholds.sameID($0.key, $1.key) && exact($0.value, $1.value)
    }
}

extension PlatformClient {
    /// Explicit read-only POST; no events, commands, archive writes or polling-triggered analysis.
    public func threeSweepThresholds(sessionID: String, sweepID: String, levelsDbm: [Double]) async throws -> PlatformThreeSweepThresholds {
        struct Body: Encodable { let sweepId: String; let levelsDbm: [Double] }
        guard threeScopeID(sessionID), threeSweepID(sweepID), PlatformThreeSweepThresholds.validLevels(levelsDbm) else {
            throw PlatformError.invalidCommand("Нужны ID сеанса, ID свипа до 128 символов и ровно три конечных уровня dBm")
        }
        let body = Body(sweepId: sweepID, levelsDbm: levelsDbm)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        let result: PlatformThreeSweepThresholds = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-three-thresholds", body: body, expectedStatuses: [200], validateResponse: { bytes in
                // Swift Dictionary cannot retain canonically equivalent but byte-distinct keys.
                // Fail closed before DTO decoding rather than silently dropping frozen provenance.
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformThreeSweepThresholds.sameID(result.sessionId, sessionID),
              PlatformThreeSweepThresholds.sameID(result.sweep.id, sweepID), result.lines.map(\.levelDbm) == levelsDbm else {
            throw PlatformError.invalidResponse("Сервер вернул другой свип или другие уровни трёх порогов")
        }
        return result
    }
}
