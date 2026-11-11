import Foundation

/// Exact UTC instant for the v1 contract. Source strings remain unchanged in measurements.
public struct PlatformTimestamp: Equatable, Comparable, Sendable {
    public let utcMicroseconds: Int64

    public init(_ text: String) throws {
        let bytes = Array(text.utf8)
        let pattern = #"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\z"#
        guard bytes.allSatisfy({ $0 < 128 }), text.range(of: pattern, options: .regularExpression) != nil else {
            throw PlatformError.invalidResponse("Нужно время ISO 8601 с часовым поясом и точностью до микросекунд")
        }
        func number(_ start: Int, _ end: Int) -> Int64 {
            bytes[start..<end].reduce(0) { $0 * 10 + Int64($1 - 48) }
        }
        let year = number(0, 4), month = Int(number(5, 7)), day = number(8, 10)
        let hour = number(11, 13), minute = number(14, 16), second = number(17, 19)
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
        let lengths: [Int64] = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        guard year > 0, (1...12).contains(month), (1...lengths[month - 1]).contains(day), hour < 24, minute < 60, second < 60 else {
            throw PlatformError.invalidResponse("Некорректная календарная дата")
        }
        let zoneStart = text.hasSuffix("Z") ? bytes.count - 1 : bytes.count - 6
        var offset: Int64 = 0
        if !text.hasSuffix("Z") {
            let h = number(zoneStart + 1, zoneStart + 3), m = number(zoneStart + 4, zoneStart + 6)
            guard h < 24, m < 60 else { throw PlatformError.invalidResponse("Некорректный часовой пояс") }
            offset = (h * 60 + m) * 60 * (bytes[zoneStart] == 45 ? -1 : 1)
        }
        var fraction: Int64 = 0
        if zoneStart > 19 {
            fraction = number(20, zoneStart)
            for _ in 0..<(6 - (zoneStart - 20)) { fraction *= 10 }
        }
        let prior = year - 1
        let days = prior * 365 + prior / 4 - prior / 100 + prior / 400 + lengths.prefix(month - 1).reduce(0, +) + day - 1 - 719_162
        utcMicroseconds = ((days * 24 + hour) * 3600 + minute * 60 + second - offset) * 1_000_000 + fraction
        guard (-62_135_596_800_000_000...253_402_300_799_999_999).contains(utcMicroseconds) else {
            throw PlatformError.invalidResponse("Дата вне диапазона UTC 0001…9999")
        }
    }

    public var canonicalUTC: String {
        let seconds = utcMicroseconds >= 0 ? utcMicroseconds / 1_000_000 : (utcMicroseconds - 999_999) / 1_000_000
        let micros = utcMicroseconds - seconds * 1_000_000
        // Proleptic Gregorian inverse; Foundation's historical calendar cutover
        // would otherwise shift dates before 1582. Microseconds never enter Date/Double.
        let days = seconds >= 0 ? seconds / 86_400 : (seconds - 86_399) / 86_400
        let clock = seconds - days * 86_400
        let z = days + 719_468
        let era = (z >= 0 ? z : z - 146_096) / 146_097
        let doe = z - era * 146_097
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365
        var year = yoe + era * 400
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100)
        let mp = (5 * doy + 2) / 153
        let day = doy - (153 * mp + 2) / 5 + 1
        let month = mp + (mp < 10 ? 3 : -9)
        year += month <= 2 ? 1 : 0
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%06dZ", year, month, day, clock / 3600, (clock % 3600) / 60, clock % 60, micros)
    }

    public static func < (a: Self, b: Self) -> Bool { a.utcMicroseconds < b.utcMicroseconds }
}
