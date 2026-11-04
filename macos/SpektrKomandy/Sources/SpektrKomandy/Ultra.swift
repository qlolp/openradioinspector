import Foundation

public enum UltraPortBaud {
    /// Страницы USB-консоли скорость линии не задают.
    /// Число нужно только чтобы открыть порт через termios. Это не скорость из документации.
    public static let termiosPlaceholder = 9600
    public static let confirmedByDevicePages = false
}

public struct UltraCommand: Equatable, Sendable {
    public let text: String

    /// Байт конца команды с ПК в описании USB-консоли не назван.
    /// Выбран один байт CR (0x0D). На приборе это не проверено.
    public static let terminator = Data([0x0D])
    public static let terminatorNeedsInstrumentCheck = true

    public init(text: String) {
        self.text = text
    }

    public func frame() -> Data {
        var data = Data(text.utf8)
        data.append(contentsOf: Self.terminator)
        return data
    }
}

public enum UltraSessionScript {
    public static let infoText = "info"
    public static let versionText = "version"
    public static let userNumberText = "deviceid"
    public static let scanText = "scan 100k 800M 290 3"

    public static let identityTexts = [infoText, versionText, userNumberText]

    public static let setupTexts = [
        "output off",
        "mode input",
        "lna off",
        "attenuate auto",
        "rbw 30",
        "calc off",
    ]

    public static func identityCommands() -> [UltraCommand] {
        identityTexts.map(UltraCommand.init)
    }

    public static func setupCommands() -> [UltraCommand] {
        setupTexts.map(UltraCommand.init)
    }

    public static func scanCommand() -> UltraCommand {
        UltraCommand(text: scanText)
    }
}

public enum UltraIdentity {
    /// Сверка со снимком консоли, а не с текущей прошивкой конкретного прибора.
    public static let currentFirmwareTextConfirmed = false

    public static func matchesConsoleSnapshot(info: String, version: String) -> Bool {
        info.contains("tinySA Ultra") && version.contains("tinySA4_")
    }
}

public enum UltraInputCeiling {
    public static let recommendedAutoAttenuatorDbm = 0.0
}

public enum UltraReadTiming {
    /// Конец ответа в описании не задан: ждём тишину на линии.
    public static let quietSeconds = 0.25
    public static let quietGapConfirmedOnDevice = false
    public static let setupMaximumSeconds = 3.0
    public static let scanMaximumSeconds = 90.0
}

public struct UltraPoint: Equatable, Sendable {
    public var hertz: Int64
    public var dbm: Double
    public var dbmToken: String

    public init(hertz: Int64, dbm: Double, dbmToken: String) {
        self.hertz = hertz
        self.dbm = dbm
        self.dbmToken = dbmToken
    }
}

public struct UltraScanParse: Equatable, Sendable {
    public var points: [UltraPoint]
    public var ignoredLines: [String]
    public var unparsedLines: [String]
    /// Образца строки `scan` с маской 3 в описании нет.
    public var layoutConfirmedOnDevice: Bool
}

public enum UltraScanParser {
    public static func parse(_ text: String) -> UltraScanParse {
        var points: [UltraPoint] = []
        var ignored: [String] = []
        var unparsed: [String] = []
        for rawLine in text.split(whereSeparator: { $0 == "\n" || $0 == "\r" }) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line == "ch>" || line == UltraSessionScript.scanText {
                ignored.append(line)
                continue
            }
            let tokens = line.split(whereSeparator: { $0 == " " || $0 == "\t" || $0 == "," }).map(String.init)
            if tokens.count == 2, let hertz = parseHertz(tokens[0]), let level = parseDbm(tokens[1]) {
                points.append(UltraPoint(hertz: hertz, dbm: level.value, dbmToken: level.token))
            } else {
                unparsed.append(line)
            }
        }
        return UltraScanParse(
            points: points,
            ignoredLines: ignored,
            unparsedLines: unparsed,
            layoutConfirmedOnDevice: false
        )
    }

    private static func parseHertz(_ token: String) -> Int64? {
        if let plain = Int64(token) {
            return plain
        }
        guard let suffix = token.last, suffix == "k" || suffix == "M" || suffix == "G" else {
            return nil
        }
        let number = String(token.dropLast()).replacingOccurrences(of: ",", with: ".")
        guard let value = Double(number) else { return nil }
        let scale: Double
        switch suffix {
        case "k":
            scale = 1_000
        case "M":
            scale = 1_000_000
        case "G":
            scale = 1_000_000_000
        default:
            return nil
        }
        return Int64((value * scale).rounded())
    }

    private static func parseDbm(_ token: String) -> (value: Double, token: String)? {
        var number = token
        if number.hasSuffix("dBm") {
            number.removeLast(3)
        } else if number.hasSuffix("dbm") {
            number.removeLast(3)
        }
        let normalized = number.replacingOccurrences(of: ",", with: ".")
        guard let value = Double(normalized), number.contains(where: { $0.isNumber }) else {
            return nil
        }
        return (value, token)
    }
}
