import Foundation

/// The core resolves the immutable sources and calculates the two outcomes independently.
public struct PlatformPairedRechecks: Decodable, Sendable {
    public struct Visit: Decodable, Sendable {
        public let sweepId: String
        public let observationIds: [String]
        public let tinyDeltaDb: Double?
        public let arDropCounts: Double?
        public let signalRepeated: Bool?
        public let discrepancyRepeated: Bool?
    }

    public struct Background: Decodable, Sendable {
        public let tinyDbm: Double
        public let arSMeterCount: Double
        public let tinyRobustSigmaDb: Double
        public let arRobustSigmaCounts: Double
    }

    public let localBackground: Background?
    public let projectId: String?
    public let eventId: String?
    public let state: String
    public let methodology: String?
    public let instrument: String?
    public let frequencyHz: Int64?
    public let visits: [Visit]
    public let signalRepeats: Int?
    public let discrepancyRepeats: Int?
    public let repeatTotal: Int?
    public let simulated: Bool?
}

extension PlatformClient {
    public func monitoringPairedRechecks(projectID: String, eventID: String,
                                         recipe: MonitoringValue? = nil) async throws -> PlatformPairedRechecks {
        guard !projectID.isEmpty, !eventID.isEmpty else {
            throw PlatformError.invalidCommand("Выберите проект и событие охраны")
        }
        let path = "/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))/paired-rechecks"
        let result: PlatformPairedRechecks
        if let recipe { result = try await post(path, body: recipe) }
        else { result = try await get(path) }
        if let project = result.projectId, !PlatformExactID.same(project, projectID) {
            throw PlatformError.invalidResponse("Ядро вернуло другой проект")
        }
        if let event = result.eventId, !PlatformExactID.same(event, eventID) {
            throw PlatformError.invalidResponse("Ядро вернуло другое событие")
        }
        switch result.state {
        case "missing_evidence" where result.visits.isEmpty: return result
        case "evaluated":
            guard result.projectId != nil, result.eventId != nil,
                  result.methodology == "paired-local-recheck-v1",
                  let instrument = result.instrument, ["tinySA", "AR-DV1"].contains(instrument),
                  let frequency = result.frequencyHz, frequency > 0, result.simulated != nil,
                  let total = result.repeatTotal, (1...2).contains(total),
                  let signals = result.signalRepeats, (0...total).contains(signals),
                  let discrepancies = result.discrepancyRepeats, (0...total).contains(discrepancies),
                  result.visits.count == total, result.visits.allSatisfy({
                      !$0.sweepId.isEmpty && !$0.observationIds.isEmpty && $0.observationIds.allSatisfy { !$0.isEmpty } &&
                          $0.tinyDeltaDb?.isFinite == true && $0.arDropCounts?.isFinite == true &&
                          $0.signalRepeated != nil && $0.discrepancyRepeated != nil
                  }), result.visits.filter({ $0.signalRepeated == true }).count == signals,
                  result.visits.filter({ $0.discrepancyRepeated == true }).count == discrepancies
            else { break }
            return result
        default: break
        }
        throw PlatformError.invalidResponse("Ядро вернуло неполные данные повторной проверки")
    }
}
