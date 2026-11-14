import Foundation
import CoreFoundation

/// Source provenance is preserved independently from the core's derived mean-power levels.
public struct PlatformAverageSource: Decodable, Equatable, Sendable {
    public let id: String
    public let interval: PlatformInterval
    public let settings: [String: PlatformComparisonSetting]
}

public struct PlatformAveragedReference: Decodable, Equatable, Sendable {
    public let method: String
    public let count: Int
    public let sourceIds: [String]
    public let sources: [PlatformAverageSource]
    public let interval: PlatformInterval
    public let levels: [Double]
    public let levelUnit: String
}

public struct PlatformSweepAverageComparison: Decodable, Equatable, Sendable {
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

    private enum CodingKeys: String, CodingKey, Sendable {
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
        guard averageScopeID(sessionId), averageScopeID(streamId), averageID(target.id),
              target.levelUnit == "dBm", target.levels.allSatisfy(\.isFinite), averageInterval(target.interval) != nil,
              reference.method == "mean_power", (2...16).contains(reference.count),
              reference.count == reference.sourceIds.count, reference.count == reference.sources.count,
              averageUniqueIDs(reference.sourceIds), reference.sourceIds.allSatisfy(averageID),
              averageSameIDs(reference.sourceIds, reference.sources.map(\.id)),
              !reference.sourceIds.contains(where: { averageSameID($0, target.id) }),
              reference.levelUnit == "dBm", deltaUnit == "dB", (1...16_384).contains(points),
              frequenciesHz.count == points, reference.levels.count == points, target.levels.count == points,
              deltaLevels.count == points, frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              reference.levels.allSatisfy(\.isFinite), deltaLevels.allSatisfy(\.isFinite),
              reference.sources.allSatisfy({ averageInterval($0.interval) != nil && averageKnown($0.settings) != nil }),
              let known = averageKnown(target.settings),
              reference.sources.allSatisfy({ averageKnown($0.settings).map { exactRecordedSettings($0, known) } == true }),
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
              averageSameID(sessionId, frozenTarget.sessionId), averageSameID(streamId, frozenTarget.streamId),
              averageSameIDs(reference.sourceIds, sources.map(\.id)), frequenciesHz == frozenTarget.frequenciesHz,
              averageSameID(target.id, frozenTarget.id), target.interval == frozenTarget.interval,
              frozenTarget.settings.map({ exactRecordedSettings(target.settings, $0) }) == true, target.levels == frozenTarget.levels,
              target.levelUnit == frozenTarget.levelUnit,
              zip(reference.sources, sources).allSatisfy({ returned, frozen in
                  averageSameID(returned.id, frozen.id) && returned.interval == frozen.interval
                      && frozen.settings.map({ exactRecordedSettings(returned.settings, $0) }) == true
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
        guard (2...16).contains(sources.count), averageUniqueIDs(all.map(\.id)),
              averageScopeID(target.sessionId), averageScopeID(target.streamId),
              let settings = target.settings.flatMap(averageKnown) else { return false }
        return all.allSatisfy {
            averageID($0.id) && averageSameID($0.sessionId, target.sessionId) && averageSameID($0.streamId, target.streamId)
                && $0.levelUnit == "dBm" && (1...16_384).contains($0.levels.count)
                && $0.levels.count == $0.frequenciesHz.count && $0.levels.allSatisfy(\.isFinite)
                && $0.frequenciesHz == target.frequenciesHz
                && $0.frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) })
                && zip($0.frequenciesHz, $0.frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 })
                && averageInterval($0.interval) != nil
                && $0.settings.flatMap(averageKnown).map({ exactRecordedSettings($0, settings) }) == true
        }
    }
}

public struct PlatformSweepChangeRegion: Decodable, Equatable, Sendable {
    public let startIndex: Int
    public let endIndex: Int
    public let startFrequencyHz: Int64
    public let endFrequencyHz: Int64
    public let peakIndex: Int
    public let peakFrequencyHz: Int64
    public let peakIncreaseDb: Double
    public let binCount: Int
}

public struct PlatformSweepChangeDetection: Decodable, Equatable, Sendable {
    public let minimumIncreaseDb: Double
    public let minimumBins: Int
    public let regions: [PlatformSweepChangeRegion]
    public let count: Int
}

public struct PlatformSweepAverageChangeDetection: Decodable, Equatable, Sendable {
    public let comparison: PlatformSweepAverageComparison
    public let detection: PlatformSweepChangeDetection
    private var exportPayload: Data?

