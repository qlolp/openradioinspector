import Foundation

/// Retain nearby frequency pixels while panning. Zooming in always requests
/// finer detail; padding never reduces the pixel density of the visible range.
struct NativeSpectrumRasterWindow: Equatable, Sendable {
    let range: ClosedRange<Double>
    let pixelWidth: Int
    private let fullRange: ClosedRange<Double>
    private let visibleSpan: Double

    static func covering(_ visible: ClosedRange<Double>, within full: ClosedRange<Double>,
                         previous: Self? = nil) -> Self? {
        let span = visible.upperBound - visible.lowerBound
        guard span.isFinite, span > 0, full.lowerBound.isFinite, full.upperBound.isFinite,
              full.lowerBound < full.upperBound, full.contains(visible.lowerBound),
              full.contains(visible.upperBound) else { return nil }
        if let previous, previous.fullRange == full,
           span >= previous.visibleSpan * (1 - 1e-12),
           previous.range.contains(visible.lowerBound), previous.range.contains(visible.upperBound) {
            return previous
        }
        let lower = max(full.lowerBound, visible.lowerBound - span)
        let upper = min(full.upperBound, visible.upperBound + span)
        let ratio = min(3, max(1, (upper - lower) / span))
        return Self(range: lower...upper, pixelWidth: min(12_288, Int(ceil(4096 * ratio))),
                    fullRange: full, visibleSpan: span)
    }
}
