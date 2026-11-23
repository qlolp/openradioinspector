import Foundation
import ORIContracts

public typealias PlatformRFExplorerBandPower = ORIContracts.RFExplorerBandPowerResult
public typealias PlatformSweepTolerance = ORIContracts.SweepToleranceResult

private extension ORIContracts.Sweep {
    func matches(_ source: PlatformSweep) -> Bool {
        PlatformExactID.same(id, source.id) && PlatformExactID.same(session_id, source.sessionId)
            && PlatformExactID.same(stream_id, source.streamId) && level_unit == source.levelUnit
            && frequencies_hz.elementsEqual(source.frequenciesHz, by: { $0 == $1 })
            && levels == source.levels
            && interval.started_at.unixMicros == (try? PlatformTimestamp(source.interval.startedAt))?.utcMicroseconds
            && interval.ended_at.unixMicros == (try? PlatformTimestamp(source.interval.endedAt))?.utcMicroseconds
    }
}

extension PlatformClient {
    private func archivedAnalysis<Result: ContractModel, Request: Encodable>(
        _ path: String, _ request: Request, as: Result.Type
    ) async throws -> Result {
        var verified: Result?
        // The strict contract reader preserves literal settings keys and distinguishes null from a missing field.
        let _: EmptyAnalysisEnvelopeData = try await post(path, body: request, expectedStatuses: [200], validateResponse: { bytes in
            let payload = try StrictJSON.parse(bytes).requireObject()
            verified = try Result(json: payload["data"] ?? .null)
        })
        try Task.checkCancellation()
        guard let verified else { throw PlatformError.invalidResponse("Нет исходных данных анализа.") }
        return verified
    }

    public func rfExplorerBandPower(source: PlatformSweep, startHz: Int64, stopHz: Int64) async throws -> PlatformRFExplorerBandPower {
        guard startHz < stopHz, source.frequenciesHz.contains(startHz), source.frequenciesHz.contains(stopHz) else {
            throw PlatformError.invalidCommand("Выберите две разные измеренные частоты в одном исходном свипе.")
        }
        let request = RFExplorerBandPowerRequest(start_hz: UInt64(startHz), stop_hz: UInt64(stopHz), sweep_id: source.id)
        let result = try await archivedAnalysis("/sessions/\(Self.segment(source.sessionId))/rf-explorer-band-power", request,
                                               as: PlatformRFExplorerBandPower.self)
        guard result.source.matches(source), result.start_hz == startHz, result.stop_hz == stopHz,
              result.start_index < result.stop_index, result.stop_index < source.frequenciesHz.count,
              source.frequenciesHz[Int(result.start_index)] == startHz, source.frequenciesHz[Int(result.stop_index)] == stopHz,
              case .number(let recordedRBW) = source.settings?["rbw_hz"], recordedRBW > 0,
              UInt64(exactly: recordedRBW) == result.rbw_hz, result.value.isFinite else {
            throw PlatformError.invalidResponse("Мощность полосы не соответствует исходному свипу, границам или записанной RBW.")
        }
        return result
    }

    public func sweepTolerance(reference: PlatformSweep, target: PlatformSweep, upperDb: Double, lowerDb: Double) async throws -> PlatformSweepTolerance {
        guard PlatformExactID.same(reference.sessionId, target.sessionId), PlatformExactID.same(reference.streamId, target.streamId),
              [upperDb, lowerDb].allSatisfy({ $0.isFinite && $0 >= 0 }) else {
            throw PlatformError.invalidCommand("Для допуска нужны два свипа одного потока и конечные неотрицательные отклонения в дБ.")
        }
        let request = SweepToleranceRequest(lower_deviation_db: lowerDb, reference_id: reference.id,
                                            target_id: target.id, upper_deviation_db: upperDb)
        let result = try await archivedAnalysis("/sessions/\(Self.segment(target.sessionId))/sweep-tolerance-envelope", request,
                                               as: PlatformSweepTolerance.self)
        guard PlatformExactID.same(result.session_id, target.sessionId), PlatformExactID.same(result.stream_id, target.streamId),
              result.reference.matches(reference), result.target.matches(target), result.upper_deviation_db == upperDb,
              result.lower_deviation_db == lowerDb, result.bins.count == target.levels.count else {
            throw PlatformError.invalidResponse("Пределы допуска не соответствуют выбранным свипам или настройкам.")
        }
        var totals: [String: UInt64] = [:]
        for (index, bin) in result.bins.enumerated() {
            guard bin.target_index == index, bin.frequency_hz == target.frequenciesHz[index] else {
                throw PlatformError.invalidResponse("Отсчёт допуска отсутствует на исходной сетке.")
            }
            if let ref = bin.reference_index {
                guard ref < reference.frequenciesHz.count, reference.frequenciesHz[Int(ref)] == bin.frequency_hz,
                      bin.classification != .uncovered, bin.lower_dbm != nil, bin.upper_dbm != nil, bin.delta_db != nil else {
                    throw PlatformError.invalidResponse("Неверный опорный отсчёт допуска.")
                }
            } else if bin.classification != .uncovered || bin.lower_dbm != nil || bin.upper_dbm != nil || bin.delta_db != nil {
                throw PlatformError.invalidResponse("В области без опорных данных указан предел допуска.")
            }
            totals[bin.classification.rawValue, default: 0] += 1
        }
        let counts = result.counts
        guard counts.above == totals["above", default: 0], counts.below == totals["below", default: 0],
              counts.within == totals["within", default: 0], counts.uncovered == totals["uncovered", default: 0] else {
            throw PlatformError.invalidResponse("Количество отсчётов допуска не соответствует исходным индексам.")
        }
        return result
    }
}

private struct EmptyAnalysisEnvelopeData: Decodable {}