    public static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.comparison == rhs.comparison && lhs.detection == rhs.detection
    }

    /// Retain the validated wire payload. Decoding settings into Double is never an export path.
    public static func validatedResponse(_ bytes: Data) throws -> Self {
        try averageResponseShape(bytes, detection: true)
        struct Response: Decodable, Sendable {
            let contract: String
            let contractVersion: Int
            let data: PlatformSweepAverageChangeDetection
        }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(Response.self, from: bytes)
        guard response.contract == "openradioinspector-platform", response.contractVersion == 1 else {
            throw PlatformError.invalidResponse("Неподдерживаемый контракт ответа сервера")
        }
        var result = response.data
        result.exportPayload = try platformRawPayload(bytes)
        return result
    }

    fileprivate func exportData() throws -> Data {
        guard let exportPayload else {
            throw PlatformError.invalidResponse("Исходный проверенный JSON недоступен. Повторите анализ.")
        }
        return exportPayload
    }

    private enum CodingKeys: String, CodingKey, Sendable { case comparison, detection }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        comparison = try values.decode(PlatformSweepAverageComparison.self, forKey: .comparison)
        detection = try values.decode(PlatformSweepChangeDetection.self, forKey: .detection)
        try validateDetection(minimumIncreaseDb: detection.minimumIncreaseDb, minimumBins: detection.minimumBins)
    }

    public func validate(
        sources: [PlatformSweep], target: PlatformSweep,
        minimumIncreaseDb: Double, minimumBins: Int,
    ) throws {
        try comparison.validate(sources: sources, target: target)
        try validateDetection(minimumIncreaseDb: minimumIncreaseDb, minimumBins: minimumBins)
    }

    /// Verify the entire maximal region list, including omitted regions and first-peak ties.
    /// Rendering still uses the core response, never this validation-only reconstruction.
    fileprivate func validateDetection(minimumIncreaseDb: Double, minimumBins: Int) throws {
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
        if start != nil { append(deltas.count - 1) }
        guard detection.regions == expected else {
            throw PlatformError.invalidResponse("Области обнаружения не согласованы с разностью свипов")
        }
    }
}

/// Versioned local artifact. The payload is the exact validated server JSON, not a re-encoding.
public struct PlatformSweepAverageExportArtifact: Sendable {
    public let data: PlatformSweepAverageChangeDetection
    public init(data: PlatformSweepAverageChangeDetection) { self.data = data }
    public func canonicalData() throws -> Data {
        var bytes = Data("{\"format\":\"openradioinspector-sweep-average-change-detection\",\"format_version\":1,\"data\":".utf8)
        bytes.append(try data.exportData())
        bytes.append(contentsOf: "}\n".utf8)
        return bytes
    }
}

/// Locate the top-level data object without Foundation bridging, key normalization or numeric conversion.
/// The complete document has already passed duplicate-key, shape and semantic validation.
func platformRawPayload(_ data: Data) throws -> Data {
    let bytes = Array(data)
    var index = 0
    func whitespace() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
    func stringEnd() {
        index += 1
        while index < bytes.count {
            let byte = bytes[index]; index += 1
            if byte == 92 { index += 1 }
            else if byte == 34 { return }
        }
    }
    func valueEnd() {
        var depth = 0
        while index < bytes.count {
            let byte = bytes[index]
            if byte == 34 { stringEnd(); continue }
            if byte == 123 || byte == 91 { depth += 1 }
            if byte == 125 || byte == 93 {
                if depth == 0 { return }
                depth -= 1
                if depth == 0 { index += 1; return }
            }
            if byte == 44 && depth == 0 { return }
            index += 1
        }
    }
    whitespace(); index += 1
    while index < bytes.count {
        whitespace()
        guard bytes[index] == 34 else { break }
        let keyStart = index; stringEnd()
        let key = try JSONDecoder().decode(String.self, from: Data(bytes[keyStart..<index]))
        whitespace(); index += 1; whitespace()
        let start = index; valueEnd()
        if key == "data" { return Data(bytes[start..<index]) }
        whitespace()
        guard index < bytes.count, bytes[index] == 44 else { break }
        index += 1
    }
    throw PlatformError.invalidResponse("Исходный JSON не содержит результата анализа")
}

