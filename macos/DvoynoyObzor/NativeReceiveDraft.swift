import Foundation
import SpektrKomandy

struct NativeReceiveDraft {
    var attenuation = "auto"
    var lna = false
    var ultra = false
    var points = "201"
    var repeats = "1"
    var sweepSeconds = "0"
    var accuracy = "normal"
    var spur = "auto"
    var vbw = "auto"
    var useVBW = false
    var trigger = "auto"
    var triggerLevel = "-60"

    func lnaAdvice(stopHz: Int64?) -> String? {
        guard lna, let stopHz, stopHz > 3_500_000_000 else { return nil }
        if stopHz > 4_000_000_000 {
            return "Верхняя граница выходит за паспортный диапазон LNA. Выключите LNA для этой развёртки."
        }
        return "Выше 3,5 ГГц сравните показания с LNA и без него."
    }

    func parameters() throws -> [String: MonitoringValue] {
        func integer(_ text: String, _ range: ClosedRange<Int64>, _ name: String) throws -> Int64 {
            guard let value = Int64(text), range.contains(value) else { throw PlatformError.invalidCommand("Проверьте поле \(name)") }
            return value
        }
        guard let seconds = Double(sweepSeconds), seconds.isFinite, (0...60).contains(seconds),
              let level = Double(triggerLevel), level.isFinite, (-174...30).contains(level) else {
            throw PlatformError.invalidCommand("Время: 0–60 с; триггер: −174…30 дБм")
        }
        let atten: MonitoringValue = attenuation == "auto" ? .string("auto") : .integer(try integer(attenuation, 0...31, "аттенюатор"))
        if lna && attenuation != "auto" && attenuation != "0" { throw PlatformError.invalidCommand("Для LNA выберите аттенюатор 0 дБ или Auto") }
        var values: [String: MonitoringValue] = ["attenuation_db": atten, "lna": .boolean(lna), "ultra": .boolean(ultra),
            "points": .integer(try integer(points, 2...450, "точки")), "sweep_seconds": .number(seconds),
            "sweep_accuracy": .string(accuracy), "spur": .string(spur), "trigger_mode": .string(trigger), "trigger_dbm": .number(level)]
        if useVBW { values["vbw_ratio"] = .string(vbw) }
        else { values["repeat"] = .integer(try integer(repeats, 1...1000, "повторы")) }
        return values
    }

    mutating func load(_ settings: MonitoringValue) {
        if settings["attenuation_db"] != .null { attenuation = settings["attenuation_db"].scalar }
        lna = settings["lna"].bool ?? false; ultra = settings["ultra"].bool ?? false
        if let n = settings["points"].integer { points = String(n) }
        if let n = settings["repeat"].integer { repeats = String(n) }
        if let n = settings["sweep_seconds"].double { sweepSeconds = String(n) }
        if !settings["sweep_accuracy"].text.isEmpty { accuracy = settings["sweep_accuracy"].text }
        if !settings["spur"].text.isEmpty { spur = settings["spur"].text }
        if !settings["trigger_mode"].text.isEmpty { trigger = settings["trigger_mode"].text }
        if let n = settings["trigger_dbm"].double { triggerLevel = String(n) }
        useVBW = settings["vbw_ratio"] != .null
        if useVBW { vbw = settings["vbw_ratio"].text }
    }
}

struct NativeWatchBand: Identifiable {
    let id = UUID()
    var name = "Полоса"
    var start = "88"
    var stop = "108"
    var rbw = "30000"
    var points = "201"
    func value() throws -> MonitoringValue {
        let low = try NativeAnalyzerDraft.hz(start), high = try NativeAnalyzerDraft.hz(stop)
        guard low < high, let count = Int64(points), (2...450).contains(count), let bandwidth = Int64(rbw) else {
            throw PlatformError.invalidCommand("Проверьте диапазон, точки и RBW полосы")
        }
        return .object(["name": .string(name), "start_hz": .integer(low), "stop_hz": .integer(high), "points": .integer(count), "rbw_hz": .integer(bandwidth)])
    }
}
