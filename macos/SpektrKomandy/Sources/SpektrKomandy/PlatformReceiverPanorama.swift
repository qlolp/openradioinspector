import Foundation

public struct PlatformReceiverPanorama: Decodable, Sendable {
    public struct Bin: Decodable, Sendable {
        public let startIndex, endIndex, measuredPoints: Int
        public let frequencyHz: Int64
        public let level: Double
        public let observationId, startedAt, endedAt: String
    }
    public let pass, totalPoints, measuredPoints: Int
    public let contextId: String?
    public let complete, lowerIsStronger: Bool
    public let startHz, stopHz, stepHz: Int
    public let levelUnit: String
    public let startedAt, endedAt: String?
    public let bins: [Bin]
    private let coverageHex: String?

    private func index(of hz: Int64) -> Int? {
        guard Int64(startHz)...Int64(stopHz) ~= hz else { return nil }
        if hz == stopHz { return totalPoints - 1 }
        let delta = Int(hz) - startHz
        return delta.isMultiple(of: stepHz) ? delta / stepHz : nil
    }

    /// Consult the archived bit set once per response. Without it only complete bins
    /// and each bin's original peak have known positions.
    public func measuredRanges() -> [ClosedRange<Int>] {
        guard let bytes = coverageBytes else {
            return bins.compactMap { bin in
                if bin.measuredPoints == bin.endIndex - bin.startIndex + 1 { return bin.startIndex...bin.endIndex }
                return index(of: bin.frequencyHz).map { $0...$0 }
            }
        }
        var ranges: [ClosedRange<Int>] = []
        var first: Int?
        for index in 0..<totalPoints {
            if bytes[index / 8] & (1 << (index % 8)) != 0 {
                if first == nil { first = index }
            } else if let start = first {
                ranges.append(start...(index - 1))
                first = nil
            }
        }
        if let first { ranges.append(first...(totalPoints - 1)) }
        return ranges
    }

    private var coverageBytes: [UInt8]? {
        guard let coverageHex, coverageHex.utf8.count == 2 * ((totalPoints + 7) / 8) else { return nil }
        func nibble(_ ch: UInt8) -> UInt8? {
            switch ch { case 48...57: ch - 48; case 97...102: ch - 87; default: nil }
        }
        let text = Array(coverageHex.utf8)
        var bytes: [UInt8] = []
        for i in stride(from: 0, to: text.count, by: 2) {
            guard let high = nibble(text[i]), let low = nibble(text[i + 1]) else { return nil }
            bytes.append(high << 4 | low)
        }
        return bytes
    }

    func validated(requestedPass: Int?, columns: Int) throws -> Self {
        let issue = PlatformError.invalidResponse("Панорама AR-DV1 не соответствует проходу, сетке или архивному покрытию.")
        guard let grid = try? PlatformARDV1ScopeConfiguration(startHz: startHz, stopHz: stopHz, stepHz: stepHz, cyclic: true),
              pass > 0, requestedPass == nil || pass == requestedPass, grid.pointCount == totalPoints,
              (0...totalPoints).contains(measuredPoints), complete == (measuredPoints == totalPoints),
              levelUnit == "s_meter_count", lowerIsStronger, bins.count <= columns,
              measuredPoints == 0 || Self.interval(startedAt, endedAt) else { throw issue }
        let bytes = coverageBytes
        if coverageHex != nil {
            guard let bytes, bytes.reduce(0, { $0 + $1.nonzeroBitCount }) == measuredPoints else { throw issue }
        }
        var last = -1, measured = 0
        for bin in bins {
            guard last < bin.startIndex, bin.startIndex <= bin.endIndex, bin.endIndex < totalPoints,
                  (1...(bin.endIndex - bin.startIndex + 1)).contains(bin.measuredPoints),
                  let index = index(of: bin.frequencyHz), (bin.startIndex...bin.endIndex).contains(index),
                  bin.level.isFinite, !bin.observationId.isEmpty, Self.interval(bin.startedAt, bin.endedAt) else { throw issue }
            if let bytes {
                guard bytes[index / 8] & (1 << (index % 8)) != 0,
                      (bin.startIndex...bin.endIndex).lazy.filter({ bytes[$0 / 8] & (1 << ($0 % 8)) != 0 }).count == bin.measuredPoints else {
                    throw issue
                }
            }
            measured += bin.measuredPoints
            last = bin.endIndex
        }
        guard measured == measuredPoints else { throw issue }
        return self
    }

