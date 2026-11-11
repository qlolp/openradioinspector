import Foundation

public struct PlatformPlotPoint: Equatable, Sendable {
    public var x: Double
    public var y: Double
    public init(x: Double, y: Double) { self.x = x; self.y = y }
}

/// Rendering-only normalization; scientific levels and deltas are never changed.
public struct PlatformTraceVerticalScale: Sendable {
    private let magnitude: Double
    private let lower: Double
    private let upper: Double

    public init(minimum: Double, maximum: Double) {
        // Subtracting unscaled +/- Double.max would overflow despite finite input.
        magnitude = max(abs(minimum), abs(maximum), 1)
        var low = minimum / magnitude
        var high = maximum / magnitude
        if high <= low { low -= 1; high += 1 }
        let padding = (high - low) * 0.08
        lower = low - padding
        upper = high + padding
    }

    public func fraction(_ level: Double) -> Double {
        (level / magnitude - lower) / (upper - lower)
    }
}

public struct PlatformTraceVertex: Equatable, Sendable {
    public var position: SIMD2<Float>
    public var color: SIMD4<Float>
    public init(position: SIMD2<Float>, color: SIMD4<Float>) { self.position = position; self.color = color }
}
public struct PlatformTraceRequest: Equatable, Sendable {
    public let current: [PlatformPlotPoint], minimum: [PlatformPlotPoint], maximum: [PlatformPlotPoint]
    public let threshold: Double?
    public let scale: PlatformSpectrumScale?
    public let dark: Bool
    public let width: Double, height: Double
    public init(current: [PlatformPlotPoint], minimum: [PlatformPlotPoint], maximum: [PlatformPlotPoint], threshold: Double?, scale: PlatformSpectrumScale?, dark: Bool, width: Double, height: Double) {
        self.current = current; self.minimum = minimum; self.maximum = maximum; self.threshold = threshold
        self.scale = scale; self.dark = dark; self.width = width; self.height = height
    }
}
public struct PlatformTraceMesh: Equatable, Sendable {
    public struct Trace: Equatable, Sendable { public let vertices: [PlatformTraceVertex] }
    public let traces: [Trace]
    public let threshold: [PlatformTraceVertex]
    public init(_ input: PlatformTraceRequest) throws {
        try Task.checkCancellation()
        let samples = input.current + input.minimum + input.maximum
        guard !samples.isEmpty else { traces = []; threshold = []; return }
        var xMin = samples.map(\.x).min() ?? 0, xMax = samples.map(\.x).max() ?? 1
        var yMin = samples.map(\.y).min() ?? 0, yMax = samples.map(\.y).max() ?? 1
        if let level = input.threshold { yMin = min(yMin, level); yMax = max(yMax, level) }
        if xMax <= xMin { xMin -= 1; xMax += 1 }
        let vertical = PlatformTraceVerticalScale(minimum: yMin, maximum: yMax)
        let width = max(input.width, 1), height = max(input.height, 1)
        func vertex(_ point: PlatformPlotPoint, _ color: SIMD4<Float>) -> PlatformTraceVertex {
            let x = 8 + (point.x - xMin) / (xMax - xMin) * max(width - 16, 1)
            let y = 8 + (input.scale?.fraction(point.y) ?? vertical.fraction(point.y)) * max(height - 16, 1)
            return PlatformTraceVertex(position: SIMD2(Float(x / width * 2 - 1), Float(y / height * 2 - 1)), color: color)
        }
        let primary: SIMD4<Float> = input.dark ? SIMD4(1, 1, 1, 1) : SIMD4(0, 0, 0, 1)
        traces = [Trace(vertices: input.minimum.map { vertex($0, SIMD4(0, 0.48, 1, 1)) }),
                  Trace(vertices: input.maximum.map { vertex($0, SIMD4(1, 0.58, 0, 1)) }),
                  Trace(vertices: input.current.map { vertex($0, primary) })]
        var dashes: [PlatformTraceVertex] = []
        if let level = input.threshold {
            let left = vertex(PlatformPlotPoint(x: xMin, y: level), SIMD4(1, 0.23, 0.19, 1)), right = vertex(PlatformPlotPoint(x: xMax, y: level), SIMD4(1, 0.23, 0.19, 1))
            let length = max(width - 16, 1)
            for x in stride(from: 0.0, to: length, by: 9) {
                var a = left, b = right
                a.position.x = left.position.x + Float(x / length) * (right.position.x - left.position.x)
                b.position.x = left.position.x + Float(min(x + 5, length) / length) * (right.position.x - left.position.x)
                dashes += [a, b]
            }
        }
        threshold = dashes
    }
}
