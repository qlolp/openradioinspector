import Foundation

/// Grid of separate FM receiver observations, including a possibly shorter final step.
public struct PlatformARDV1ScopeConfiguration: Equatable, Sendable {
    public let startHz: Int
    public let stopHz: Int
    public let stepHz: Int

    public let cyclic: Bool

    public init(startHz: Int, stopHz: Int, stepHz: Int, cyclic: Bool = false) throws {
        let receiverRange = 100_000...1_300_000_000
        guard receiverRange.contains(startHz), receiverRange.contains(stopHz),
              startHz < stopHz, (10...1_300_000_000).contains(stepHz),
              startHz.isMultiple(of: 10), stopHz.isMultiple(of: 10), stepHz.isMultiple(of: 10) else {
            throw PlatformError.invalidCommand("Задайте диапазон 0,1–1300 МГц и положительный шаг. Частоты должны быть кратны 10 Гц.")
        }
        self.startHz = startHz
        self.stopHz = stopHz
        self.stepHz = stepHz
        self.cyclic = cyclic
        let limit = cyclic ? 129_991 : 1000
        guard pointCount <= limit else {
            throw PlatformError.invalidCommand("Увеличьте шаг или сузьте диапазон до \(limit) точек.")
        }
    }

    public var pointCount: Int { (stopHz - startHz + stepHz - 1) / stepHz + 1 }

    public func frequency(at index: Int) -> Int64? {
        guard (0..<pointCount).contains(index) else { return nil }
        return Int64(min(startHz + index * stepHz, stopHz))
    }
}

public struct PlatformARDV1ScopePoint: Decodable, Identifiable, Sendable {
    public let observation: PlatformObservation
    public let index: Int
    public let level: Double
    public let tuneCommand: String
    public var id: PlatformExactID { PlatformExactID(observation.id) }
    let total: Int

    public init(from decoder: Decoder) throws {
        struct Metadata: Decodable {
            let scanIndex, scanTotal: Int
            let scanTuneCommand, scopeSource, frequencySource: String
            let frequencyReadback, simulated: Bool
        }
        enum Keys: String, CodingKey { case metadata }
        observation = try PlatformObservation(from: decoder)
        let metadata = try decoder.container(keyedBy: Keys.self).decode(Metadata.self, forKey: .metadata)
        guard metadata.frequencyReadback, !metadata.simulated,
              metadata.scopeSource == "sequential_receiver_readback",
              metadata.frequencySource == "receiver_RX",
              ["RF", "ZK"].contains(metadata.scanTuneCommand),
              observation.levelUnit == "s_meter_count", observation.mode == "FM",
              let level = observation.level, level.isFinite else {
            throw PlatformError.invalidResponse("Архив вернул измерение без подтверждения частоты AR-DV1.")
        }
        index = metadata.scanIndex
        total = metadata.scanTotal
        self.level = level
        tuneCommand = metadata.scanTuneCommand
    }
}

extension PlatformClient {
    public func supportsARDV1Scope() async throws -> Bool {
        struct Capabilities: Decodable {
            struct Features: Decodable { let ardv1SampledScope: Bool? }
            let features: Features
        }
        let capabilities: Capabilities = try await get("/capabilities")
        return capabilities.features.ardv1SampledScope == true
    }

    public func ardv1ScopeWorker(sessionID: String, streamID: String) async throws -> PlatformWorker {
        let workers: [PlatformWorker] = try await get("/sessions/\(Self.segment(sessionID))/workers")
        guard let worker = workers.first(where: { PlatformExactID.same($0.streamId, streamID) }) else {
            throw PlatformError.invalidResponse("Ядро не вернуло состояние прохода AR-DV1.")
        }
        return worker
    }

    public func ardv1ScopePoints(
        sessionID: String, streamID: String, configuration: PlatformARDV1ScopeConfiguration
    ) async throws -> [PlatformARDV1ScopePoint] {
        let points: [PlatformARDV1ScopePoint] = try await get(
            "/sessions/\(Self.segment(sessionID))/observations",
            query: [
                URLQueryItem(name: "stream_id", value: streamID),
                URLQueryItem(name: "limit", value: String(configuration.pointCount)),
            ]
        )
        let sorted = points.sorted { $0.index < $1.index }
        guard points.count <= configuration.pointCount, Set(points.map(\.id)).count == points.count,
              sorted.enumerated().allSatisfy({ index, point in
                  index == point.index && point.total == configuration.pointCount
                      && configuration.frequency(at: index) == point.observation.frequencyHz
                      && PlatformExactID.same(point.observation.sessionId, sessionID)
                      && PlatformExactID.same(point.observation.streamId, streamID)
              }) else {
            throw PlatformError.invalidResponse("Архив вернул чужие, повторные или отсутствующие точки прохода.")
        }
        return sorted
    }
}
