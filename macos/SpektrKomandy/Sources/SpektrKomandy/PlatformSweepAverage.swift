import Foundation

/// Source provenance is preserved independently from the core's derived mean-power levels.
public struct PlatformAverageSource: Decodable, Equatable {
    public let id: String
    public let interval: PlatformInterval
    public let settings: [String: PlatformComparisonSetting]
}

public struct PlatformAveragedReference: Decodable, Equatable {
    public let method: String
    public let count: Int
    public let sourceIds: [String]
    public let sources: [PlatformAverageSource]
    public let interval: PlatformInterval
    public let levels: [Double]
    public let levelUnit: String
}

public struct PlatformSweepAverageComparison: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String
    public let frequenciesHz: [Int64]
    public let reference: PlatformAveragedReference
    public let target: PlatformComparisonSweep
    public let deltaLevels: [Double]
    public let deltaUnit: String
    public let maxIncrease: PlatformComparisonExtremum
    public let minChange: PlatformComparisonExtremum
    public let points: Int

    private enum CodingKeys: String, CodingKey {
        case sessionId, streamId, frequenciesHz, reference, target, deltaLevels, deltaUnit, maxIncrease, minChange, points
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        frequenciesHz = try v.decode([Int64].self, forKey: .frequenciesHz)
        reference = try v.decode(PlatformAveragedReference.self, forKey: .reference)
        target = try v.decode(PlatformComparisonSweep.self, forKey: .target)
        deltaLevels = try v.decode([Double].self, forKey: .deltaLevels)
        deltaUnit = try v.decode(String.self, forKey: .deltaUnit)
        maxIncrease = try v.decode(PlatformComparisonExtremum.self, forKey: .maxIncrease)
        minChange = try v.decode(PlatformComparisonExtremum.self, forKey: .minChange)
        points = try v.decode(Int.self, forKey: .points)
        guard averageScopeID(sessionId), averageScopeID(streamId), averageID(target.id), target.isValid, averageInterval(target.interval) != nil,
              reference.method == "mean_power", (2...16).contains(reference.count),
              reference.count == reference.sourceIds.count, reference.count == reference.sources.count,
              Set(reference.sourceIds).count == reference.count, reference.sourceIds.allSatisfy(averageID),
              reference.sourceIds == reference.sources.map(\.id), !reference.sourceIds.contains(target.id),
              reference.levelUnit == "dBm", deltaUnit == "dB", (1...16_384).contains(points),
              frequenciesHz.count == points, reference.levels.count == points, target.levels.count == points,
              deltaLevels.count == points, frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              reference.levels.allSatisfy(\.isFinite), deltaLevels.allSatisfy(\.isFinite),
              reference.sources.allSatisfy({ averageInterval($0.interval) != nil && averageKnown($0.settings) != nil }),
              let known = averageKnown(target.settings),
              reference.sources.allSatisfy({ averageKnown($0.settings) == known }),
              let envelope = averageInterval(reference.interval),
              envelope.0 == reference.sources.compactMap({ averageInterval($0.interval)?.0 }).min(),
              envelope.1 == reference.sources.compactMap({ averageInterval($0.interval)?.1 }).max(),
              let maximum = deltaLevels.max(), let minimum = deltaLevels.min(),
              let maxIndex = deltaLevels.firstIndex(of: maximum), let minIndex = deltaLevels.firstIndex(of: minimum),
              maxIncrease.deltaDb == maximum, minChange.deltaDb == minimum,
              maxIncrease.frequencyHz == frequenciesHz[maxIndex], minChange.frequencyHz == frequenciesHz[minIndex],
              deltaLevels.indices.allSatisfy({ deltaLevels[$0] == target.levels[$0] - reference.levels[$0] }) else {
            throw DecodingError.dataCorruptedError(forKey: .reference, in: v, debugDescription: "Некорректное сравнение с усреднённым эталоном")
        }
    }

    /// Validate exact frozen source/target provenance, including sequence. Never calculate a mean in the client.
    public func validate(sources: [PlatformSweep], target frozenTarget: PlatformSweep) throws {
        guard Self.validSources(sources, target: frozenTarget),
              sessionId == frozenTarget.sessionId, streamId == frozenTarget.streamId,
              reference.sourceIds == sources.map(\.id), frequenciesHz == frozenTarget.frequenciesHz,
              target.id == frozenTarget.id, target.interval == frozenTarget.interval,
              target.settings == frozenTarget.settings, target.levels == frozenTarget.levels,
              target.levelUnit == frozenTarget.levelUnit,
              zip(reference.sources, sources).allSatisfy({
                  $0.id == $1.id && $0.interval == $1.interval && $0.settings == $1.settings
              }), reference.levels.indices.allSatisfy({ index in
                  let levels = sources.map { $0.levels[index] }
                  return reference.levels[index] >= levels.min()! && reference.levels[index] <= levels.max()!
              }) else { throw PlatformError.invalidResponse("Ответ не соответствует зафиксированным свипам или границам исходных уровней") }
    }

    public static func sourceInterval(_ sources: [PlatformSweep]) -> PlatformInterval? {
        guard !sources.isEmpty, sources.allSatisfy({ averageInterval($0.interval) != nil }),
              let first = sources.min(by: { averageInterval($0.interval)!.0 < averageInterval($1.interval)!.0 }),
              let last = sources.max(by: { averageInterval($0.interval)!.1 < averageInterval($1.interval)!.1 }) else { return nil }
        return PlatformInterval(startedAt: first.interval.startedAt, endedAt: last.interval.endedAt)
    }

    public static func validSources(_ sources: [PlatformSweep], target: PlatformSweep) -> Bool {
        let all = sources + [target]
        guard (2...16).contains(sources.count), Set(all.map(\.id)).count == all.count,
              let settings = target.settings.flatMap(averageKnown) else { return false }
        return all.allSatisfy {
            averageID($0.id) && $0.sessionId == target.sessionId && $0.streamId == target.streamId
                && $0.levelUnit == "dBm" && (1...16_384).contains($0.levels.count)
                && $0.levels.count == $0.frequenciesHz.count && $0.levels.allSatisfy(\.isFinite)
                && $0.frequenciesHz == target.frequenciesHz
                && $0.frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) })
                && zip($0.frequenciesHz, $0.frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 })
                && averageInterval($0.interval) != nil && $0.settings.flatMap(averageKnown) == settings
        }
    }
}

