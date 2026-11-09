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

/// Sample-count occupancy reported by the core; acquisition coverage is not RF airtime.
public struct PlatformOccupancySummary: Decodable, Equatable {
    public let method: String
    public let comparison: String
    public let thresholdDbm: Double
    public let sampleCount: Int
    public let exceedanceCounts: [Int]
    public let sampleFractions: [Double]
    public let acquisitionSpanSeconds: Double
    public let acquisitionCoveredSeconds: Double
    public let acquisitionGapSeconds: Double
    public let acquisitionGapCount: Int

    private enum CodingKeys: String, CodingKey {
        case method, comparison, thresholdDbm, sampleCount, exceedanceCounts, sampleFractions
        case acquisitionSpanSeconds, acquisitionCoveredSeconds, acquisitionGapSeconds, acquisitionGapCount, rfTimeOccupancy
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        method = try v.decode(String.self, forKey: .method)
        comparison = try v.decode(String.self, forKey: .comparison)
        thresholdDbm = try v.decode(Double.self, forKey: .thresholdDbm)
        sampleCount = try v.decode(Int.self, forKey: .sampleCount)
        exceedanceCounts = try v.decode([Int].self, forKey: .exceedanceCounts)
        sampleFractions = try v.decode([Double].self, forKey: .sampleFractions)
        acquisitionSpanSeconds = try v.decode(Double.self, forKey: .acquisitionSpanSeconds)
        acquisitionCoveredSeconds = try v.decode(Double.self, forKey: .acquisitionCoveredSeconds)
        acquisitionGapSeconds = try v.decode(Double.self, forKey: .acquisitionGapSeconds)
        acquisitionGapCount = try v.decode(Int.self, forKey: .acquisitionGapCount)
        guard try v.decodeNil(forKey: .rfTimeOccupancy),
              method == "sample_fraction", comparison == "strictly_greater", thresholdDbm.isFinite,
              (1...33).contains(sampleCount), (0..<sampleCount).contains(acquisitionGapCount),
              [acquisitionSpanSeconds, acquisitionCoveredSeconds, acquisitionGapSeconds].allSatisfy({ $0.isFinite && $0 >= 0 }),
              acquisitionCoveredSeconds <= acquisitionSpanSeconds,
              abs(acquisitionSpanSeconds - acquisitionCoveredSeconds - acquisitionGapSeconds) <= 0.000001,
              exceedanceCounts.count == sampleFractions.count,
              zip(exceedanceCounts, sampleFractions).allSatisfy({
                  (0...sampleCount).contains($0) && $1.isFinite && abs($1 - Double($0) / Double(sampleCount)) <= 1e-12
              }) else { throw PlatformError.invalidResponse("Некорректная сводка занятости") }
    }
}

public struct PlatformSweepOccupancy: Decodable, Equatable {
    public let context: PlatformSweepContext
    public let occupancy: PlatformOccupancySummary
    private var exportPayload: Data?
    private enum CodingKeys: String, CodingKey { case occupancy }

    public init(from decoder: Decoder) throws {
        context = try PlatformSweepContext(from: decoder)
        occupancy = try decoder.container(keyedBy: CodingKeys.self).decode(PlatformOccupancySummary.self, forKey: .occupancy)
        guard occupancy.sampleCount == context.items.count,
              occupancy.exceedanceCounts.count == context.points else {
            throw PlatformError.invalidResponse("Занятость не соответствует архивному окну")
        }
    }
    public static func == (a: Self, b: Self) -> Bool { a.context == b.context && a.occupancy == b.occupancy }

    public static func validatedResponse(_ bytes: Data) throws -> Self {
        var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        struct Response: Decodable { let contract: String; let contractVersion: Int; let data: PlatformSweepOccupancy }
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(Response.self, from: bytes)
        guard response.contract == "openradioinspector-platform", response.contractVersion == 1 else {
            throw PlatformError.invalidResponse("Неизвестный контракт занятости")
        }
        var result = response.data
        result.exportPayload = try platformRawPayload(bytes)
        return result
    }
    public func exportData() throws -> Data {
        guard let exportPayload else { throw PlatformError.invalidResponse("Отсутствует исходный ответ сервера для экспорта") }
        var bytes = Data("{\"format\":\"openradioinspector-sweep-occupancy\",\"format_version\":1,\"data\":".utf8)
        bytes.append(exportPayload); bytes.append(contentsOf: "}\n".utf8)
        return bytes
    }
}

extension PlatformClient {
    public func sweepOccupancy(sessionID: String, sweepID: String, before: Int, after: Int, thresholdDbm: Double) async throws -> PlatformSweepOccupancy {
        struct Body: Encodable { let sweepId: String; let before: Int; let after: Int; let thresholdDbm: Double }
        guard contextScopeID(sessionID), contextSweepID(sweepID), PlatformContextCounts.valid(before),
              PlatformContextCounts.valid(after), thresholdDbm.isFinite else {
            throw PlatformError.invalidCommand("Нужны ID, N1/N2 от 0 до 16 и конечный порог dBm")
        }
        let body = Body(sweepId: sweepID, before: before, after: after, thresholdDbm: thresholdDbm)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        var wireResponse: Data?
        let _: PlatformSweepOccupancy = try await post("/sessions/\(Self.segment(sessionID))/sweep-occupancy", body: body, expectedStatuses: [200], validateResponse: { bytes in
            var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            wireResponse = bytes
        })
        try Task.checkCancellation()
        guard let wireResponse else { throw PlatformError.invalidResponse("Отсутствует ответ сервера") }
        let result = try PlatformSweepOccupancy.validatedResponse(wireResponse)
        guard PlatformSweepContext.sameID(result.context.sessionId, sessionID),
              PlatformSweepContext.sameID(result.context.anchorId, sweepID),
              result.context.requested.before == before, result.context.requested.after == after,
              result.occupancy.thresholdDbm == thresholdDbm else {
            throw PlatformError.invalidResponse("Сервер вернул другое окно или порог")
        }
        return result
    }
}
