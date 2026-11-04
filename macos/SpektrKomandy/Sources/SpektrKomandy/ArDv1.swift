import Foundation

public enum ArDv1AnalogMode: String, CaseIterable, Equatable, Hashable, Sendable {
    case fm
    case am

    public var commandText: String {
        switch self {
        case .fm:
            return "MD0F0"
        case .am:
            return "MD0F1"
        }
    }
}

public struct ArDv1Step: Equatable, Hashable, Identifiable, Sendable {
    public var id: String { parameter }
    public let parameter: String
    public let title: String
    public let unitsOf10Hz: Int64

    public static let overviewDefaultParameter = "010.00"

    public static let all: [ArDv1Step] = [
        ArDv1Step(parameter: "000.01", title: "0,01 кГц", unitsOf10Hz: 1),
        ArDv1Step(parameter: "000.05", title: "0,05 кГц", unitsOf10Hz: 5),
        ArDv1Step(parameter: "000.10", title: "0,1 кГц", unitsOf10Hz: 10),
        ArDv1Step(parameter: "000.50", title: "0,5 кГц", unitsOf10Hz: 50),
        ArDv1Step(parameter: "001.00", title: "1 кГц", unitsOf10Hz: 100),
        ArDv1Step(parameter: "002.00", title: "2 кГц", unitsOf10Hz: 200),
        ArDv1Step(parameter: "005.00", title: "5 кГц", unitsOf10Hz: 500),
        ArDv1Step(parameter: "006.25", title: "6,25 кГц", unitsOf10Hz: 625),
        ArDv1Step(parameter: "007.50", title: "7,5 кГц", unitsOf10Hz: 750),
        ArDv1Step(parameter: "008.33", title: "8,33 кГц", unitsOf10Hz: 833),
        ArDv1Step(parameter: "009.00", title: "9 кГц", unitsOf10Hz: 900),
        ArDv1Step(parameter: "010.00", title: "10 кГц", unitsOf10Hz: 1_000),
        ArDv1Step(parameter: "012.50", title: "12,5 кГц", unitsOf10Hz: 1_250),
        ArDv1Step(parameter: "015.00", title: "15 кГц", unitsOf10Hz: 1_500),
        ArDv1Step(parameter: "020.00", title: "20 кГц", unitsOf10Hz: 2_000),
        ArDv1Step(parameter: "025.00", title: "25 кГц", unitsOf10Hz: 2_500),
        ArDv1Step(parameter: "030.00", title: "30 кГц", unitsOf10Hz: 3_000),
        ArDv1Step(parameter: "050.00", title: "50 кГц", unitsOf10Hz: 5_000),
        ArDv1Step(parameter: "100.00", title: "100 кГц", unitsOf10Hz: 10_000),
        ArDv1Step(parameter: "500.00", title: "500 кГц", unitsOf10Hz: 50_000),
    ]

    public static var overviewDefault: ArDv1Step {
        all.first { $0.parameter == overviewDefaultParameter } ?? all[0]
    }

    init(parameter: String, title: String, unitsOf10Hz: Int64) {
        self.parameter = parameter
        self.title = title
        self.unitsOf10Hz = unitsOf10Hz
    }
}

public enum ArDv1Timing {
    public static let startingPauseMilliseconds = 25
    /// Пауза после RF на текущей прошивке не подтверждена.
    public static let pauseConfirmedOnCurrentFirmware = false
}

public struct ArDv1Command: Equatable, Sendable {
    public let text: String

    init(text: String) {
        self.text = text
    }

    public func frame() -> Data {
        var data = Data(text.utf8)
        data.append(0x0D)
        return data
    }
}

public enum ArDv1CommandFactory {
    public static func resultCodesOn() -> ArDv1Command { make("RE1") }
    public static func readFirmware() -> ArDv1Command { make("VR") }
    public static func readSerial() -> ArDv1Command { make("RN") }
    public static func selectVfoA() -> ArDv1Command { make("VFA") }
    public static func analogMode(_ mode: ArDv1AnalogMode) -> ArDv1Command { make(mode.commandText) }
    public static func step(_ step: ArDv1Step) -> ArDv1Command { make("ST" + step.parameter) }
    public static func readLevel() -> ArDv1Command { make("LM") }
    public static func leaveRemote() -> ArDv1Command { make("EX") }

    public static func frequency(units: Int64) throws -> ArDv1Command {
        guard FrequencyText.arFrequencyAllowed(units) else {
            throw SessionProblem.frequencyRange
        }
        return make(FrequencyText.rfCommandText(units: units))
    }

