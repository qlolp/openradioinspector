import Foundation

/// История свипов для водопада: строки «время × частота».
/// Порядок столбцов задаёт первый принятый свип. Если набор частот
/// меняется, история начинается заново: смешивать в одном окне строки
/// с разной раскладкой столбцов нельзя.
public struct WaterfallHistory: Equatable, Sendable {
    /// Частоты столбцов в порядке первого свипа.
    public private(set) var grid: [Int64] = []
    /// Строки уровней; последняя — самая свежая.
    public private(set) var rows: [[Double]] = []
    /// Сколько свипов принято всего, включая вытесненные.
    public private(set) var absorbedSweeps: Int = 0
    /// Сколько старых строк вытеснено за пределы окна.
    public private(set) var droppedRows: Int = 0
    /// Максимальное число хранимых строк.
    public let capacity: Int

    public init(capacity: Int = WaterfallLimits.startingCapacity) {
        self.capacity = max(1, capacity)
    }

    /// Принимает один законченный свип.
    /// Возвращает true, если набор частот сменился и при этом отброшены
    /// накопленные строки. Первый свип пустой истории перезапуском не считается.
    @discardableResult
    public mutating func absorb(_ bins: [LevelBin]) -> Bool {
        let frequencies = bins.map(\.frequency)
        var restarted = false
        if frequencies != grid {
            restarted = !rows.isEmpty
            grid = frequencies
            rows = []
            droppedRows = 0
        }
        rows.append(bins.map(\.level))
        absorbedSweeps += 1
        if rows.count > capacity {
            let extra = rows.count - capacity
            rows.removeFirst(extra)
            droppedRows += extra
        }
        return restarted
    }
}

public enum WaterfallLimits {
    /// Стартовая глубина окна. У приёмника свип медленный,
    /// поэтому запас больше, чем у анализатора, всё равно пригодится.
    public static let startingCapacity = 240
}

/// Шкала уровней водопада: перевод уровня в долю 0...1 для палитры.
public struct WaterfallScale: Equatable, Sendable {
    public var low: Double
    public var high: Double

    public init(low: Double, high: Double) {
        if high > low {
            self.low = low
            self.high = high
        } else {
            // Все уровни одинаковые: шкалу раздвигаем, иначе деление на ноль.
            self.low = low - 0.5
            self.high = high + 0.5
        }
    }

    /// Доля шкалы для уровня; выход за шкалу прижимается к краям.
    public func fraction(_ level: Double) -> Double {
        let raw = (level - low) / (high - low)
        return Swift.min(1, Swift.max(0, raw))
    }

    /// Автошкала по всем видимым строкам; пустая история шкалы не даёт.
    public static func auto(rows: [[Double]]) -> WaterfallScale? {
        var low = Double.greatestFiniteMagnitude
        var high = -Double.greatestFiniteMagnitude
        var found = false
        for row in rows {
            for level in row where level.isFinite {
                low = Swift.min(low, level)
                high = Swift.max(high, level)
                found = true
            }
        }
        guard found else { return nil }
        return WaterfallScale(low: low, high: high)
    }
}

/// Палитра водопада: тёмный фон → пурпур → оранжевый → жёлтый.
/// Каналы от 0 до 1, без привязки к цветовому пространству экрана:
/// подстановку в sRGB делает отрисовка.
public enum WaterfallPalette {
    public struct Stop: Equatable, Sendable {
        public var position: Double
        public var red: Double
        public var green: Double
        public var blue: Double

        public init(_ position: Double, _ red: Double, _ green: Double, _ blue: Double) {
            self.position = position
            self.red = red
            self.green = green
            self.blue = blue
        }
    }

    /// Опорные точки градиента, по мотивам шкалы inferno.
    public static let stops: [Stop] = [
        Stop(0.00, 0.00, 0.00, 0.02),
        Stop(0.25, 0.20, 0.03, 0.33),
        Stop(0.50, 0.51, 0.09, 0.43),
        Stop(0.75, 0.85, 0.26, 0.23),
        Stop(1.00, 0.99, 1.00, 0.64),
    ]

    /// Цвет доли шкалы; доля вне 0...1 прижимается к краям.
    public static func color(_ fraction: Double) -> (red: Double, green: Double, blue: Double) {
        let t = Swift.min(1, Swift.max(0, fraction))
        var upper = stops.count - 1
        for index in stops.indices where stops[index].position >= t {
            upper = index
            break
        }
        if upper == 0 {
            let stop = stops[0]
            return (stop.red, stop.green, stop.blue)
        }
        let low = stops[upper - 1]
        let high = stops[upper]
        let span = high.position - low.position
        let mix = span > 0 ? (t - low.position) / span : 0
        return (
            low.red + (high.red - low.red) * mix,
            low.green + (high.green - low.green) * mix,
            low.blue + (high.blue - low.blue) * mix
        )
    }
}
