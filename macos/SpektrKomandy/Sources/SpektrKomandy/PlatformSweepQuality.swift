import Foundation
import CoreFoundation

/// Read-only heuristic, never a hardware diagnosis or a sample rejection rule.
public struct PlatformSweepQuality: Decodable, Equatable {
    public struct Sweep: Decodable, Equatable {
        public let id: String
        public let interval: PlatformInterval
        public let levelUnit: String
        public let settings: PlatformComparisonSetting
    }
    public struct Parameters: Decodable, Equatable {
        public let edgeMarginDb: Double
        public let highLevelDbm: Double
        public static func valid(edge: Double, high: Double) -> Bool {
            edge.isFinite && high.isFinite && edge > 0 && edge <= 200 && (-200...100).contains(high)
        }
    }
    public struct Edge: Decodable, Equatable {
        public let edge: String
        public let index: Int
        public let frequencyHz: Int64
        public let levelDbm: Double
        public let neighborIndex: Int
        public let neighborLevelDbm: Double
        public let deltaDb: Double
        public let flagged: Bool
    }
    public let sessionId: String
    public let streamId: String
    public let sweep: Sweep
    public let parameters: Parameters
    public let points: Int
    public let peakIndex: Int
    public let peakFrequencyHz: Int64
    public let peakLevelDbm: Double
    public let highLevelCount: Int
    public let edgeAssessments: [Edge]
    public let flags: [String]
    public let assessment: String
    public let method: String
    public let hardwareDiagnosis: Bool
    public let rawModified: Bool

