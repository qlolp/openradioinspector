import Foundation

public enum FrequencyText {
    public static let tenHertzPerMegahertz: Int64 = 100_000
    public static let arMinimumUnits: Int64 = 10_000
    public static let arMaximumUnits: Int64 = 130_000_000

    public static func parseMHz(_ text: String) -> Int64? {
        let trimmed = text
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: ",", with: ".")
        if trimmed.isEmpty { return nil }
        let parts = trimmed.split(separator: ".", omittingEmptySubsequences: false)
        if parts.count > 2 { return nil }
        guard let whole = parseDigits(parts[0]) else { return nil }
        var fraction: Int64 = 0
        if parts.count == 2 {
            let digits = parts[1]
            if digits.isEmpty || digits.count > 5 { return nil }
            guard let raw = parseDigits(digits) else { return nil }
            fraction = raw * powerOfTen(5 - digits.count)
        }
        let scaled = whole.multipliedReportingOverflow(by: tenHertzPerMegahertz)
        guard !scaled.overflow else { return nil }
        let units = scaled.partialValue.addingReportingOverflow(fraction)
        guard !units.overflow else { return nil }
        return units.partialValue
    }

    public static func arFrequencyAllowed(_ units: Int64) -> Bool {
        units >= arMinimumUnits && units <= arMaximumUnits
    }

    public static func rfCommandText(units: Int64) -> String {
        let whole = units / tenHertzPerMegahertz
        let fraction = units % tenHertzPerMegahertz
        return "RF" + padded(whole, 4) + "." + padded(fraction, 5)
    }

    public static func formatScaled(_ value: Int64, denominator: Int64, separator: String) -> String {
        let negative = value < 0
        let absolute = negative ? -value : value
        let whole = absolute / denominator
        let fraction = absolute % denominator
        let width = digitCount(denominator) - 1
        var digits = padded(fraction, width)
        while digits.last == "0" {
            digits.removeLast()
        }
        let body = digits.isEmpty ? String(whole) : "\(whole)\(separator)\(digits)"
        return negative ? "-" + body : body
    }

    public static func formatMHzMachine(from10Hz units: Int64) -> String {
        formatScaled(units, denominator: tenHertzPerMegahertz, separator: ".")
    }

    public static func formatMHzRussian(from10Hz units: Int64) -> String {
        formatScaled(units, denominator: tenHertzPerMegahertz, separator: ",")
    }

    public static func formatKHzRussian(from10Hz units: Int64) -> String {
        formatScaled(units, denominator: 100, separator: ",")
    }

    public static func formatMHzRussian(fromHertz hertz: Int64) -> String {
        formatScaled(hertz, denominator: 1_000_000, separator: ",")
    }

    public static func formatKHzRussian(fromHertz hertz: Int64) -> String {
        formatScaled(hertz, denominator: 1_000, separator: ",")
    }

    public static func grid(from start: Int64, to end: Int64, step: Int64) -> [Int64] {
        guard step > 0, end >= start else { return [] }
        var points: [Int64] = []
        var cursor = start
        while cursor <= end {
            points.append(cursor)
            if points.count > ScanBudget.maximumPoints {
                break
            }
            let advanced = cursor.addingReportingOverflow(step)
            if advanced.overflow {
                break
            }
            cursor = advanced.partialValue
        }
        return points
    }

    private static func parseDigits(_ text: Substring) -> Int64? {
        if text.isEmpty { return nil }
        for character in text {
            if character < "0" || character > "9" { return nil }
        }
        return Int64(text)
    }

    private static func powerOfTen(_ exponent: Int) -> Int64 {
        var value: Int64 = 1
        var remaining = exponent
        while remaining > 0 {
            value *= 10
            remaining -= 1
        }
        return value
    }

    private static func digitCount(_ value: Int64) -> Int {
        var count = 1
        var remainder = value
        while remainder >= 10 {
            remainder /= 10
            count += 1
        }
        return count
    }

    private static func padded(_ value: Int64, _ width: Int) -> String {
        let text = String(value)
        if text.count >= width { return text }
        return String(repeating: "0", count: width - text.count) + text
    }
}

public enum ScanBudget {
    public static let maximumPoints = 20_000
}
