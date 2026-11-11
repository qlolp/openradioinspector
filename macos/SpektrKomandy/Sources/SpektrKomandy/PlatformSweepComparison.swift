import Foundation

/// Recorded JSON provenance, including unknown extension keys. Never coerces a boolean to a number.
public indirect enum PlatformComparisonSetting: Decodable, Equatable, Sendable {
    case string(String), number(Double), boolean(Bool), array([Self]), object([String: Self]), null

    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer()
        if value.decodeNil() { self = .null }
        else if let bool = try? value.decode(Bool.self) { self = .boolean(bool) }
        else if let number = try? value.decode(Double.self), number.isFinite { self = .number(number) }
        else if let string = try? value.decode(String.self) { self = .string(string) }
        else if let array = try? value.decode([Self].self) { self = .array(array) }
        else { self = .object(try value.decode([String: Self].self)) }
    }

    fileprivate var isRecorded: Bool {
        switch self {
        case .null: return false
        case .array(let values): return values.allSatisfy(\.isRecorded)
        case .object(let values): return values.values.allSatisfy(\.isRecorded)
        default: return true
        }
    }
}

public struct PlatformComparisonSweep: Decodable, Equatable, Identifiable, Sendable {
    public let id: String
    public let interval: PlatformInterval
    public let levels: [Double]
    public let levelUnit: String
    public let settings: [String: PlatformComparisonSetting]

    var isValid: Bool {
        let recorded = settings.filter { $0.key != "sequence" }
        return !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && levelUnit == "dBm" && !levels.isEmpty && levels.allSatisfy(\.isFinite)
            && !recorded.isEmpty && recorded.values.allSatisfy(\.isRecorded)
            && comparisonIntervalIsUTC(interval)
    }
}

public struct PlatformComparisonExtremum: Decodable, Equatable, Sendable {
    public let frequencyHz: Int64
    public let deltaDb: Double
}

/// Server-computed target minus reference. Raw dBm levels remain separate from derived dB.
public struct PlatformSweepComparison: Decodable, Equatable, Sendable {
    public let sessionId: String
    public let streamId: String
    public let reference: PlatformComparisonSweep
    public let target: PlatformComparisonSweep
    public let frequenciesHz: [Int64]
    public let deltaLevels: [Double]
    public let deltaUnit: String
    public let maxIncrease: PlatformComparisonExtremum
    public let minChange: PlatformComparisonExtremum
    public let points: Int

    private enum CodingKeys: String, CodingKey, Sendable {
        case sessionId, streamId, reference, target, frequenciesHz, deltaLevels, deltaUnit
        case maxIncrease, minChange, points
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        streamId = try values.decode(String.self, forKey: .streamId)
        reference = try values.decode(PlatformComparisonSweep.self, forKey: .reference)
        target = try values.decode(PlatformComparisonSweep.self, forKey: .target)
        frequenciesHz = try values.decode([Int64].self, forKey: .frequenciesHz)
        deltaLevels = try values.decode([Double].self, forKey: .deltaLevels)
        deltaUnit = try values.decode(String.self, forKey: .deltaUnit)
        maxIncrease = try values.decode(PlatformComparisonExtremum.self, forKey: .maxIncrease)
        minChange = try values.decode(PlatformComparisonExtremum.self, forKey: .minChange)
        points = try values.decode(Int.self, forKey: .points)
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              reference.isValid, target.isValid, deltaUnit == "dB", (1...16_384).contains(points),
              frequenciesHz.count == points, deltaLevels.count == points,
              reference.levels.count == points, target.levels.count == points,
              frequenciesHz.allSatisfy({ $0 >= 0 }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              deltaLevels.allSatisfy(\.isFinite),
              maxIncrease.deltaDb.isFinite, minChange.deltaDb.isFinite,
              let maximum = deltaLevels.max(), let minimum = deltaLevels.min(),
              let maxIndex = deltaLevels.firstIndex(of: maximum),
              let minIndex = deltaLevels.firstIndex(of: minimum),
              maxIncrease.deltaDb == maximum, minChange.deltaDb == minimum,
              maxIncrease.frequencyHz == frequenciesHz[maxIndex],
              minChange.frequencyHz == frequenciesHz[minIndex] else {
            throw DecodingError.dataCorruptedError(
                forKey: .deltaLevels, in: values,
                debugDescription: "Некорректный результат сравнения архивных свипов"
            )
        }
    }
}

private func comparisonIntervalIsUTC(_ interval: PlatformInterval) -> Bool {
    func date(_ text: String) -> Date? {
        guard text.hasSuffix("Z") || text.hasSuffix("+00:00") else { return nil }
        let format = ISO8601DateFormatter()
        format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = format.date(from: text) { return date }
        format.formatOptions = [.withInternetDateTime]
        return format.date(from: text)
    }
    guard let start = date(interval.startedAt), let end = date(interval.endedAt) else { return false }
    return start <= end
}

extension PlatformClient {
    public func sweepComparison(
        sessionID: String, referenceID: String, targetID: String
    ) async throws -> PlatformSweepComparison {
        guard [sessionID, referenceID, targetID].allSatisfy({
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }) else {
            throw PlatformError.invalidCommand("Для сравнения нужны ID сеанса и двух архивных свипов")
        }
        try Task.checkCancellation()
        let result: PlatformSweepComparison = try await get(
            "/sessions/\(Self.segment(sessionID))/sweep-comparison",
            query: [URLQueryItem(name: "reference_id", value: referenceID),
                    URLQueryItem(name: "target_id", value: targetID)]
        )
        try Task.checkCancellation()
        guard result.sessionId == sessionID,
              result.reference.id == referenceID, result.target.id == targetID else {
            throw PlatformError.invalidResponse("Сервер вернул сравнение других архивных свипов")
        }
        return result
    }
}
