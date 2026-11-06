import Foundation

public enum TraceClock {
    public static func stamp(_ date: Date) -> String {
        guard let zone = TimeZone(secondsFromGMT: 0) else {
            return "1970-01-01T00:00:00Z"
        }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: date)
        return four(parts.year ?? 0)
            + "-"
            + two(parts.month ?? 0)
            + "-"
            + two(parts.day ?? 0)
            + "T"
            + two(parts.hour ?? 0)
            + ":"
            + two(parts.minute ?? 0)
            + ":"
            + two(parts.second ?? 0)
            + "Z"
    }

    private static func two(_ value: Int) -> String {
        let text = String(value)
        if text.count >= 2 { return text }
        return String(repeating: "0", count: 2 - text.count) + text
    }

    private static func four(_ value: Int) -> String {
        let text = String(value)
        if text.count >= 4 { return text }
        return String(repeating: "0", count: 4 - text.count) + text
    }

    public static func fileToken(_ date: Date) -> String {
        stamp(date)
            .replacingOccurrences(of: "-", with: "")
            .replacingOccurrences(of: ":", with: "")
    }

    public static func sessionFilename(instrument: String, date: Date = Date()) -> String {
        "\(instrument)-\(fileToken(date))-\(UUID().uuidString).trassa.json"
    }
}

public struct ArSweep: Codable, Equatable, Sendable {
    public var time: String
    public var frequenciesMHz: [String]
    public var levels: [Int]

    public init(time: String, frequenciesMHz: [String], levels: [Int]) {
        self.time = time
        self.frequenciesMHz = frequenciesMHz
        self.levels = levels
    }

    enum CodingKeys: String, CodingKey {
        case time
        case frequenciesMHz = "frequencies_mhz"
        case levels
    }
}

public struct ArTraceDocument: Codable, Equatable, Sendable {
    public var format: String
    public var instrument: String
    public var f1MHz: String
    public var f2MHz: String
    public var step: String
    public var mode: String
    public var firmware: String
    public var levelUnit: String
    public var sweeps: [ArSweep]

    public init(
        f1MHz: String,
        f2MHz: String,
        step: String,
        mode: String,
        firmware: String,
        sweeps: [ArSweep]
    ) {
        self.format = "dvoynoy-obzor-trassa-1"
        self.instrument = "AOR AR-DV1"
        self.f1MHz = f1MHz
        self.f2MHz = f2MHz
        self.step = step
        self.mode = mode
        self.firmware = firmware
        self.levelUnit = "s-meter-count"
        self.sweeps = sweeps
    }

    enum CodingKeys: String, CodingKey {
        case format
        case instrument
        case f1MHz = "f1_mhz"
        case f2MHz = "f2_mhz"
        case step
        case mode
        case firmware
        case levelUnit = "level_unit"
        case sweeps
    }
}

public struct UltraSweep: Codable, Equatable, Sendable {
    public var time: String
    public var frequenciesHz: [String]
    public var levelsDbm: [String]
    public var unparsed: [String]

    public init(time: String, frequenciesHz: [String], levelsDbm: [String], unparsed: [String]) {
        self.time = time
        self.frequenciesHz = frequenciesHz
        self.levelsDbm = levelsDbm
        self.unparsed = unparsed
    }

    enum CodingKeys: String, CodingKey {
        case time
        case frequenciesHz = "frequencies_hz"
        case levelsDbm = "levels_dbm"
        case unparsed
    }
}

public struct UltraTraceDocument: Codable, Equatable, Sendable {
    public var format: String
    public var instrument: String
    public var scan: String
    public var rbw: String
    public var lna: String
    public var ultraMode: String
    public var attenuator: String
    public var version: String
    public var levelUnit: String
    public var terminator: String
    public var terminatorConfirmed: Bool
    public var scanLayoutConfirmed: Bool
    public var sweeps: [UltraSweep]

    public init(
        version: String,
        sweeps: [UltraSweep],
        terminatorConfirmed: Bool = true,
        scanLayoutConfirmed: Bool = true
    ) {
        self.format = "dvoynoy-obzor-trassa-1"
        self.instrument = "tinySA Ultra"
        self.scan = UltraSessionScript.scanText
        self.rbw = "30"
        self.lna = "off"
        self.ultraMode = "not-sent"
        self.attenuator = "auto"
        self.version = version
        self.levelUnit = "dBm"
        self.terminator = "CR"
        self.terminatorConfirmed = terminatorConfirmed
        self.scanLayoutConfirmed = scanLayoutConfirmed
        self.sweeps = sweeps
    }

    enum CodingKeys: String, CodingKey {
        case format
        case instrument
        case scan
        case rbw
        case lna
        case ultraMode = "ultra_mode"
        case attenuator
        case version
        case levelUnit = "level_unit"
        case terminator
        case terminatorConfirmed = "terminator_confirmed"
        case scanLayoutConfirmed = "scan_layout_confirmed"
        case sweeps
    }
}

public enum TraceArchive {
    public static func encode(_ document: ArTraceDocument) throws -> Data {
        try encoder().encode(document)
    }

    public static func encode(_ document: UltraTraceDocument) throws -> Data {
        try encoder().encode(document)
    }

    private static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        return encoder
    }
}
