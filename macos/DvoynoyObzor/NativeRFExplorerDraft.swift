import Foundation
import SpektrKomandy

struct NativeRFExplorerPreset: Codable, Identifiable {
    let name: String
    let bounds: ClosedRange<Int64>
    var id: String { name }
}

struct NativeRFExplorerDraft {
    enum Mode { case instrument, window, survey }
    enum Style { case startStop, centerSpan }
    var mode = Mode.instrument
    var port = ""
    var customSurveyRange = false
    var style = Style.startStop
    var startMHz = "", stopMHz = "", centerMHz = "", spanMHz = ""

    var usesBounds: Bool { mode == .window || (mode == .survey && customSurveyRange) }
    var needsPort: Bool { port.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }

    var plannedSurveyTiles: Int? {
        guard mode == .survey, usesBounds, let range = try? enteredBounds() else { return nil }
        return try? PlatformRFExplorerConfiguration.plannedSurveyTiles(startHz: Int(range.lowerBound), stopHz: Int(range.upperBound))
    }

    func configuration() throws -> PlatformRFExplorerConfiguration {
        let bounds = usesBounds ? try enteredBounds() : nil
        return try PlatformRFExplorerConfiguration(port: port,
            startHz: bounds.map { Int($0.lowerBound) }, stopHz: bounds.map { Int($0.upperBound) },
            surveyStepHz: mode == .survey ? PlatformRFExplorerConfiguration.surveySpacingHz : nil)
    }

    private func enteredBounds() throws -> ClosedRange<Int64> {
        let low, high: Int64
        switch style {
        case .startStop:
            low = try NativeHistoryFrequencyInput.hz(startMHz)
            high = try NativeHistoryFrequencyInput.hz(stopMHz)
        case .centerSpan:
            let center = try NativeHistoryFrequencyInput.hz(centerMHz)
            let span = try NativeHistoryFrequencyInput.hz(spanMHz)
            guard span.isMultiple(of: 2), center >= span / 2 else {
                throw PlatformError.invalidCommand("Границы диапазона должны выражаться целыми неотрицательными Гц.")
            }
            low = center - span / 2
            high = center + span / 2
        }
        guard low < high else { throw PlatformError.invalidCommand("Конец диапазона должен быть выше начала.") }
        return low...high
    }

    mutating func switchStyle(to next: Style) throws {
        guard next != style else { return }
        let empty = style == .startStop ? startMHz.isEmpty && stopMHz.isEmpty : centerMHz.isEmpty && spanMHz.isEmpty
        if !empty {
            let range = try enteredBounds()
            guard (range.upperBound - range.lowerBound).isMultiple(of: 2) else {
                throw PlatformError.invalidCommand("В таком диапазоне центр приходится на половину Гц.")
            }
            setFields(range)
        }
        style = next
    }

    private mutating func setFields(_ range: ClosedRange<Int64>) {
        let span = range.upperBound - range.lowerBound
        startMHz = NativeHistoryFrequencyInput.mhz(range.lowerBound)
        stopMHz = NativeHistoryFrequencyInput.mhz(range.upperBound)
        centerMHz = NativeHistoryFrequencyInput.mhz(range.lowerBound + span / 2)
        spanMHz = NativeHistoryFrequencyInput.mhz(span)
    }

    mutating func applySurveyRange(_ range: ClosedRange<Int64>?) {
        mode = .survey
        customSurveyRange = range != nil
        if let range { setFields(range) }
    }

    func savedRange(name: String) throws -> NativeRFExplorerPreset {
        let title = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard mode == .survey, usesBounds, !title.isEmpty, title.count <= 80 else {
            throw PlatformError.invalidCommand("Введите имя диапазона, до 80 символов.")
        }
        let range = try enteredBounds()
        try PlatformRFExplorerConfiguration.validateBounds(startHz: Int(range.lowerBound), stopHz: Int(range.upperBound),
                                                          surveyStepHz: PlatformRFExplorerConfiguration.surveySpacingHz)
        return NativeRFExplorerPreset(name: title, bounds: range)
    }
}

struct NativeRFExplorerReadback {
    let board, input: String
    let bounds: ClosedRange<Int64>
    let simulated: Bool?

    init?(_ settings: [String: PlatformComparisonSetting]?) {
        guard let settings, settings["settings_readback"] == .boolean(true),
              settings["settings_source"] == .string("device_readback"),
              case .string(let board) = settings["active_board"], case .string(let input) = settings["active_input"],
              case .number(let low) = settings["board_min_hz"], case .number(let high) = settings["board_max_hz"],
              let start = Int64(exactly: low), let stop = Int64(exactly: high), start >= 0, start < stop else { return nil }
        self.board = board
        self.input = input
        bounds = start...stop
        if case .boolean(let value) = settings["simulated"] { simulated = value }
        else { simulated = nil }
    }

    var label: String {
        let boardName = board == "main" ? "основная плата" : (board == "expansion" ? "плата расширения" : board)
        let inputName = input == "left" ? "левый вход SMA" : (input == "right" ? "правый вход SMA" : input)
        let range = "\(FrequencyText.formatMHzRussian(fromHertz: bounds.lowerBound))–\(FrequencyText.formatMHzRussian(fromHertz: bounds.upperBound)) МГц"
        let origin: String
        switch simulated {
        case true: origin = "Симуляция"
        case false: origin = "Считано с прибора"
        case nil: origin = "Считанная конфигурация · признак симуляции не указан"
        }
        return "\(origin) · \(boardName) · \(inputName) · пределы \(range)"
    }
}