public struct PlatformSweepChangeRegion: Decodable, Equatable {
    public let startIndex: Int
    public let endIndex: Int
    public let startFrequencyHz: Int64
    public let endFrequencyHz: Int64
    public let peakIndex: Int
    public let peakFrequencyHz: Int64
    public let peakIncreaseDb: Double
    public let binCount: Int
}

public struct PlatformSweepChangeDetection: Decodable, Equatable {
    public let minimumIncreaseDb: Double
    public let minimumBins: Int
    public let regions: [PlatformSweepChangeRegion]
    public let count: Int
}

public struct PlatformSweepAverageChangeDetection: Decodable, Equatable {
    public let comparison: PlatformSweepAverageComparison
    public let detection: PlatformSweepChangeDetection

    public func validate(
        sources: [PlatformSweep], target: PlatformSweep,
        minimumIncreaseDb: Double, minimumBins: Int,
    ) throws {
        try comparison.validate(sources: sources, target: target)
        guard minimumIncreaseDb.isFinite, (0...120).contains(minimumIncreaseDb),
              (1...16_384).contains(minimumBins),
              detection.minimumIncreaseDb == minimumIncreaseDb,
              detection.minimumBins == minimumBins,
              detection.count == detection.regions.count else {
            throw PlatformError.invalidResponse("Параметры или сводка обнаружения не совпадают с запросом")
        }
        let frequencies = comparison.frequenciesHz, deltas = comparison.deltaLevels
        var expected: [PlatformSweepChangeRegion] = []
        var start: Int?
        func append(_ end: Int) {
            guard let start, end - start + 1 >= minimumBins else { return }
            var peak = start
            if start < end {
                for index in (start + 1)...end where deltas[index] > deltas[peak] { peak = index }
            }
            expected.append(PlatformSweepChangeRegion(
                startIndex: start, endIndex: end,
                startFrequencyHz: frequencies[start], endFrequencyHz: frequencies[end],
                peakIndex: peak, peakFrequencyHz: frequencies[peak],
                peakIncreaseDb: deltas[peak], binCount: end - start + 1,
            ))
        }
        for index in deltas.indices {
            if deltas[index] >= minimumIncreaseDb {
                if start == nil { start = index }
            } else if start != nil {
                append(index - 1); start = nil
            }
        }
        if let start { append(deltas.count - 1) }
        guard detection.regions == expected else {
            throw PlatformError.invalidResponse("Области обнаружения не согласованы с разностью свипов")
        }
    }
}

