import Foundation
import SpektrKomandy

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

/// A row belongs to the selected session, analyzer and exact revision of its scan plan.
struct NativeBandCoverage: Identifiable {
    let band: MonitoringValue
    let report: MonitoringValue
    var id: PlatformExactID { band.exactID }

    static func rows(status: MonitoringValue, sessionID: String) -> [Self] {
        let project = status["project"], plan = project["scan_plan"]
        let analyzer = project["analyzer_stream_id"].text
        guard !sessionID.isEmpty, PlatformExactID.same(project["active_session_id"].text, sessionID),
              !analyzer.isEmpty, !plan["id"].text.isEmpty else { return [] }
        let worker = status["workers"].items.first {
            PlatformExactID.same($0["session_id"].text, sessionID) &&
                PlatformExactID.same($0["stream_id"].text, analyzer)
        }
        let coverage = worker?["current"]["scan_coverage"] ?? .null
        let matches = PlatformExactID.same(coverage["plan_id"].text, plan["id"].text) &&
            plan["revision"].integer != nil && coverage["revision"].integer == plan["revision"].integer
        let reports = matches ? coverage["bands"].items : []
        return plan["config"]["bands"].items.map { band in
            Self(band: band, report: reports.first { $0.exactID == band.exactID } ?? .null)
        }
    }

    var state: String {
        switch report["state"].text {
        case "fresh": "Актуально"
        case "partial": "Частичное покрытие"
        case "stale": "Данные устарели"
        case "paused": "Обзор приостановлен"
        case "not_observed": "Измерений ещё нет"
        default: "Покрытие неизвестно"
        }
    }

    static func duration(_ value: MonitoringValue) -> String {
        guard let seconds = value.double, seconds.isFinite, seconds >= 0 else { return "нет данных" }
        return seconds.formatted(.number.precision(.fractionLength(0...1))) + " с"
    }

    static func quality(_ value: MonitoringValue) -> String {
        quality(status: value["status"].text, reason: value["reason"].text)
    }

    static func quality(of sweep: PlatformSweep) -> String {
        guard case .object(let fields) = sweep.settings?["measurement_qualification"],
              case .string(let status) = fields["status"] else { return "Нет сведений о качестве" }
        let reason: String
        if case .string(let value) = fields["reason"] { reason = value } else { reason = "" }
        return quality(status: status, reason: reason)
    }

    private static func quality(status: String, reason: String) -> String {
        let title: String
        switch status {
        case "synthetic": return "Учебные данные"
        case "supported": title = "В пределах подтверждённого профиля"
        case "partial": title = "Профиль подтверждён частично"
        case "unverified": title = "Профиль не подтверждён"
        default: return "Нет сведений о качестве"
        }
        if reason == "reference_required_above_800_mhz" {
            return title + " · выше 800 МГц требуется опорное измерение"
        }
        return title
    }
}
