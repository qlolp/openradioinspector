import Foundation
import SpektrKomandy

/// An operator estimate, never an archive-budget or baseline-validity override.
struct NativeBaselineProgress {
    let preflight: MonitoringValue
    var sweeps: Int64? {
        guard let count = preflight["baseline_collection"]["sweeps"].integer, count >= 0 else { return nil }
        return count
    }
    var elapsedSeconds: Double? {
        valid(preflight["baseline_collection"]["elapsed_seconds"].double, positive: false)
    }
    var sweepFraction: Double { min(1, Double(sweeps ?? 0) / 20) }
    var timeFraction: Double { min(1, (elapsedSeconds ?? 0) / 60) }
    var ready: Bool {
        preflight["baseline_collection"]["ready"].bool == true && (sweeps ?? 0) >= 20 && (elapsedSeconds ?? 0) >= 60
    }
    var estimatedRemainingSeconds: Double? {
        guard let sweeps, let elapsedSeconds else { return nil }
        let time = max(0, 60 - elapsedSeconds)
        if sweeps >= 20 { return time > 0 || ready ? time : nil }
        let performance = preflight["performance"]
        guard let period = valid(performance["mean_observed_cycle_seconds"].double, positive: true)
                ?? valid(performance["mean_sweep_seconds"].double, positive: true) else { return nil }
        let estimate = max(time, Double(20 - sweeps) * period)
        return estimate.isFinite ? estimate : nil
    }
    private func valid(_ value: Double?, positive: Bool) -> Double? {
        guard let value, value.isFinite, positive ? value > 0 : value >= 0 else { return nil }
        return value
    }
}
