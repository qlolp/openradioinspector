import Foundation

public struct LevelBin: Equatable, Sendable {
    public var frequency: Int64
    public var level: Double

    public init(frequency: Int64, level: Double) {
        self.frequency = frequency
        self.level = level
    }
}

public struct Exceedance: Equatable, Hashable, Identifiable, Sendable {
    public var sequence: Int
    public var low: Int64
    public var high: Int64
    public var width: Int64
    public var hits: Int

    public var id: Int { sequence }

    public var center: Int64 {
        low + (high - low) / 2
    }

    init(sequence: Int, low: Int64, high: Int64, width: Int64, hits: Int) {
        self.sequence = sequence
        self.low = low
        self.high = high
        self.width = width
        self.hits = hits
    }
}

public enum ExceedanceList {
    public static func update(existing: [Exceedance], bins: [LevelBin], threshold: Double) -> [Exceedance] {
        var rows = existing
        var next = (existing.map(\.sequence).max() ?? 0) + 1
        let ordered = bins.sorted { $0.frequency < $1.frequency }
        var index = 0
        while index < ordered.count {
            if ordered[index].level < threshold {
                index += 1
                continue
            }
            let start = index
            var end = index
            while end + 1 < ordered.count && ordered[end + 1].level >= threshold {
                end += 1
            }
            let low = ordered[start].frequency
            let high = ordered[end].frequency
            let span = high - low
            let width = span > 0 ? span : neighborSpacing(ordered, start: start, end: end)
            let center = low + (high - low) / 2
            if let match = rows.firstIndex(where: { row in
                center >= row.low && center <= row.high && width <= row.width * 5
            }) {
                rows[match].hits += 1
            } else {
                rows.append(Exceedance(sequence: next, low: low, high: high, width: width, hits: 1))
                next += 1
            }
            index = end + 1
        }
        return rows
    }

    private static func neighborSpacing(_ bins: [LevelBin], start: Int, end: Int) -> Int64 {
        if start > 0 {
            let gap = bins[start].frequency - bins[start - 1].frequency
            if gap > 0 { return gap }
        }
        if end + 1 < bins.count {
            let gap = bins[end + 1].frequency - bins[end].frequency
            if gap > 0 { return gap }
        }
        return 0
    }
}

public struct TraceEnvelope: Equatable, Sendable {
    public private(set) var minimum: [Int64: Double]
    public private(set) var maximum: [Int64: Double]

    public init() {
        minimum = [:]
        maximum = [:]
    }

    public mutating func absorb(_ bins: [LevelBin]) {
        for bin in bins {
            if let current = minimum[bin.frequency] {
                minimum[bin.frequency] = Swift.min(current, bin.level)
            } else {
                minimum[bin.frequency] = bin.level
            }
            if let current = maximum[bin.frequency] {
                maximum[bin.frequency] = Swift.max(current, bin.level)
            } else {
                maximum[bin.frequency] = bin.level
            }
        }
    }

    public func minimumSeries() -> [LevelBin] {
        series(minimum)
    }

    public func maximumSeries() -> [LevelBin] {
        series(maximum)
    }

    private func series(_ values: [Int64: Double]) -> [LevelBin] {
        values.keys.sorted().compactMap { key in
            values[key].map { LevelBin(frequency: key, level: $0) }
        }
    }
}
