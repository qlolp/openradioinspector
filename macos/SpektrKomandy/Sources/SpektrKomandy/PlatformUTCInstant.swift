/// Exact Gregorian UTC instants used for ordering and validating raw sources.
/// Integer microseconds avoid formatter allocation, locale and floating-point rounding.
enum PlatformUTCInstant {
    static func microseconds(_ text: String) -> Int64? {
        let bytes = Array(text.utf8)
        guard (20...32).contains(bytes.count) else { return nil }
        let end: Int
        if bytes.last == 90 {
            end = bytes.count - 1
        } else if bytes.suffix(6).elementsEqual([43, 48, 48, 58, 48, 48]) {
            end = bytes.count - 6
        } else { return nil }
        guard end >= 19, bytes[4] == 45, bytes[7] == 45, bytes[10] == 84,
              bytes[13] == 58, bytes[16] == 58 else { return nil }
        func number(_ start: Int, _ count: Int) -> Int? {
            var value = 0
            for index in start..<(start + count) {
                let digit = bytes[index]
                guard (48...57).contains(digit) else { return nil }
                value = value * 10 + Int(digit - 48)
            }
            return value
        }
        guard let year = number(0, 4), year > 0,
              let month = number(5, 2), (1...12).contains(month),
              let day = number(8, 2), day > 0,
              let hour = number(11, 2), hour < 24,
              let minute = number(14, 2), minute < 60,
              let second = number(17, 2), second < 60 else { return nil }
        let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
        let maximumDay = month == 2 ? (leap ? 29 : 28) : ([4, 6, 9, 11].contains(month) ? 30 : 31)
        guard day <= maximumDay else { return nil }
        var fraction: Int64 = 0
        if end != 19 {
            guard (21...26).contains(end), bytes[19] == 46 else { return nil }
            for index in 20..<end {
                guard (48...57).contains(bytes[index]) else { return nil }
                fraction = fraction * 10 + Int64(bytes[index] - 48)
            }
            for _ in (end - 20)..<6 { fraction *= 10 }
        }
        // Gregorian days relative to 1970-01-01, with a March-based civil year.
        let adjustedYear = year - (month <= 2 ? 1 : 0)
        let era = adjustedYear / 400
        let yearOfEra = adjustedYear - era * 400
        let dayOfYear = (153 * (month + (month > 2 ? -3 : 9)) + 2) / 5 + day - 1
        let dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear
        let days = Int64(era * 146097 + dayOfEra - 719468)
        let seconds = days * 86400 + Int64(hour * 3600 + minute * 60 + second)
        return seconds * 1_000_000 + fraction
    }
}