    private static func make(_ text: String) -> ArDv1Command {
        precondition(!text.contains(" "))
        precondition(!text.contains("\r"))
        precondition(!text.contains("\n"))
        return ArDv1Command(text: text)
    }
}

public struct ArDv1ResultCode: Equatable, Sendable {
    public var kind: Int
    public var continued: Bool
}

public struct ArDv1Line: Equatable, Sendable {
    public var raw: String
    public var resultCode: ArDv1ResultCode?
    /// Пример целой строки с кодом RE1 в описании не приведён.
    /// Разборщик ставит две цифры в начало ответа. На приёмнике это не проверено.
    public var resultCodePlacementConfirmed: Bool
    public var body: String
}

public enum ArDv1Squelch: Int, Equatable, Sendable {
    case closed = 0
    case open = 1
    case tone = 2
    case digitalDetected = 3

    public var title: String {
        switch self {
        case .closed:
            return "закрыт"
        case .open:
            return "открыт"
        case .tone:
            return "тон или код"
        case .digitalDetected:
            return "обнаружен цифровой режим"
        }
    }
}

public struct ArDv1Level: Equatable, Sendable {
    public var level: Int
    public var digitCount: Int
    /// В описании LM ширина поля записана и как два знака, и как три.
    public var digitCountConfirmed: Bool
    public var squelch: ArDv1Squelch
}

public struct ArDv1Identity: Equatable, Sendable {
    public var body: String
    public var value: String
    public var patternMatched: Bool
    /// Для номера длина в примере закрыта. Для прошивки живой ответ здесь тоже не снимался.
    public var lengthConfirmed: Bool
}

public enum ArDv1Reply {
    public static func parse(raw: String) -> ArDv1Line {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.count >= 2 {
            let characters = Array(text)
            if let kind = Int(String(characters[0])),
               (1...6).contains(kind),
               characters[1] == "0" || characters[1] == "1" {
                let rest = String(text.dropFirst(2))
                let head = rest.first
                let plausible = rest.isEmpty || head == "?" || head == " " || head?.isLetter == true
                if plausible {
                    return ArDv1Line(
                        raw: text,
                        resultCode: ArDv1ResultCode(kind: kind, continued: characters[1] == "1"),
                        resultCodePlacementConfirmed: false,
                        body: rest.trimmingCharacters(in: .whitespaces)
                    )
                }
            }
        }
        return ArDv1Line(
            raw: text,
            resultCode: nil,
            resultCodePlacementConfirmed: false,
            body: text
        )
    }

    public static func parseLevel(body: String) -> ArDv1Level? {
        guard body.hasPrefix("LM"), body.count > 3 else { return nil }
        let payload = body.dropFirst(2)
        guard let last = payload.last, let squelchCode = Int(String(last)), let squelch = ArDv1Squelch(rawValue: squelchCode) else {
            return nil
        }
        let digits = payload.dropLast()
        if digits.isEmpty || digits.count > 6 { return nil }
        for character in digits where character < "0" || character > "9" {
            return nil
        }
        guard let level = Int(digits) else { return nil }
        return ArDv1Level(
            level: level,
            digitCount: digits.count,
            digitCountConfirmed: false,
            squelch: squelch
        )
    }

    public static func parseFirmware(body: String) -> ArDv1Identity? {
        guard body.hasPrefix("VR") else { return nil }
        let value = String(body.dropFirst(2))
        if value.isEmpty { return nil }
        let matched = value.range(of: #"^\d{4}[A-Za-z]$"#, options: .regularExpression) != nil
        return ArDv1Identity(body: body, value: value, patternMatched: matched, lengthConfirmed: false)
    }

    public static func parseSerial(body: String) -> ArDv1Identity? {
        guard body.hasPrefix("RN") else { return nil }
        let value = String(body.dropFirst(2))
        if value.isEmpty { return nil }
        return ArDv1Identity(body: body, value: value, patternMatched: true, lengthConfirmed: false)
    }

    public static func isRejection(_ line: ArDv1Line) -> Bool {
        guard let code = line.resultCode else { return line.body == "?" }
        return code.kind >= 3
    }
}

public struct StuckMeterWatch: Equatable, Sendable {
    public static let startingLimit = 10
    /// Стартовая длина серии взята с наблюдения старой прошивки и здесь не подтверждена.
    public static let startingLimitConfirmedOnCurrentFirmware = false

    public let limit: Int
    public private(set) var run = 0
    private var last: Int?

    public init(limit: Int) {
        self.limit = limit
    }

    public mutating func observe(_ level: Int) -> Bool {
        if last == level {
            run += 1
        } else {
            last = level
            run = 1
        }
        return run >= limit && limit > 1
    }
}