    private static func interval(_ start: String?, _ end: String?) -> Bool {
        guard let start, let end, let a = try? PlatformTimestamp(start), let b = try? PlatformTimestamp(end) else { return false }
        return a <= b
    }

    /// Use adjacent observed pass starts when available. A partial-pass estimate requires observed elapsed time.
    public func revisitSeconds(after previous: Self?) -> (seconds: Double, estimated: Bool)? {
        guard let startedAt, let start = try? PlatformTimestamp(startedAt) else { return nil }
        if let previous, previous.pass == pass - 1, previous.startHz == startHz, previous.stopHz == stopHz, previous.stepHz == stepHz,
           let earlier = previous.startedAt, let t = try? PlatformTimestamp(earlier), start > t {
            return (Double(start.utcMicroseconds - t.utcMicroseconds) / 1e6, false)
        }
        guard measuredPoints > 1, let endedAt, let end = try? PlatformTimestamp(endedAt), end > start else { return nil }
        return (Double(end.utcMicroseconds - start.utcMicroseconds) / 1e6 / Double(measuredPoints) * Double(totalPoints), true)
    }
}

extension PlatformClient {
    private func receiverPanoramaPath(_ session: String, _ stream: String) -> String {
        "/sessions/\(Self.segment(session))/streams/\(Self.segment(stream))/receiver-panorama"
    }

    public func receiverPanorama(sessionID: String, streamID: String, pass: Int? = nil, columns: Int = 512, contextID: String? = nil) async throws -> PlatformReceiverPanorama {
        guard (2...1024).contains(columns), pass.map({ $0 > 0 }) != false else {
            throw PlatformError.invalidCommand("Нужны положительный номер прохода и 2–1024 колонки.")
        }
        var query = [URLQueryItem(name: "columns", value: String(columns))]
        if let pass { query.append(.init(name: "pass", value: String(pass))) }
        if let contextID { query.append(.init(name: "context_id", value: contextID)) }
        let result: PlatformReceiverPanorama = try await get(receiverPanoramaPath(sessionID, streamID), query: query)
        if let contextID, result.contextId != contextID {
            throw PlatformError.invalidResponse("Архив вернул другой контекст прохода AR-DV1.")
        }
        return try result.validated(requestedPass: pass, columns: columns)
    }

    public func receiverOriginal(sessionID: String, streamID: String, pass: Int, frequencyHz: Int64,
                                 observationID: String, fromEvent: Bool = false, contextID: String? = nil) async throws -> MonitoringValue {
        guard pass > 0, (100_000...1_300_000_000).contains(frequencyHz), !observationID.isEmpty else {
            throw PlatformError.invalidCommand("Нужна ссылка на исходное наблюдение в проходе.")
        }
        let row: MonitoringValue
        if fromEvent {
            // An event keeps its exact original even if another sample later became the bin peak.
            row = try await get("/sessions/\(Self.segment(sessionID))/observations/\(Self.segment(observationID))")
        } else {
            var query = [URLQueryItem(name: "pass", value: String(pass)), .init(name: "frequency_hz", value: String(frequencyHz))]
            if let contextID { query.append(.init(name: "context_id", value: contextID)) }
            row = try await get(receiverPanoramaPath(sessionID, streamID) + "/point", query: query)
        }
        guard PlatformExactID.same(row["session_id"].text, sessionID), PlatformExactID.same(row["stream_id"].text, streamID),
              PlatformExactID.same(row["id"].text, observationID), row["metadata"]["scan_pass"].integer == Int64(pass),
              row["frequency_hz"].integer == frequencyHz, row["level_unit"].text == "s_meter_count",
              row["level"].double?.isFinite == true, row["metadata"]["frequency_readback"].bool == true,
              row["metadata"]["frequency_source"].text == "receiver_RX",
              let a = try? PlatformTimestamp(row["interval"]["started_at"].text),
              let b = try? PlatformTimestamp(row["interval"]["ended_at"].text), a <= b else {
            throw PlatformError.invalidResponse("Ядро не подтвердило исходное наблюдение, частоту RX и проход AR-DV1.")
        }
        return row
    }

}
