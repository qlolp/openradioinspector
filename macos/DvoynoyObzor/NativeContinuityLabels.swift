import Foundation

enum NativeContinuityLabels {
    static func coverage(_ value: String, coreReachable: Bool, statusVerified: Bool) -> String {
        guard coreReachable, statusVerified else { return "Покрытие не подтверждено" }
        return ["wide": "Широкий обзор", "narrow": "Узкая полоса · широкий обзор приостановлен", "restoring": "Восстановление широкого обзора", "preparation": "Подготовка эталона", "stopped": "Покрытие остановлено", "paused": "Покрытие на паузе", "unavailable": "Покрытие недоступно", "error": "Ошибка покрытия"][value] ?? "Покрытие не подтверждено"
    }
    static func guardState(_ value: String, coreReachable: Bool, selected: Bool,
                           statusVerified: Bool, armed: Bool) -> String {
        guard coreReachable else { return "Охрана · состояние неизвестно" }
        guard selected else { return "Охрана · проект не выбран" }
        guard statusVerified else { return "Статус охраны не подтверждён" }
        if armed { return "Охрана включена" }
        return ["disarmed": "Охрана выключена", "error": "Охрана · ошибка", "preparing": "Подготовка охраны"][value] ?? "Статус охраны не подтверждён"
    }
}
