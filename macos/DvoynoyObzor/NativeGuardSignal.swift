import Foundation
import SpektrKomandy

struct NativeGuardSignal: Identifiable {
    let value: MonitoringValue
    var id: PlatformExactID { value.exactID }

    var recognition: String {
        switch value["recognition"]["state"].text {
        case "new": "Новый"
        case "similar": "Похож на известный"
        case "changed": "Известный изменился"
        default: "Нет классификации"
        }
    }

    var receiver: String {
        if isReceiverPrimary { return "Основной источник AR-DV1" }
        let analysis = value["analysis"]
        let state = analysis["state"].text
        let title = receiverTitle(analysis, state: state)
        return receiverSimulated ? "\(title) · симуляция" : title
    }

    var receiverSimulated: Bool {
        let analysis = value["analysis"]
        return analysis["verification_evidence"]["simulated"].bool == true ||
            analysis["receiver_status"].items.last?["metadata"]["simulated"].bool == true
    }

    private func receiverTitle(_ analysis: MonitoringValue, state: String) -> String {
        if analysis["verification_evidence"]["reason"].text == "event_reviewed" { return "Проверка отменена оператором" }
        if state == "queued" { return "В очереди приёмника" }
        switch analysis["verification"].text {
        case "observed_both": return receiverSimulated ? "Оба наблюдения" : "Отмечен обоими приборами"
        case "not_observed": return "Приёмник не обнаружил"
        case "unsupported_frequency": return "За пределами диапазона приёмника"
        case "inconclusive" where !["expired", "interrupted", "failed", "no_observations"].contains(state):
            return "Приём не подтверждён"
        default: break
        }
        return switch state {
        case "observed": "Есть наблюдение AR-DV1"
        case "not_present_at_receiver": "Приёмник не обнаружил"
        case "unsupported_frequency": "За пределами диапазона приёмника"
        case "no_observations": "Нет наблюдений приёмника"
        case "expired": "Проверка просрочена"
        case "interrupted": "Исследование прервано"
        case "failed": "Ошибка исследования"
        default: "Нет результата приёмника"
        }
    }

    var receiverReason: String? {
        let analysis = value["analysis"]
        let reason = analysis["verification_evidence"]["reason"].text
        if analysis["state"].text == "expired" || reason == "event_expired" {
            return "Возраст события превысил окно сопоставления."
        }
        return switch reason {
        case "coincident_receipt_bound_observations": "Наблюдения tinySA и AR-DV1 совпали по времени после подтверждённой настройки."
        case "receiver_squelch_closed_during_tinysa_detection": "При срабатывании tinySA шумодав AR-DV1 оставался закрыт."
        case "receiver_activity_ambiguous": "Активность по данным AR-DV1 неоднозначна."
        case "no_coincident_tinysa_source": "В интервале приёма нет подходящего исходного свипа tinySA."
        case "missing_receiver_readback": "Ядро не получило подходящее наблюдение с подтверждённой настройкой AR-DV1."
        case "outside_receiver_range": "Частота события вне поддерживаемого диапазона AR-DV1."
        case "event_time_in_future": "Время события позже момента проверки."
        case "verification_pending": "Ожидается результат исследования."
        case "control_context_revoked": "Настройка перестала действовать до завершения проверки."
        case "event_reviewed": nil
        default: [analysis["reason"].text, analysis["error"].text].first { !$0.isEmpty }
        }
    }

    var detectorKind: String {
        switch value["kind"].text {
        case "new_signal": "Новый сигнал"
        case "persistent": "Устойчивое превышение"
        case "impulse": "Импульсное превышение"
        case "broadband_change": "Широкополосное изменение"
        case "dense_change": "Превышение в нескольких участках полосы"
        case "authorized_changed": "Изменение разрешённого сигнала"
        default: "Вид срабатывания неизвестен"
        }
    }

    var detectorState: String {
        switch value["state"].text {
        case "open": "Открыто"
        case "closed": "Завершено"
        case "interrupted": "Прервано"
        default: "Состояние неизвестно"
        }
    }

    var observationSource: NativeReceiverOriginalLink? {
        guard isReceiverPrimary else { return nil }
        let candidates = [value["peak_source"]] + value["source_refs"].items.reversed()
        for ref in candidates {
            let session = ref["session_id"].text, stream = ref["stream_id"].text, id = ref["observation_id"].text
            guard !id.isEmpty, !stream.isEmpty, !session.isEmpty,
                  value["run_session_id"].text.isEmpty || PlatformExactID.same(session, value["run_session_id"].text),
                  let n = ref["scan_pass"].integer, let pass = Int(exactly: n), pass > 0,
                  let hz = ref["frequency_hz"].integer, (100_000...1_300_000_000).contains(hz) else { continue }
            return .init(session: PlatformExactID(session), stream: PlatformExactID(stream),
                         observation: PlatformExactID(id), pass: pass, frequencyHz: hz, archivedEvent: true)
        }
        return nil
    }
    var isReceiverPrimary: Bool { value["level_unit"].text == "s_meter_count" }
    var peakReading: String { Self.level(value["peak_level"], unit: isReceiverPrimary ? "s_meter_count" : "dBm") }
    var excessReading: String {
        isReceiverPrimary ? Self.level(value["excess_counts"], unit: "отсч. S-метра") : Self.level(value["peak_excess_db"], unit: "дБ")
    }

    var receiverSource: NativeGuardSignalSource? {
        let source = NativeGuardSignalSource(value["analysis"]["verification_evidence"]["analyzer"])
        return source.flatMap { matchesRunSession($0) ? $0 : nil }
    }

