import Foundation
import SpektrKomandy

/// Presentation-only inspection of saved bins. Never interpolates measured levels or per-bin time.
struct NativeSpectrumSample: Equatable {
    let sweep: PlatformSweep
    let bin: Int
    var frequency: Int64 { sweep.frequenciesHz[bin] }
    var level: Double { sweep.levels[bin] }
}

struct NativeSpectrumTimeline {
    struct Row {
        let sweep: PlatformSweep
        let start: Date
        let end: Date
    }
    let rows: [Row]
    let start: Date
    let end: Date
    var duration: Double { max(0.001, end.timeIntervalSince(start)) }

    init(_ sweeps: [PlatformSweep]) {
        rows = sweeps.compactMap { sweep in
            guard let start = Self.date(sweep.interval.startedAt), let end = Self.date(sweep.interval.endedAt), end >= start else { return nil }
            return Row(sweep: sweep, start: start, end: end)
        }.sorted { $0.end < $1.end }
        start = rows.map(\.start).min() ?? .distantPast
        end = rows.map(\.end).max() ?? .distantPast
    }

    func fraction(_ date: Date) -> Double { end.timeIntervalSince(date) / duration }
    func row(at fraction: Double) -> Row? {
        guard (0...1).contains(fraction) else { return nil }
        let date = end.addingTimeInterval(-fraction * duration)
        // A point in an acquisition gap has no measurement. Newest wins for overlapping intervals.
        return rows.reversed().first { date >= $0.start && date <= $0.end }
    }
    static func date(_ value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
    static func utc(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter.string(from: date)
    }
}

enum NativeSpectrumInspection {
    static func nearestBin(_ frequency: Double, in sweep: PlatformSweep) -> Int? {
        guard frequency.isFinite, !sweep.frequenciesHz.isEmpty else { return nil }
        return sweep.frequenciesHz.indices.min { abs(Double(sweep.frequenciesHz[$0]) - frequency) < abs(Double(sweep.frequenciesHz[$1]) - frequency) }
    }
    static func peaks(in sweep: PlatformSweep, range: ClosedRange<Double>, limit: Int = 3) -> [Int] {
        let values = sweep.levels
        let candidates = values.indices.filter { i in
            range.contains(Double(sweep.frequenciesHz[i])) &&
            (i == 0 || values[i] > values[i - 1]) &&
            (i == values.count - 1 || values[i] >= values[i + 1])
        }.sorted { values[$0] == values[$1] ? $0 < $1 : values[$0] > values[$1] }
        var selected: [Int] = []
        for i in candidates where selected.allSatisfy({ abs($0 - i) >= 3 }) {
            if selected.count >= limit { break }
            selected.append(i)
        }
        return selected
    }
    static func frequency(_ hz: Double) -> String { String(format: "%.3f МГц", hz / 1_000_000) }
    static func interval(_ sweep: PlatformSweep) -> String {
        guard let start = NativeSpectrumTimeline.date(sweep.interval.startedAt), let end = NativeSpectrumTimeline.date(sweep.interval.endedAt) else { return "Время неизвестно" }
        let day = String(sweep.interval.startedAt.prefix(10))
        let endDay = String(sweep.interval.endedAt.prefix(10))
        return "\(day) \(NativeSpectrumTimeline.utc(start))–\(day == endDay ? "" : endDay + " ")\(NativeSpectrumTimeline.utc(end)) UTC"
    }
    static func delta(_ a: NativeSpectrumSample, _ b: NativeSpectrumSample) -> String {
        let dt = NativeSpectrumTimeline.date(b.sweep.interval.endedAt).flatMap { end in
            NativeSpectrumTimeline.date(a.sweep.interval.endedAt).map { end.timeIntervalSince($0) }
        }
        return String(format: "Δf %+.3f кГц · ΔL %+.1f дБ", Double(b.frequency - a.frequency) / 1000, b.level - a.level)
            + (dt.map { String(format: " · Δt %+.3f с (концы свипов)", $0) } ?? "")
    }
}
