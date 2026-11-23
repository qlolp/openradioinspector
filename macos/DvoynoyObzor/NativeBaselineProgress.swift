import Foundation
import SpektrKomandy

/// An operator estimate, never an archive-budget or baseline-validity override.
struct NativeBaselineProgress {
    let preflight: MonitoringValue
    private var collection: MonitoringValue { preflight["baseline_collection"] }
    var bands: [MonitoringValue] { collection["bands"].items }
    var eligibleBands: [MonitoringValue] { bands.filter { ($0["eligible_windows"].integer ?? 0) > 0 } }
    var coverage: (ready: Int64, total: Int64, label: String)? {
        if !bands.isEmpty {
            let total = eligibleBands.compactMap { $0["eligible_windows"].integer }.reduce(0, +)
            let ready = eligibleBands.compactMap { $0["ready_windows"].integer }.reduce(0, +)
            return (ready, total, "Готовые окна для эталона")
        }
        for (count, ready, label) in [("points", "ready_points", "Точки с эталоном"), ("windows", "ready_windows", "Окна с эталоном")] {
            if let total = collection[count].integer, let n = collection[ready].integer, total > 0, (0...total).contains(n) {
                return (n, total, label)
            }
        }
        return nil
    }
    var usesPasses: Bool { collection["passes"].integer != nil }
    var minimum: Int64 { max(1, collection[usesPasses ? "minimum_passes" : "minimum_sweeps"].integer ?? 20) }
    var minimumSeconds: Double { max(1, collection["minimum_seconds"].double ?? 60) }
    var sweeps: Int64? {
        if !bands.isEmpty { return eligibleBands.map { $0["sweeps"].integer ?? 0 }.min() }
        guard let count = collection[usesPasses ? "passes" : "sweeps"].integer, count >= 0 else { return nil }
        return count
    }
    var elapsedSeconds: Double? {
        if !bands.isEmpty { return eligibleBands.map { valid($0["elapsed_seconds"].double, positive: false) ?? 0 }.min() }
        return valid(collection["elapsed_seconds"].double, positive: false)
    }
    var sweepFraction: Double { min(1, Double(sweeps ?? 0) / Double(minimum)) }
    var timeFraction: Double { min(1, (elapsedSeconds ?? 0) / minimumSeconds) }
    var ready: Bool {
        if !bands.isEmpty {
            return collection["ready"].bool == true && !eligibleBands.isEmpty && eligibleBands.allSatisfy {
                !$0["band_id"].text.isEmpty && $0["ready"].bool == true &&
                $0["ready_windows"].integer == $0["eligible_windows"].integer &&
                ($0["sweeps"].integer ?? 0) >= minimum && ($0["elapsed_seconds"].double ?? 0) >= minimumSeconds
            }
        }
        return collection["ready"].bool == true && (sweeps ?? 0) >= minimum && (elapsedSeconds ?? 0) >= minimumSeconds && (coverage.map { $0.ready == $0.total } ?? true)
    }
    var estimatedRemainingSeconds: Double? {
        guard let sweeps, let elapsedSeconds else { return nil }
        let time = max(0, minimumSeconds - elapsedSeconds)
        if sweeps >= minimum { return time > 0 || ready ? time : nil }
        if usesPasses || !bands.isEmpty { return nil }
        let performance = preflight["performance"]
        guard let period = valid(performance["mean_observed_cycle_seconds"].double, positive: true)
                ?? valid(performance["mean_sweep_seconds"].double, positive: true) else { return nil }
        let estimate = max(time, Double(minimum - sweeps) * period)
        return estimate.isFinite ? estimate : nil
    }
    private func valid(_ value: Double?, positive: Bool) -> Double? {
        guard let value, value.isFinite, positive ? value > 0 : value >= 0 else { return nil }
        return value
    }
}