private func averageSameID(_ left: String, _ right: String) -> Bool {
    PlatformThreeSweepThresholds.sameID(left, right)
}
private func averageSameIDs(_ left: [String], _ right: [String]) -> Bool {
    left.count == right.count && zip(left, right).allSatisfy(averageSameID)
}
private func averageUniqueIDs(_ values: [String]) -> Bool {
    Set(values.map { Data($0.utf8) }).count == values.count
}
private func averageScopeID(_ id: String) -> Bool {
    // Match Python str.strip's 29 whitespace scalars, without normalizing the identity.
    // Foundation additionally treats U+200B as blank and omits U+001C...U+001F.
    !id.isEmpty && !id.unicodeScalars.allSatisfy { scalar in
        switch scalar.value {
        case 0x0009...0x000D, 0x001C...0x0020, 0x0085, 0x00A0, 0x1680,
             0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000: return true
        default: return false
        }
    }
}
private func averageID(_ id: String) -> Bool {
    averageScopeID(id) && id.unicodeScalars.count <= 128
}
func averageKnown(_ settings: [String: PlatformComparisonSetting]) -> [String: PlatformComparisonSetting]? {
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
func averageInterval(_ interval: PlatformInterval) -> (Int64, Int64)? {
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

private struct AverageRawKey: CodingKey, Sendable {
    let stringValue: String
    var intValue: Int? { nil }
    init(_ value: String) { stringValue = value }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

/// Check original keys before Foundation can strip a leading U+FEFF during NSString bridging.
/// Settings are arbitrary recorded JSON, so their keys and values are deliberately not rewritten.
private struct AverageRawEnvelope: Decodable, Sendable {
    init(from decoder: Decoder) throws {
        func object(_ decoder: Decoder, _ keys: Set<String>) throws -> KeyedDecodingContainer<AverageRawKey> {
            let value = try decoder.container(keyedBy: AverageRawKey.self)
            guard Set(value.allKeys.map(\.stringValue)) == keys else {
                throw PlatformError.invalidResponse("Некорректные исходные поля JSON усреднённого эталона")
            }
            return value
        }
        func interval(_ decoder: Decoder) throws { _ = try object(decoder, ["started_at", "ended_at"]) }
        func source(_ decoder: Decoder, target: Bool) throws {
            let keys: Set<String> = target ? ["id", "interval", "settings", "levels", "level_unit"] : ["id", "interval", "settings"]
            let value = try object(decoder, keys)
            try interval(value.superDecoder(forKey: .init("interval")))
        }
        func comparison(_ decoder: Decoder) throws {
            let value = try object(decoder, ["session_id", "stream_id", "frequencies_hz", "reference", "target",
                "delta_levels", "delta_unit", "max_increase", "min_change", "points"])
            let reference = try object(value.superDecoder(forKey: .init("reference")),
                ["method", "count", "source_ids", "sources", "interval", "levels", "level_unit"])
            try interval(reference.superDecoder(forKey: .init("interval")))
            var sources = try reference.nestedUnkeyedContainer(forKey: .init("sources"))
            while !sources.isAtEnd { try source(sources.superDecoder(), target: false) }
            try source(value.superDecoder(forKey: .init("target")), target: true)
            for name in ["max_increase", "min_change"] {
                _ = try object(value.superDecoder(forKey: .init(name)), ["frequency_hz", "delta_db"])
            }
        }
        let envelope = try object(decoder, ["contract", "contract_version", "data"])
        let data = try envelope.superDecoder(forKey: .init("data"))
        let keys = try data.container(keyedBy: AverageRawKey.self)
        if keys.contains(.init("comparison")) {
            let payload = try object(data, ["comparison", "detection"])
            try comparison(payload.superDecoder(forKey: .init("comparison")))
            let found = try object(payload.superDecoder(forKey: .init("detection")),
                ["minimum_increase_db", "minimum_bins", "regions", "count"])
            var regions = try found.nestedUnkeyedContainer(forKey: .init("regions"))
            while !regions.isAtEnd {
                _ = try object(regions.superDecoder(), ["start_index", "end_index", "start_frequency_hz", "end_frequency_hz",
                    "peak_index", "peak_frequency_hz", "peak_increase_db", "bin_count"])
            }
        } else { try comparison(data) }
    }
}

/// Preserve wire field spellings, integer types and provenance before JSONDecoder's key conversion.
private func averageResponseShape(_ bytes: Data, detection: Bool) throws {
    var scanner = MaskJSONScanner(bytes: Array(bytes), exactKeyIdentity: detection); try scanner.check()
    _ = try JSONDecoder().decode(AverageRawEnvelope.self, from: bytes)
    func invalid() -> PlatformError { .invalidResponse("Некорректные поля или JSON-типы усреднённого эталона") }
    func object(_ value: Any?, keys: Set<String>) throws -> [String: Any] {
        guard let result = value as? [String: Any], Set(result.keys) == keys else { throw invalid() }
        return result
    }
    func integer(_ value: Any?) -> Bool {
        guard let value = value as? NSNumber, CFGetTypeID(value) != CFBooleanGetTypeID() else { return false }
        return !["f", "d"].contains(String(cString: value.objCType))
    }
    let envelope = try object(JSONSerialization.jsonObject(with: bytes), keys: ["contract", "contract_version", "data"])
    guard integer(envelope["contract_version"]) else { throw invalid() }
    let comparison: [String: Any]
    let comparisonKeys: Set<String> = ["session_id", "stream_id", "frequencies_hz", "reference", "target",
        "delta_levels", "delta_unit", "max_increase", "min_change", "points"]
    if detection {
        let data = try object(envelope["data"], keys: ["comparison", "detection"])
        comparison = try object(data["comparison"], keys: comparisonKeys)
        let found = try object(data["detection"], keys: ["minimum_increase_db", "minimum_bins", "regions", "count"])
        guard integer(found["minimum_bins"]), integer(found["count"]),
              let regions = found["regions"] as? [[String: Any]], regions.count <= 16_384 else { throw invalid() }
        let integerFields: Set<String> = ["start_index", "end_index", "start_frequency_hz", "end_frequency_hz",
            "peak_index", "peak_frequency_hz", "bin_count"]
        for region in regions {
            _ = try object(region, keys: integerFields.union(["peak_increase_db"]))
            guard integerFields.allSatisfy({ integer(region[$0]) }) else { throw invalid() }
        }
    } else { comparison = try object(envelope["data"], keys: comparisonKeys) }
    guard integer(comparison["points"]), let frequencies = comparison["frequencies_hz"] as? [Any],
          frequencies.allSatisfy(integer) else { throw invalid() }
    for name in ["max_increase", "min_change"] {
        let extremum = try object(comparison[name], keys: ["frequency_hz", "delta_db"])
        guard integer(extremum["frequency_hz"]) else { throw invalid() }
    }
    let reference = try object(comparison["reference"], keys: ["method", "count", "source_ids", "sources", "interval", "levels", "level_unit"])
    guard integer(reference["count"]), let sources = reference["sources"] as? [[String: Any]] else { throw invalid() }
    _ = try object(reference["interval"], keys: ["started_at", "ended_at"])
    for source in sources {
        _ = try object(source, keys: ["id", "interval", "settings"])
        _ = try object(source["interval"], keys: ["started_at", "ended_at"])
    }
    let target = try object(comparison["target"], keys: ["id", "interval", "settings", "levels", "level_unit"])
    _ = try object(target["interval"], keys: ["started_at", "ended_at"])
}

extension PlatformClient {
    /// Explicit read-only POST. Local pinning never writes an archive reference.
    public func sweepAverageComparison(sessionID: String, referenceIDs: [String], targetID: String) async throws -> PlatformSweepAverageComparison {
        struct Body: Encodable, Sendable { let referenceIds: [String]; let targetId: String }
        guard averageScopeID(sessionID), averageID(targetID), (2...16).contains(referenceIDs.count),
              referenceIDs.allSatisfy(averageID), averageUniqueIDs(referenceIDs),
              !referenceIDs.contains(where: { averageSameID($0, targetID) }) else { throw PlatformError.invalidCommand("Нужны 2–16 разных ID эталонных свипов и отдельный целевой ID (до 128 символов)") }
        let body = Body(referenceIds: referenceIDs, targetId: targetID)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        let result: PlatformSweepAverageComparison = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-average-comparison", body: body, expectedStatuses: [200],
            validateResponse: { try averageResponseShape($0, detection: false) })
        try Task.checkCancellation()
        guard averageSameID(result.sessionId, sessionID), averageSameIDs(result.reference.sourceIds, referenceIDs),
              averageSameID(result.target.id, targetID) else { throw PlatformError.invalidResponse("Сервер вернул другие источники усреднённого эталона") }
        return result
    }

    public func sweepAverageChangeDetection(
        sessionID: String, referenceIDs: [String], targetID: String,
        minimumIncreaseDb: Double, minimumBins: Int,
    ) async throws -> PlatformSweepAverageChangeDetection {
        struct Body: Encodable, Sendable {
            let referenceIds: [String]
            let targetId: String
            let minimumIncreaseDb: Double
            let minimumBins: Int
        }
        guard averageScopeID(sessionID), averageID(targetID),
              (2...16).contains(referenceIDs.count), referenceIDs.allSatisfy(averageID),
              averageUniqueIDs(referenceIDs), !referenceIDs.contains(where: { averageSameID($0, targetID) }),
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
        var wireResponse: Data?
        let _: PlatformSweepAverageChangeDetection = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-average-change-detection",
            body: body, expectedStatuses: [200],
            validateResponse: { bytes in
                try averageResponseShape(bytes, detection: true)
                wireResponse = bytes
            },
        )
        try Task.checkCancellation()
        guard let wireResponse else { throw PlatformError.invalidResponse("Отсутствует исходный ответ анализа") }
        let result = try PlatformSweepAverageChangeDetection.validatedResponse(wireResponse)
        guard averageSameID(result.comparison.sessionId, sessionID),
              averageSameIDs(result.comparison.reference.sourceIds, referenceIDs),
              averageSameID(result.comparison.target.id, targetID) else {
            throw PlatformError.invalidResponse("Сервер вернул другие свипы")
        }
        try result.validateDetection(minimumIncreaseDb: minimumIncreaseDb, minimumBins: minimumBins)
        return result
    }
}
