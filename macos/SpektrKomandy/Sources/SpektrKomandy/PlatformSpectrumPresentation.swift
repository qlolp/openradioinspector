import Foundation

/// Rendering-only bounds. Never rewrites source measurements.
public struct PlatformSpectrumScale: Equatable {
    public let lower: Double
    public let upper: Double
    public init?(lower: Double, upper: Double) {
        guard lower.isFinite, upper.isFinite, lower < upper else { return nil }
        self.lower = lower; self.upper = upper
    }
    public func fraction(_ value: Double) -> Double {
        if value <= lower { return 0 }; if value >= upper { return 1 }
        let magnitude = max(abs(lower), abs(upper), Double.leastNormalMagnitude)
        return min(1, max(0, (value / magnitude - lower / magnitude) / (upper / magnitude - lower / magnitude)))
    }
    public static func fit(_ levels: [Double]) -> Self? {
        guard !levels.isEmpty, levels.allSatisfy(\.isFinite), let low = levels.min(), let high = levels.max() else { return nil }
        let magnitude = max(abs(low), abs(high), 1)
        var padding = max((high / magnitude - low / magnitude) * 0.08, 1 / magnitude)
        func endpoint(_ normalized: Double) -> Double { min(1, max(-1, normalized)) * Double.greatestFiniteMagnitude }
        func bounds() -> (Double, Double) {
            let ratio = magnitude / Double.greatestFiniteMagnitude
            return (endpoint((low / magnitude - padding) * ratio), endpoint((high / magnitude + padding) * ratio))
        }
        var (lower, upper) = bounds()
        if lower >= upper { padding = max(abs(low / magnitude) * 0.08, 1 / magnitude); (lower, upper) = bounds() }
        return Self(lower: lower, upper: upper)
    }
}

/// A bounded, local display snapshot, independent of acquisition and transport.
public struct PlatformSpectrumPresentation {
    public struct Scope: Equatable {
        public let server: String
        public let session: String
        public let stream: String
        public init(server: String, session: String, stream: String) { self.server = server; self.session = session; self.stream = stream }
        public static func == (a: Self, b: Self) -> Bool {
            PlatformSpectrumPresentation.same(a.server, b.server) && PlatformSpectrumPresentation.same(a.session, b.session) && PlatformSpectrumPresentation.same(a.stream, b.stream)
        }
    }
    public static let capacity = 40
    public private(set) var sweeps: [PlatformSweep] = []
    public private(set) var scale: PlatformSpectrumScale?
    public private(set) var paused = false
    public private(set) var error: String?
    private var scope: Scope?
    private var newest: [PlatformSweep] = []
    public init() {}
    fileprivate static func same(_ a: String, _ b: String) -> Bool { a.utf8.elementsEqual(b.utf8) }
    // Validate Gregorian UTC without Date rounding or calendar normalization.
    private static func validInterval(_ interval: PlatformInterval) -> Bool {
        func instant(_ text: String) -> String? {
            let pattern = #"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)\z"#
            guard text.utf8.allSatisfy({ $0 < 128 }), text.range(of: pattern, options: .regularExpression) != nil else { return nil }
            let bytes = Array(text.utf8)
            func number(_ start: Int, _ end: Int) -> Int { Int(String(decoding: bytes[start..<end], as: UTF8.self))! }
            let year = number(0, 4), month = number(5, 7), day = number(8, 10)
            guard year > 0, (1...12).contains(month), number(11, 13) < 24, number(14, 16) < 60, number(17, 19) < 60 else { return nil }
            let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
            let days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            guard (1...days[month - 1]).contains(day) else { return nil }
            let body = text.hasSuffix("Z") ? String(text.dropLast()) : String(text.dropLast(6))
            let parts = body.split(separator: ".")
            let fraction = parts.count == 2 ? String(parts[1]) : ""
            return String(parts[0]) + "." + fraction + String(repeating: "0", count: 6 - fraction.count)
        }
        guard let start = instant(interval.startedAt), let end = instant(interval.endedAt) else { return false }
        return start <= end
    }
    public var clippedCount: Int { guard let scale else { return 0 }; return sweeps.flatMap(\.levels).filter { $0 < scale.lower || $0 > scale.upper }.count }
    public mutating func reset() { self = Self() }
    public mutating func update(scope: Scope, sweeps incoming: [PlatformSweep]) {
        guard !scope.server.isEmpty, !scope.session.isEmpty, !scope.stream.isEmpty,
              !incoming.isEmpty, incoming.allSatisfy({ sweep in
            !sweep.id.isEmpty
                && Self.same(sweep.sessionId, scope.session)
                && Self.same(sweep.streamId, scope.stream)
                && Self.same(sweep.levelUnit, "dBm")
                && !sweep.frequenciesHz.isEmpty
                && sweep.frequenciesHz.count == sweep.levels.count
                && sweep.levels.allSatisfy(\.isFinite)
                && Self.validInterval(sweep.interval)
                && sweep.frequenciesHz.allSatisfy { $0 >= 0 && $0 <= 9_007_199_254_740_991 }
                && zip(sweep.frequenciesHz, sweep.frequenciesHz.dropFirst()).allSatisfy { $0 <= $1 }
        }), let last = incoming.last else { reset(); return }
        let suffix = Array(incoming.reversed().prefix { $0.frequenciesHz == last.frequenciesHz }.prefix(Self.capacity).reversed())
        if self.scope != scope || sweeps.last?.frequenciesHz != last.frequenciesHz { reset() }
        self.scope = scope; newest = suffix
        if !paused { sweeps = suffix }
        if scale == nil { fit() }
    }
    public mutating func togglePause() {
        guard !sweeps.isEmpty else { return }
        paused.toggle()
        if !paused { sweeps = newest }
    }
    public mutating func fit() { scale = PlatformSpectrumScale.fit(sweeps.flatMap(\.levels)); error = nil }
    public mutating func setBounds(lower: String, upper: String) {
        guard lower.utf8.allSatisfy({ $0 < 128 }), upper.utf8.allSatisfy({ $0 < 128 }) else {
            error = "Нужны конечные границы: минимум < максимум"; return
        }
        func trimASCII(_ text: String) -> String {
            var bytes = ArraySlice(text.utf8)
            func whitespace(_ byte: UInt8) -> Bool { byte == 32 || (9...13).contains(byte) }
            while let first = bytes.first, whitespace(first) { bytes.removeFirst() }
            while let last = bytes.last, whitespace(last) { bytes.removeLast() }
            return String(decoding: bytes, as: UTF8.self)
        }
        let lower = trimASCII(lower), upper = trimASCII(upper)
        let pattern = #"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\z"#
        guard lower.utf8.allSatisfy({ $0 < 128 }), upper.utf8.allSatisfy({ $0 < 128 }), lower.range(of: pattern, options: .regularExpression) != nil, upper.range(of: pattern, options: .regularExpression) != nil, !sweeps.isEmpty, let low = Double(lower), let high = Double(upper), let value = PlatformSpectrumScale(lower: low, upper: high) else {
            error = "Нужны конечные границы: минимум < максимум"; return
        }
        scale = value; error = nil
    }
}