private func averageScopeID(_ id: String) -> Bool {
    !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
}
private func averageID(_ id: String) -> Bool {
    !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && id.unicodeScalars.count <= 128
}
private func averageKnown(_ settings: [String: PlatformComparisonSetting]) -> [String: PlatformComparisonSetting]? {
    func recorded(_ value: PlatformComparisonSetting) -> Bool {
        switch value {
        case .null: return false
        case .number(let number): return number.isFinite
        case .array(let items): return items.allSatisfy(recorded)
        case .object(let items): return items.values.allSatisfy(recorded)
        default: return true
        }
    }
    let known = settings.filter { $0.key != "sequence" }
    return !known.isEmpty && known.values.allSatisfy(recorded) ? known : nil
}
// Foundation's ISO8601DateFormatter truncates fractional seconds. Parse them separately.
private func averageInterval(_ interval: PlatformInterval) -> (Int64, Int64)? {
    func instant(_ text: String) -> Int64? {
        let body: String
        if text.hasSuffix("Z") { body = String(text.dropLast()) }
        else if text.hasSuffix("+00:00") { body = String(text.dropLast(6)) }
        else { return nil }
        let pieces = body.split(separator: ".", omittingEmptySubsequences: false)
        guard (1...2).contains(pieces.count), pieces[0].utf8.count == 19 else { return nil }
        let secondsText = String(pieces[0]) + "Z"
        let formatter = ISO8601DateFormatter(); formatter.formatOptions = [.withInternetDateTime]
        guard let seconds = formatter.date(from: secondsText), formatter.string(from: seconds) == secondsText else { return nil }
        var fraction: Int64 = 0
        if pieces.count == 2 {
            let digits = String(pieces[1])
            guard (1...6).contains(digits.utf8.count), digits.utf8.allSatisfy({ (48...57).contains($0) }),
                  let microseconds = Int64(digits + String(repeating: "0", count: 6 - digits.utf8.count)) else { return nil }
            fraction = microseconds
        }
        return Int64(seconds.timeIntervalSince1970) * 1_000_000 + fraction
    }
    guard let start = instant(interval.startedAt), let end = instant(interval.endedAt), start <= end else { return nil }
    return (start, end)
}

extension PlatformClient {
    /// Explicit read-only POST. Local pinning never writes an archive reference.
    public func sweepAverageComparison(sessionID: String, referenceIDs: [String], targetID: String) async throws -> PlatformSweepAverageComparison {
        struct Body: Encodable { let referenceIds: [String]; let targetId: String }
        guard averageScopeID(sessionID), averageID(targetID), (2...16).contains(referenceIDs.count),
              referenceIDs.allSatisfy(averageID), Set(referenceIDs).count == referenceIDs.count,
              !referenceIDs.contains(targetID) else { throw PlatformError.invalidCommand("Нужны 2–16 разных ID эталонных свипов и отдельный целевой ID (до 128 символов)") }
        let body = Body(referenceIds: referenceIDs, targetId: targetID)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        let result: PlatformSweepAverageComparison = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-average-comparison", body: body, expectedStatuses: [200])
        try Task.checkCancellation()
        guard result.sessionId == sessionID, result.reference.sourceIds == referenceIDs,
              result.target.id == targetID else { throw PlatformError.invalidResponse("Сервер вернул другие источники усреднённого эталона") }
        return result
    }

    public func sweepAverageChangeDetection(
        sessionID: String, referenceIDs: [String], targetID: String,
        minimumIncreaseDb: Double, minimumBins: Int,
    ) async throws -> PlatformSweepAverageChangeDetection {
        struct Body: Encodable {
            let referenceIds: [String]
            let targetId: String
            let minimumIncreaseDb: Double
            let minimumBins: Int
        }
        guard averageScopeID(sessionID), averageID(targetID),
              (2...16).contains(referenceIDs.count), referenceIDs.allSatisfy(averageID),
              Set(referenceIDs).count == referenceIDs.count, !referenceIDs.contains(targetID),
              minimumIncreaseDb.isFinite, (0...120).contains(minimumIncreaseDb),
              (1...16_384).contains(minimumBins) else {
            throw PlatformError.invalidCommand("Некорректные ID свипов или параметры обнаружения")
        }
        let body = Body(
            referenceIds: referenceIDs, targetId: targetID,
            minimumIncreaseDb: minimumIncreaseDb, minimumBins: minimumBins,
        )
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else {
            throw PlatformError.invalidCommand("Запрос превышает 8192 байта")
        }
        try Task.checkCancellation()
        let result: PlatformSweepAverageChangeDetection = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-average-change-detection",
            body: body, expectedStatuses: [200],
        )
        try Task.checkCancellation()
        guard result.comparison.sessionId == sessionID,
              result.comparison.reference.sourceIds == referenceIDs,
              result.comparison.target.id == targetID else {
            throw PlatformError.invalidResponse("Сервер вернул другие свипы")
        }
        return result
    }
}
