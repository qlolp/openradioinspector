import Foundation
import SpektrKomandy

enum Completion: Error { case sentinel }
enum ProofError: Error { case invalid(String) }
struct Proof: Codable, Sendable {
    let sequences: [String]
    let exact_utf8_ids: [[UInt8]]
    let preceding_acks_proven_by_sentinel: Bool
}
actor Recorder {
    var ids: [[UInt8]] = []
    var sequences: [String] = []
    let expectedIDs: [[UInt8]], bits: [String], sentinel: UInt64
    let startedAt: String, endedAt: String, unit: String
    init(ids: [[UInt8]], bits: [String], sentinel: UInt64, interval: [String: String], unit: String) {
        expectedIDs = ids; self.bits = bits; self.sentinel = sentinel
        startedAt = interval["started_at"]!; endedAt = interval["ended_at"]!; self.unit = unit
    }
    func accept(_ event: PlatformMeasurementEvent) throws {
        if event.sequence == sentinel { throw Completion.sentinel }
        guard ids.count < expectedIDs.count, let source = event.committedEvent?.payload.sweep,
              source.id.utf8.map({ $0 }) == expectedIDs[ids.count],
              source.levels.map({ String(format: "%016llx", $0.bitPattern) }) == bits,
              source.frequenciesHz == [100000000, 100000001, 100000002],
              source.interval.startedAt == startedAt, source.interval.endedAt == endedAt,
              source.levelUnit == unit,
              event.displayIssue == nil else { throw ProofError.invalid("Exact source mismatch") }
        ids.append(Array(source.id.utf8)); sequences.append(String(event.sequence))
    }
    func report() -> Proof { Proof(sequences: sequences, exact_utf8_ids: ids, preceding_acks_proven_by_sentinel: true) }
}

@main struct Interop {
    static func main() async throws {
        let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
        let raw = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let url = raw["url"] as! String
        let after = UInt64(raw["after"] as! String)!
        let sweeps = raw["expected_sweeps"] as! [[String: Any]]
        let recorder = Recorder(ids: sweeps.map { Array(($0["id"] as! String).utf8) },
                                bits: raw["level_bits"] as! [String], sentinel: after + UInt64(sweeps.count) + 1,
                                interval: sweeps[0]["interval"] as! [String: String], unit: sweeps[0]["level_unit"] as! String)
        let client = try PlatformClient(baseURL: url)
        guard try await client.measurementTransport() == .binaryV1 else { throw ProofError.invalid("Production binary negotiation missing") }
        do { try await client.consumeMeasurements(after: after, using: .binaryV1) { try await recorder.accept($0) } }
        catch Completion.sentinel {}
        let report = await recorder.report()
        guard report.sequences.count == sweeps.count else { throw ProofError.invalid("Incomplete committed replay") }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        let output = try encoder.encode(report)
        print(String(decoding: output, as: UTF8.self))
    }
}