    var peakSource: NativeGuardSignalSource? {
        let source = NativeGuardSignalSource(value["peak_source"])
        return source.flatMap { matchesRunSession($0) ? $0 : nil }
    }

    var source: NativeGuardSignalSource? {
        receiverSource ?? peakSource ?? value["source_refs"].items.reversed().lazy.compactMap(NativeGuardSignalSource.init)
            .first(where: matchesRunSession)
    }

    private func matchesRunSession(_ source: NativeGuardSignalSource) -> Bool {
        let sessionID = value["run_session_id"].text
        return sessionID.isEmpty || PlatformExactID.same(source.sessionID.rawValue, sessionID)
    }

    static func frequency(_ value: MonitoringValue) -> String {
        value.integer.map(NativeAnalyzerDraft.mhz) ?? "—"
    }

    static func level(_ value: MonitoringValue, unit: String) -> String {
        guard let number = value.double, number.isFinite else { return "—" }
        return String(format: "%.1f %@", number, unit)
    }
}

struct NativeGuardSignalSource: Equatable {
    let sessionID: PlatformExactID
    let streamID: PlatformExactID
    let sweepID: PlatformExactID

    init?(_ value: MonitoringValue) {
        let session = value["session_id"].text, stream = value["stream_id"].text, sweep = value["sweep_id"].text
        guard !session.isEmpty, !stream.isEmpty, !sweep.isEmpty else { return nil }
        sessionID = PlatformExactID(session)
        streamID = PlatformExactID(stream)
        sweepID = PlatformExactID(sweep)
    }
}

struct NativeGuardSignalQueue {
    struct Position: Equatable {
        let page: Int
        let after: PlatformExactID
    }

    private struct Page {
        let rows: [NativeGuardSignal]
        var next: PlatformExactID?

        init(_ value: MonitoringValue = .null) {
            rows = value["items"].items.map(NativeGuardSignal.init)
            let next = value["next_cursor"].text
            self.next = next.isEmpty ? nil : PlatformExactID(next)
        }
    }

    private var latest = Page()
    private var exhaustedLatest: PlatformExactID?
    private var history: Page?
    private var backPositions: [Position] = []
    private(set) var position: Position?
    private(set) var selectedID: PlatformExactID?
    private(set) var unread: Set<PlatformExactID> = []
    private(set) var refreshedAt: Date?

    var isLatest: Bool { position == nil }
    var rows: [NativeGuardSignal] { history?.rows ?? latest.rows }
    var selected: NativeGuardSignal? { rows.first { $0.id == selectedID } }
    var selectedPollValue: MonitoringValue {
        latest.rows.first { $0.id == selectedID }?.value ?? selected?.value ?? .null
    }
    var olderPosition: Position? {
        let cursor = isLatest ? latest.next : history?.next
        return cursor.map { Position(page: (position?.page ?? 1) + 1, after: $0) }
    }
    var newerPosition: Position? { backPositions.last }

    mutating func update(_ value: MonitoringValue, at date: Date) {
        var next = Page(value)
        let ids = Set(next.rows.map(\.id))
        if refreshedAt != nil { unread.formUnion(ids.subtracting(latest.rows.map(\.id))) }
        unread.formIntersection(ids)
        if next.next == exhaustedLatest { next.next = nil } else { exhaustedLatest = nil }
        latest = next
        if isLatest { reconcileSelection() }
        refreshedAt = date
    }

    mutating func showHistory(_ value: MonitoringValue, at target: Position) {
        let next = Page(value)
        let activePage = position?.page ?? 1
        if next.rows.isEmpty, target.page > activePage {
            // The core may return the final record as a cursor even on the final page.
            if isLatest, latest.next == target.after {
                exhaustedLatest = target.after
                latest.next = nil
            } else if history?.next == target.after {
                history?.next = nil
            }
            return
        }
        if let position, target.page > activePage {
            backPositions.append(position)
            // Preserve a bounded back trail without limiting how far the operator may browse.
            if backPositions.count > 32 { backPositions.removeFirst() }
        } else if target.page < activePage {
            backPositions.removeAll { $0.page >= target.page }
        }
        position = target
        history = next
        reconcileSelection()
    }

    mutating func showLatest() {
        position = nil
        history = nil
        backPositions = []
        reconcileSelection()
    }

    mutating func select(_ id: PlatformExactID?) {
        guard id == nil || rows.contains(where: { $0.id == id }) else { return }
        selectedID = id
    }

    mutating func markSelectedRead(_ id: PlatformExactID) {
        guard selectedID == id, rows.contains(where: { $0.id == id }) else { return }
        unread.remove(id)
    }

    private mutating func reconcileSelection() {
        if let selectedID, !rows.contains(where: { $0.id == selectedID }) { self.selectedID = nil }
    }
}

enum NativeGuardSignalRequest: Hashable {
    case inbox, inboxPage, detail, original, catalog, confirm, review, rechecks
}

extension MonitoringKnownSignalKind {
    var title: String {
        switch self {
        case .broadcast: "Вещание"
        case .aviation: "Авиация"
        case .maritime: "Морская связь"
        case .rail: "Железнодорожная связь"
        case .municipal: "Муниципальная связь"
        case .cellular: "Сотовая связь"
        case .shortRange: "Маломощные устройства"
        case .wifi: "Wi-Fi"
        case .bluetooth: "Bluetooth"
        case .remoteControl: "Дистанционное управление"
        case .satelliteNavigation: "Спутниковая навигация"
        case .other: "Другое"
        case .unknown: "Не определён"
        }
    }
}
