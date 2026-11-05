import Foundation

public enum SessionProblem: Error, Equatable, Sendable {
    case emptyPath
    case missingPort(String)
    case cannotOpen(path: String, detail: String)
    case portBusy(String)
    case accessDenied(String)
    case timedOut(String)
    case incompleteReply(String)
    case unsupportedBaud(Int)
    case notConnected
    case tooManyPoints(Int)
    case badNumber(String)
    case frequencyOrder
    case frequencyRange
    case stuckMeter(level: Int, count: Int)
    case deviceReply(String)
    case emptyDeviceReply(String)
    case badLevel(String)
    case unparsedScan(String)
    case cannotWriteTrace(String)
}

extension SessionProblem: LocalizedError {
    public var errorDescription: String? {
        switch self {
        case .emptyPath:
            return "Укажите путь к порту."
        case .missingPort(let path):
            return "Порт «\(path)» не найден."
        case .cannotOpen(let path, let detail):
            return "Не удалось открыть порт «\(path)»: \(detail)"
        case .portBusy(let path):
            return "Порт «\(path)» занят."
        case .accessDenied(let path):
            return "Нет доступа к порту «\(path)»."
        case .timedOut(let what):
            return "Ответ не пришёл вовремя: \(what)."
        case .incompleteReply(let raw):
            return "Ответ оборвался: \(raw)"
        case .unsupportedBaud(let baud):
            return "Скорость \(baud) для этого порта не поддерживается открытием."
        case .notConnected:
            return "Прибор не подключён."
        case .tooManyPoints(let count):
            return "Слишком много точек: \(count). Сузьте границы или увеличьте шаг."
        case .badNumber(let field):
            return "Проверьте поле «\(field)»: нужно число."
        case .frequencyOrder:
            return "Нижняя граница больше верхней."
        case .frequencyRange:
            return "Частота вне диапазона 0,1–1300 МГц."
        case .stuckMeter(let level, let count):
            return "Подряд один и тот же отсчёт \(level), уже \(count) раз. Обзор остановлен. Команду сброса приёмника программа не отправляла."
        case .deviceReply(let raw):
            return "Прибор не принял команду. Ответ: \(raw)"
        case .emptyDeviceReply(let command):
            return "Пустой ответ на «\(command)». Конец строки или скорость могли не совпасть с прибором."
        case .badLevel(let raw):
            return "Не удалось разобрать уровень. Ответ: \(raw)"
        case .unparsedScan(let raw):
            return "В ответе scan не нашлось пар частота–уровень. Раскладка маски 3 документом не задана. Ответ: \(raw)"
        case .cannotWriteTrace(let detail):
            return "Не удалось записать файл трассы: \(detail)"
        }
    }
}