    /// Validate summary consistency without replacing the server's derived values.
    public func validate() throws {
        func validID(_ id: String) -> Bool { !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        let maxHz: Int64 = 9_007_199_254_740_991
        guard validID(sessionId), validID(streamId), validID(sweep.id), sweep.id.unicodeScalars.count <= 128,
              sweep.levelUnit == "dBm", averageInterval(sweep.interval) != nil,
              Parameters.valid(edge: parameters.edgeMarginDb, high: parameters.highLevelDbm),
              (2...16_384).contains(points), (0..<points).contains(peakIndex),
              (0...maxHz).contains(peakFrequencyHz), peakLevelDbm.isFinite,
              (0...points).contains(highLevelCount), edgeAssessments.count == 2,
              method == "archived_edge_level_review_v1", !hardwareDiagnosis, !rawModified else { throw invalid() }
        var knownLevels: [Int: Double] = [:]
        var knownFrequencies: [Int: Int64] = [peakIndex: peakFrequencyHz]
        for (position, edge) in edgeAssessments.enumerated() {
            guard edge.edge == (position == 0 ? "first" : "last"), edge.index == (position == 0 ? 0 : points - 1),
                  edge.neighborIndex == (position == 0 ? 1 : points - 2),
                  (0...maxHz).contains(edge.frequencyHz),
                  [edge.levelDbm, edge.neighborLevelDbm, edge.deltaDb].allSatisfy(\.isFinite),
                  edge.deltaDb == edge.levelDbm - edge.neighborLevelDbm,
                  edge.flagged == (edge.deltaDb >= parameters.edgeMarginDb),
                  peakLevelDbm >= max(edge.levelDbm, edge.neighborLevelDbm),
                  peakIndex != edge.index || (peakFrequencyHz == edge.frequencyHz && peakLevelDbm == edge.levelDbm)
            else { throw invalid() }
            for (index, level) in [(edge.index, edge.levelDbm), (edge.neighborIndex, edge.neighborLevelDbm)] {
                if let previous = knownLevels[index], previous != level { throw invalid() }
                knownLevels[index] = level
            }
            if let previous = knownFrequencies[edge.index], previous != edge.frequencyHz { throw invalid() }
            knownFrequencies[edge.index] = edge.frequencyHz
        }
        if let level = knownLevels[peakIndex], level != peakLevelDbm { throw invalid() }
        knownLevels[peakIndex] = peakLevelDbm
        // Every known sample constrains both the count and the first-maximum tie rule.
        let knownHigh = knownLevels.values.filter { $0 >= parameters.highLevelDbm }.count
        guard highLevelCount >= knownHigh, highLevelCount <= knownHigh + points - knownLevels.count,
              knownLevels.allSatisfy({ $0.value <= peakLevelDbm && ($0.value != peakLevelDbm || $0.key >= peakIndex) })
        else { throw invalid() }
        let frequencies = knownFrequencies.sorted { $0.key < $1.key }
        guard zip(frequencies, frequencies.dropFirst()).allSatisfy({
            $1.value - $0.value >= Int64($1.key - $0.key)
        }) else { throw invalid() }
        let expected = (edgeAssessments[0].flagged ? ["first_edge_jump"] : [])
            + (edgeAssessments[1].flagged ? ["last_edge_jump"] : [])
            + (highLevelCount > 0 ? ["level_threshold"] : [])
        guard flags == expected, assessment == (expected.isEmpty ? "no_flags" : "review"),
              (highLevelCount > 0) == (peakLevelDbm >= parameters.highLevelDbm),
              edgeAssessments[0].frequencyHz < edgeAssessments[1].frequencyHz,
              (edgeAssessments[0].frequencyHz...edgeAssessments[1].frequencyHz).contains(peakFrequencyHz) else { throw invalid() }
    }
    /// Bind every reported number and provenance field to the frozen archive selection.
    public func validate(source: PlatformSweep) throws {
        try validate()
        guard Self.validSource(source), Self.sameID(sessionId, source.sessionId), Self.sameID(streamId, source.streamId),
              Self.sameID(sweep.id, source.id), sweep.interval == source.interval,
              exactRecordedSettings(["value": sweep.settings], ["value": source.settings.map { .object($0) } ?? .null]),
              points == source.levels.count,
              let peak = source.levels.indices.max(by: { source.levels[$0] < source.levels[$1] }),
              peakLevelDbm == source.levels[peak], peakLevelDbm == source.levels[peakIndex],
              peakIndex == source.levels.firstIndex(of: peakLevelDbm), peakFrequencyHz == source.frequenciesHz[peakIndex],
              highLevelCount == source.levels.filter({ $0 >= parameters.highLevelDbm }).count else { throw invalid() }
        for edge in edgeAssessments {
            guard edge.frequencyHz == source.frequenciesHz[edge.index], edge.levelDbm == source.levels[edge.index],
                  edge.neighborLevelDbm == source.levels[edge.neighborIndex] else { throw invalid() }
        }
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool { PlatformThreeSweepThresholds.sameID(a, b) }
    public static func validSource(_ source: PlatformSweep) -> Bool {
        (2...16_384).contains(source.levels.count) && source.levelUnit == "dBm"
            && source.id.unicodeScalars.count <= 128 && !source.id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && source.levels.count == source.frequenciesHz.count && source.levels.allSatisfy(\.isFinite)
            && averageInterval(source.interval) != nil
            && zip(source.frequenciesHz, source.frequenciesHz.dropFirst()).allSatisfy({ $0 < $1 })
            && source.frequenciesHz.allSatisfy({ (0...9_007_199_254_740_991).contains($0) })
    }
    private func invalid() -> PlatformError { .invalidResponse("Некорректный или устаревший отчёт качества свипа") }
}

extension PlatformClient {
    public func sweepQuality(sessionID: String, sweepID: String, edgeMarginDb: Double = 20, highLevelDbm: Double = 0) async throws -> PlatformSweepQuality {
        struct Body: Encodable { let sweepId: String; let edgeMarginDb: Double; let highLevelDbm: Double }
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !sweepID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, sweepID.unicodeScalars.count <= 128,
              PlatformSweepQuality.Parameters.valid(edge: edgeMarginDb, high: highLevelDbm) else {
            throw PlatformError.invalidCommand("Нужен ID свипа до 128 символов; край: >0…200 dB, уровень: −200…100 dBm")
        }
        let body = Body(sweepId: sweepID, edgeMarginDb: edgeMarginDb, highLevelDbm: highLevelDbm)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 8192 else { throw PlatformError.invalidCommand("Запрос превышает 8192 байта") }
        try Task.checkCancellation()
        let result: PlatformSweepQuality = try await post("/sessions/\(Self.segment(sessionID))/sweep-quality", body: body,
            expectedStatuses: [200], validateResponse: Self.validateQualityJSON)
        try Task.checkCancellation()
        try result.validate()
        guard PlatformSweepQuality.sameID(result.sessionId, sessionID), PlatformSweepQuality.sameID(result.sweep.id, sweepID),
              result.parameters.edgeMarginDb == edgeMarginDb, result.parameters.highLevelDbm == highLevelDbm else {
            throw PlatformError.invalidResponse("Ответ относится к другому свипу или порогам")
        }
        return result
    }
    private static func validateQualityJSON(_ bytes: Data) throws {
        guard bytes.count <= 524_288 else { throw PlatformError.invalidResponse("Отчёт превышает 524 288 байт") }
        var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        func exact(_ value: Any?, _ keys: Set<String>) -> [String: Any]? {
            guard let object = value as? [String: Any], Set(object.keys) == keys else { return nil }; return object
        }
        func integer(_ value: Any?) -> Bool {
            guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return false }
            return !["f", "d"].contains(String(cString: n.objCType))
        }
        guard let envelope = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              let data = exact(envelope["data"], ["session_id", "stream_id", "sweep", "parameters", "points", "peak_index", "peak_frequency_hz", "peak_level_dbm", "high_level_count", "edge_assessments", "flags", "assessment", "method", "hardware_diagnosis", "raw_modified"]),
              let sweep = exact(data["sweep"], ["id", "interval", "level_unit", "settings"]),
              exact(sweep["interval"], ["started_at", "ended_at"]) != nil,
              exact(data["parameters"], ["edge_margin_db", "high_level_dbm"]) != nil,
              ["points", "peak_index", "peak_frequency_hz", "high_level_count"].allSatisfy({ integer(data[$0]) }),
              let edges = data["edge_assessments"] as? [[String: Any]], edges.count == 2,
              edges.allSatisfy({ exact($0, ["edge", "index", "frequency_hz", "level_dbm", "neighbor_index", "neighbor_level_dbm", "delta_db", "flagged"]) != nil && integer($0["index"]) && integer($0["frequency_hz"]) && integer($0["neighbor_index"]) }) else {
            throw PlatformError.invalidResponse("Некорректные поля отчёта качества")
        }
    }
}
