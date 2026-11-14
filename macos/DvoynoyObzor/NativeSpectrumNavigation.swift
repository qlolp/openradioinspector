import Foundation

/// Display-only navigation. It never changes the instrument's sweep configuration.
enum NativeSpectrumNavigation {
    static func fitted(center: Double, width: Double, within full: ClosedRange<Double>) -> ClosedRange<Double> {
        let extent = full.upperBound - full.lowerBound
        let width = min(extent, max(extent / 1024, width.isFinite ? width : extent))
        let center = center.isFinite ? center : (full.lowerBound + full.upperBound) / 2
        let low = max(full.lowerBound, min(full.upperBound - width, center - width / 2))
        return low...(low + width)
    }
    static func zoom(_ range: ClosedRange<Double>, factor: Double, anchor: Double, within full: ClosedRange<Double>) -> ClosedRange<Double> {
        guard factor.isFinite, factor > 0 else { return range }
        let fraction = min(1, max(0, (anchor - range.lowerBound) / (range.upperBound - range.lowerBound)))
        let width = min(full.upperBound - full.lowerBound, max((full.upperBound - full.lowerBound) / 1024, (range.upperBound - range.lowerBound) * factor))
        return fitted(center: anchor + (0.5 - fraction) * width, width: width, within: full)
    }
}

struct NativeAnalyzerDraft {
    var start = "88", stop = "108", center = "98", span = "20"
    var rbw = "10000"
    enum Field { case start, stop, center, span }
    static func hz(_ text: String) throws -> Int64 {
        guard let value = Double(text.replacingOccurrences(of: ",", with: ".")), value.isFinite,
              value >= 0, value <= 800, abs(value * 1_000_000 - (value * 1_000_000).rounded()) < 0.001 else {
            throw DraftError.invalid("Частота: 0…800 МГц, точность до 1 Гц.")
        }
        return Int64((value * 1_000_000).rounded())
    }
    mutating func synchronize(_ field: Field) throws {
        let low: Int64, high: Int64
        switch field {
        case .start, .stop: low = try Self.hz(start); high = try Self.hz(stop)
        case .center, .span:
            let middle = try Self.hz(center), width = try Self.hz(span)
            low = middle - width / 2; high = low + width
        }
        guard low >= 100_000, high <= 800_000_000, high > low else {
            throw DraftError.invalid("Нужны Start < Stop в пределах 0.1…800 МГц. Zero span здесь не используется.")
        }
        start = Self.mhz(low); stop = Self.mhz(high)
        center = String(format: "%.6f", Double(low + high) / 2_000_000)
        span = Self.mhz(high - low)
    }
    static func mhz(_ hz: Int64) -> String { String(format: "%.6f", Double(hz) / 1_000_000) }
    enum DraftError: LocalizedError {
        case invalid(String)
        var errorDescription: String? { switch self { case .invalid(let text): text } }
    }
}
