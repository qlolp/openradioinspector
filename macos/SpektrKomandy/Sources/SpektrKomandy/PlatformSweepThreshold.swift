import Foundation

public struct PlatformSweepThresholdParameters: Decodable, Equatable {
    public let method: String
    public let windowPoints: Int
    public let marginDb: Double
    public let edgePolicy: String
    public let comparison: String

    public static func valid(windowPoints: Int, marginDb: Double) -> Bool {
        (3...101).contains(windowPoints) && windowPoints % 2 == 1
            && marginDb.isFinite && (0...120).contains(marginDb)
    }

    var isValid: Bool {
        method == "rolling_median" && edgePolicy == "clip" && comparison == "strictly_greater"
            && Self.valid(windowPoints: windowPoints, marginDb: marginDb)
    }
}

public struct PlatformSweepThresholdRegion: Decodable, Equatable, Identifiable {
    public let startIndex: Int
    public let endIndex: Int
    public let startFrequencyHz: Int64
    public let endFrequencyHz: Int64
    public let peakIndex: Int
    public let peakFrequencyHz: Int64
    public let peakExcessDb: Double
    public let points: Int
    public var id: Int { startIndex }
}

/// Read-only core calculation. Raw levels and derived dBm threshold / dB excess stay separate.
public struct PlatformSweepThreshold: Decodable, Equatable {
    public let sessionId: String
    public let streamId: String
    public let sweep: PlatformComparisonSweep
    public let frequenciesHz: [Int64]
    public let parameters: PlatformSweepThresholdParameters
    public let baselineLevels: [Double]
    public let thresholdLevels: [Double]
    public let levelUnit: String
    public let excessLevels: [Double]
    public let deltaUnit: String
    public let regions: [PlatformSweepThresholdRegion]
    public let points: Int
    public let exceedancePoints: Int
    public let regionCount: Int

    private enum CodingKeys: String, CodingKey {
        case sessionId, streamId, sweep, frequenciesHz, parameters, baselineLevels, thresholdLevels
        case levelUnit, excessLevels, deltaUnit, regions, points, exceedancePoints, regionCount
    }

    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        streamId = try values.decode(String.self, forKey: .streamId)
        sweep = try values.decode(PlatformComparisonSweep.self, forKey: .sweep)
        frequenciesHz = try values.decode([Int64].self, forKey: .frequenciesHz)
        parameters = try values.decode(PlatformSweepThresholdParameters.self, forKey: .parameters)
        baselineLevels = try values.decode([Double].self, forKey: .baselineLevels)
        thresholdLevels = try values.decode([Double].self, forKey: .thresholdLevels)
        levelUnit = try values.decode(String.self, forKey: .levelUnit)
        excessLevels = try values.decode([Double].self, forKey: .excessLevels)
        deltaUnit = try values.decode(String.self, forKey: .deltaUnit)
        regions = try values.decode([PlatformSweepThresholdRegion].self, forKey: .regions)
        points = try values.decode(Int.self, forKey: .points)
        exceedancePoints = try values.decode(Int.self, forKey: .exceedancePoints)
        regionCount = try values.decode(Int.self, forKey: .regionCount)
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              sweep.isValid, parameters.isValid, levelUnit == "dBm", deltaUnit == "dB",
              (3...16_384).contains(points), frequenciesHz.count == points,
              sweep.levels.count == points, baselineLevels.count == points,
              thresholdLevels.count == points, excessLevels.count == points,
              frequenciesHz.allSatisfy({ $0 >= 0 }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              baselineLevels.allSatisfy(\.isFinite), thresholdLevels.allSatisfy(\.isFinite),
              excessLevels.allSatisfy(\.isFinite), regionCount == regions.count,
              exceedancePoints == excessLevels.filter({ $0 > 0 }).count else {
            throw DecodingError.dataCorruptedError(forKey: .points, in: values,
                debugDescription: "Некорректный результат адаптивного порога")
        }
        // Consistency validation only. UI renders the core's arrays, never a client calculation.
        for index in 0..<points {
            guard thresholdLevels[index] == baselineLevels[index] + parameters.marginDb,
                  excessLevels[index] == sweep.levels[index] - thresholdLevels[index] else {
                throw DecodingError.dataCorruptedError(forKey: .thresholdLevels, in: values,
                    debugDescription: "Несогласованные единицы или производные уровни")
            }
        }
        var cursor = 0
        for region in regions {
            while cursor < points && excessLevels[cursor] <= 0 { cursor += 1 }
            guard cursor < points, region.startIndex == cursor else {
                throw DecodingError.dataCorruptedError(forKey: .regions, in: values,
                    debugDescription: "Неверное начало непрерывной области")
            }
            var peak = cursor
            while cursor < points && excessLevels[cursor] > 0 {
                if excessLevels[cursor] > excessLevels[peak] { peak = cursor }
                cursor += 1
            }
            guard region.endIndex == cursor - 1,
                  region.points == cursor - region.startIndex,
                  region.startFrequencyHz == frequenciesHz[region.startIndex],
                  region.endFrequencyHz == frequenciesHz[cursor - 1],
                  region.peakIndex == peak, region.peakFrequencyHz == frequenciesHz[peak],
                  region.peakExcessDb.isFinite, region.peakExcessDb == excessLevels[peak] else {
                throw DecodingError.dataCorruptedError(forKey: .regions, in: values,
                    debugDescription: "Неверные границы или максимум непрерывной области")
            }
        }
        guard excessLevels.dropFirst(cursor).allSatisfy({ $0 <= 0 }) else {
            throw DecodingError.dataCorruptedError(forKey: .regions, in: values,
                debugDescription: "Пропущена область превышения")
        }
    }
}

extension PlatformClient {
    public func sweepThreshold(
        sessionID: String, sweepID: String, windowPoints: Int = 21, marginDb: Double = 6
    ) async throws -> PlatformSweepThreshold {
        guard [sessionID, sweepID].allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              PlatformSweepThresholdParameters.valid(windowPoints: windowPoints, marginDb: marginDb) else {
            throw PlatformError.invalidCommand("Нужны ID сеанса и свипа, нечётное окно 3–101 и конечный запас 0–120 dB")
        }
        try Task.checkCancellation()
        let result: PlatformSweepThreshold = try await get(
            "/sessions/\(Self.segment(sessionID))/sweep-threshold",
            query: [URLQueryItem(name: "sweep_id", value: sweepID),
                    URLQueryItem(name: "window_points", value: String(windowPoints)),
                    URLQueryItem(name: "margin_db", value: String(marginDb))], expectedStatuses: [200]
        )
        try Task.checkCancellation()
        guard result.sessionId == sessionID, result.sweep.id == sweepID,
              result.parameters.windowPoints == windowPoints, result.parameters.marginDb == marginDb else {
            throw PlatformError.invalidResponse("Сервер вернул порог другого свипа или других параметров")
        }
        return result
    }
}
