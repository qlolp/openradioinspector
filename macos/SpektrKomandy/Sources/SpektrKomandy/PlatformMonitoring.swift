import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum MonitoringValue: Codable, Equatable, Sendable {
    case object([String: MonitoringValue]), array([MonitoringValue]), string(String)
    case integer(Int64), number(Double), boolean(Bool), null
    public init(from decoder: Decoder) throws {
        let box = try decoder.singleValueContainer()
        if box.decodeNil() { self = .null }
        else if let x = try? box.decode(Bool.self) { self = .boolean(x) }
        else if let x = try? box.decode(Int64.self) { self = .integer(x) }
        else if let x = try? box.decode(Double.self) { self = .number(x) }
        else if let x = try? box.decode(String.self) { self = .string(x) }
        else if let x = try? box.decode([String: MonitoringValue].self) { self = .object(x) }
        else { self = .array(try box.decode([MonitoringValue].self)) }
    }
    public func encode(to encoder: Encoder) throws {
        var box = encoder.singleValueContainer()
        switch self {
        case .object(let x): try box.encode(x)
        case .array(let x): try box.encode(x)
        case .string(let x): try box.encode(x)
        case .integer(let x): try box.encode(x)
        case .number(let x): try box.encode(x)
        case .boolean(let x): try box.encode(x)
        case .null: try box.encodeNil()
        }
    }
    public subscript(_ key: String) -> MonitoringValue { if case .object(let x) = self { return x[key] ?? .null }; return .null }
    public var exactID: PlatformExactID { PlatformExactID(self["id"].text) }
    public var text: String { if case .string(let x) = self { return x }; return "" }
    public var items: [MonitoringValue] { if case .array(let x) = self { return x }; return [] }
    public var bool: Bool? { if case .boolean(let x) = self { return x }; return nil }
    public var integer: Int64? { if case .integer(let x) = self { return x }; return nil }
    public var double: Double? {
        switch self { case .integer(let x): return Double(x); case .number(let x): return x; default: return nil }
    }
    public var utcDate: Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: text) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: text)
    }
    public var scalar: String {
        switch self {
        case .string(let x): return x
        case .integer(let x): return String(x)
        case .number(let x): return String(x)
        case .boolean(let x): return x ? "да" : "нет"
        default: return "—"
        }
    }
    public var pretty: String { let e = JSONEncoder(); e.outputFormatting = [.prettyPrinted, .sortedKeys]; return (try? String(decoding: e.encode(self), as: UTF8.self)) ?? "" }
}

/// Explicit project pages retain the selected scope, including a project outside the visible page.
public struct MonitoringProjectPageState: Sendable {
    public private(set) var items: [MonitoringValue] = []
    public private(set) var selectedID = ""
    public private(set) var pageIndex = 0
    public private(set) var nextCursor = ""
    private var cursors: [String?] = [nil]
    private var pageItems: [MonitoringValue] = []
    private var loaded = false
    public init() {}

    public func cursor(for page: Int) -> String? {
        cursors.indices.contains(page) ? cursors[page] : nil
    }
    public mutating func select(_ id: String) {
        guard id.isEmpty || items.contains(where: { PlatformExactID.same($0["id"].text, id) }) else { return }
        selectedID = id
    }
    public mutating func apply(_ result: MonitoringValue, page: Int) {
        guard cursors.indices.contains(page) else { return }
        let selection = items.first { PlatformExactID.same($0["id"].text, selectedID) }
        pageIndex = page
        cursors = Array(cursors.prefix(page + 1))
        nextCursor = result["next_cursor"].text
        if !nextCursor.isEmpty { cursors.append(nextCursor) }
        pageItems = Array(result["items"].items.prefix(200))
        items = pageItems
        if let selection, !items.contains(where: { PlatformExactID.same($0["id"].text, selectedID) }) {
            items.insert(selection, at: 0)
        }
        if !loaded && selectedID.isEmpty { selectedID = items.first?["id"].text ?? "" }
        loaded = true
    }
    public mutating func created(_ project: MonitoringValue) {
        guard !project["id"].text.isEmpty else { return }
        selectedID = project["id"].text
        items = pageItems.filter { !PlatformExactID.same($0["id"].text, selectedID) }
        items.insert(project, at: 0)
        loaded = true
    }
}

public struct MonitoringHistoryQuery: Equatable, Sendable {
    public var limit: Int
    public var start: String?
    public var end: String?
    public var minHz: Int64?
    public var maxHz: Int64?
    public var eventID: String?
    public init(limit: Int = 32, start: String? = nil, end: String? = nil,
                minHz: Int64? = nil, maxHz: Int64? = nil, eventID: String? = nil) {
        self.limit = limit; self.start = start; self.end = end
        self.minHz = minHz; self.maxHz = maxHz; self.eventID = eventID
    }
    func query(after: String?) throws -> [URLQueryItem] {
        guard (1...64).contains(limit), minHz.map({ $0 >= 0 }) ?? true,
              maxHz.map({ $0 >= 0 }) ?? true,
              minHz == nil || maxHz == nil || minHz! <= maxHz! else {
            throw PlatformError.invalidCommand("История: 1…64 свипа и корректные границы Гц")
        }
        for text in [start, end].compactMap({ $0 }) {
            guard (text.hasSuffix("Z") || text.hasSuffix("+00:00")), MonitoringValue.string(text).utcDate != nil else {
                throw PlatformError.invalidCommand("Введите время UTC в формате ISO 8601 с Z")
            }
        }
        if let start, let end,
           let first = MonitoringValue.string(start).utcDate, let last = MonitoringValue.string(end).utcDate, first > last {
            throw PlatformError.invalidCommand("Начало истории позже окончания")
        }
        var result = [URLQueryItem(name: "limit", value: String(limit))]
        for (name, value) in [("after", after), ("start", start), ("end", end),
                              ("min_hz", minHz.map(String.init)), ("max_hz", maxHz.map(String.init)), ("event_id", eventID)] {
            if let value, !value.isEmpty { result.append(.init(name: name, value: value)) }
        }
        return result
    }
}

/// Core-selected display samples, retaining each sweep's own frequency grid and UTC interval.
public struct MonitoringHistoryRow: Equatable, Sendable, Identifiable {
    public let id: String
    public let sessionID: String
    public let streamID: String
    public let startedAt: Date
    public let endedAt: Date
    public let frequenciesHz: [Int64]
    public let levels: [Double]
    public let originalPointCount: Int
    public let displayOnly: Bool
    public init(_ value: MonitoringValue, raw: Bool = false) throws {
        let frequencies = value["frequencies_hz"].items.compactMap(\.integer)
        let levels = value["levels"].items.compactMap(\.double)
        guard !value["id"].text.isEmpty, !value["session_id"].text.isEmpty, !value["stream_id"].text.isEmpty,
              let started = value["interval"]["started_at"].utcDate, let ended = value["interval"]["ended_at"].utcDate,
              ended >= started, value["level_unit"].text == "dBm", !raw || !frequencies.isEmpty,
              frequencies.count == value["frequencies_hz"].items.count,
              levels.count == value["levels"].items.count, levels.count == frequencies.count,
              levels.allSatisfy(\.isFinite), frequencies.allSatisfy({ $0 >= 0 }),
              zip(frequencies, frequencies.dropFirst()).allSatisfy({ $0.0 < $0.1 }),
              raw || frequencies.count <= 320, raw || value["display_only"].bool == true else {
            throw PlatformError.invalidResponse("Некорректный свип истории dBm/UTC")
        }
        let original = value["original_point_count"].integer ?? Int64(frequencies.count)
        guard original >= Int64(frequencies.count), original <= Int64(Int.max) else {
            throw PlatformError.invalidResponse("Некорректное число исходных точек")
        }
        id = value["id"].text; sessionID = value["session_id"].text; streamID = value["stream_id"].text
        startedAt = started; endedAt = ended; frequenciesHz = frequencies; self.levels = levels
        originalPointCount = Int(original); displayOnly = !raw
    }
}

/// A bounded latest-page snapshot; historical page navigation never feeds this state.
public struct MonitoringAlertSoundState: Sendable {
    private var initialized = false
    private var seen: Set<PlatformExactID> = []
    public init() {}
    public mutating func observe(_ items: [MonitoringValue], enabled: Bool) -> Bool {
        let latest = Array(items.prefix(100))
        let shouldSound = initialized && enabled && latest.contains {
            !$0["id"].text.isEmpty && !seen.contains(PlatformExactID($0["id"].text)) && $0["acknowledged_at"] == .null
        }
        seen = Set(latest.map { $0["id"].text }.filter { !$0.isEmpty }.map { PlatformExactID($0) })
        initialized = true
        return shouldSound
    }
}

/// Follow a changed project session once; later manual archive browsing remains selected.
public struct MonitoringProjectSessionFollowState: Sendable {
    private var followed: [PlatformExactID: [PlatformExactID: PlatformExactID]] = [:]
    public init() {}
    public mutating func sessionToFollow(endpoint: String, projectID: String, activeSessionID: String) -> String? {
        let endpointKey = PlatformExactID(endpoint), projectKey = PlatformExactID(projectID), sessionKey = PlatformExactID(activeSessionID)
        guard !endpoint.isEmpty, !projectID.isEmpty, !activeSessionID.isEmpty,
              followed[endpointKey]?[projectKey] != sessionKey else { return nil }
        followed[endpointKey, default: [:]][projectKey] = sessionKey
        return activeSessionID
    }
}

/// Engineering starting values, not a hardware calibration or a detection guarantee.
public struct MonitoringDetectorSettings: Equatable, Sendable {
    public let marginDB: Double
    public let impulseDB: Double
    public let hysteresisDB: Double
    public let confirmations: Int64
    public let windowSweeps: Int64
    public let dwellSeconds: Double
    public init() {
        marginDB = 6; impulseDB = 12; hysteresisDB = 3; confirmations = 3; windowSweeps = 5; dwellSeconds = 10
    }
    public init(marginDB: Double, impulseDB: Double, hysteresisDB: Double, confirmations: Int64, windowSweeps: Int64, dwellSeconds: Double) throws {
        guard [marginDB, impulseDB, hysteresisDB, dwellSeconds].allSatisfy(\.isFinite),
              (1...60).contains(marginDB), (1...120).contains(impulseDB), impulseDB >= marginDB,
              (0...20).contains(hysteresisDB), hysteresisDB <= marginDB, (1...30).contains(dwellSeconds),
              (1...15).contains(windowSweeps), confirmations >= 1, confirmations <= windowSweeps else {
            throw PlatformError.invalidCommand("Детектор: порог 1…60 dB; импульс 1…120 dB и ≥ порога; гистерезис 0…20 dB и ≤ порога; 1 ≤ подтверждения ≤ окно ≤ 15; прослушивание 1…30 с.")
        }
        self.marginDB = marginDB; self.impulseDB = impulseDB; self.hysteresisDB = hysteresisDB
        self.confirmations = confirmations; self.windowSweeps = windowSweeps; self.dwellSeconds = dwellSeconds
    }
    public init(_ value: MonitoringValue) throws {
        guard let margin = value["margin_db"].double, let impulse = value["impulse_db"].double,
              let hysteresis = value["hysteresis_db"].double, let confirmations = value["confirmations"].integer,
              let window = value["window_sweeps"].integer, let dwell = value["dwell_seconds"].double else {
            throw PlatformError.invalidResponse("Ядро не предоставило полный набор параметров детектора с целыми счётчиками")
        }
        try self.init(marginDB: margin, impulseDB: impulse, hysteresisDB: hysteresis,
                      confirmations: confirmations, windowSweeps: window, dwellSeconds: dwell)
    }
    public var body: [String: MonitoringValue] {
        ["margin_db": .number(marginDB), "impulse_db": .number(impulseDB), "hysteresis_db": .number(hysteresisDB),
         "confirmations": .integer(confirmations), "window_sweeps": .integer(windowSweeps), "dwell_seconds": .number(dwellSeconds)]
    }
}

extension PlatformClient {
    public func monitoringProjects(after: String? = nil, limit: Int = 200) async throws -> MonitoringValue {
        guard (1...200).contains(limit) else {
            throw PlatformError.invalidCommand("Список проектов: 1…200 проектов на странице")
        }
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let after, !after.isEmpty { query.append(.init(name: "after", value: after)) }
        let result: MonitoringValue = try await get("/monitoring-projects", query: query)
        guard result["items"].items.count <= limit else {
            throw PlatformError.invalidResponse("Ядро вернуло слишком много проектов на странице")
        }
        return result
    }

    public func monitoring(_ path: String = "", body: [String: MonitoringValue]? = nil) async throws -> MonitoringValue {
        guard path.isEmpty || (path.hasPrefix("/") && !path.contains("?") && !path.contains("..")) else { throw PlatformError.connection("Некорректный путь проекта") }
        if let body { return try await post("/monitoring-projects" + path, body: body) }
        return try await get("/monitoring-projects" + path)
    }

    public func coreCapabilities() async throws -> MonitoringValue { try await get("/capabilities") }

    public func monitoringProject(
        _ projectID: String, resource: String = "", body: [String: MonitoringValue]? = nil
    ) async throws -> MonitoringValue {
        guard !projectID.isEmpty,
              ["", "preflight", "baselines", "analyze", "allowed", "events", "configure", "arm", "disarm", "status"].contains(resource) else {
            throw PlatformError.invalidCommand("Нужны проект и известный ресурс наблюдения")
        }
        let path = "/monitoring-projects/" + Self.segment(projectID) + (resource.isEmpty ? "" : "/" + resource)
        if let body {
            return try await post(path, body: body, timeoutInterval: resource == "disarm" ? 45 : 10)
        }
        return try await get(path)
    }

    public func monitoringEvent(projectID: String, eventID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !eventID.isEmpty else {
            throw PlatformError.invalidCommand("Нужны проект и событие")
        }
        return try await get("/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))")
    }

    public func monitoringControl(projectID: String, action: String, body: [String: MonitoringValue]) async throws -> MonitoringValue {
        guard !projectID.isEmpty,
              ["manual/acquire", "manual/renew", "manual/return", "manual/tune", "search/start", "search/mark"].contains(action) else {
            throw PlatformError.invalidCommand("Неизвестное действие ручного управления")
        }
        return try await post("/monitoring-projects/\(Self.segment(projectID))/" + action, body: body, timeoutInterval: 20)
    }

    public func monitoringSearchStatus(projectID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        let result: MonitoringValue = try await get("/monitoring-projects/\(Self.segment(projectID))/search/status")
        guard result["series"].items.count <= 256 else { throw PlatformError.invalidResponse("Слишком длинный ряд поиска") }
        return result
    }

    public func monitoringHistory(projectID: String, query: MonitoringHistoryQuery = .init(), after: String? = nil) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        let result: MonitoringValue = try await get("/monitoring-projects/\(Self.segment(projectID))/history", query: query.query(after: after))
        guard result["items"].items.count <= query.limit else { throw PlatformError.invalidResponse("Слишком большая страница истории") }
        for row in result["items"].items { _ = try MonitoringHistoryRow(row) }
        return result
    }

    public func monitoringHistorySweep(projectID: String, sweepID: String, comparison: Bool = false) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !sweepID.isEmpty else { throw PlatformError.invalidCommand("Выберите свип проекта") }
        let result: MonitoringValue = try await get("/monitoring-projects/\(Self.segment(projectID))/history/\(Self.segment(sweepID))" + (comparison ? "/comparison" : ""))
        if comparison {
            guard PlatformExactID.same(result["sweep_id"].text, sweepID), result["compatible"].bool != nil,
                  result["display_only"].bool == true, result["items"].items.count <= 320 else {
                throw PlatformError.invalidResponse("Некорректное сравнение с эталоном")
            }
            for point in result["items"].items {
                guard point["frequency_hz"].integer != nil,
                      ["baseline_p50_dbm", "baseline_p95_dbm", "current_dbm", "delta_db"].allSatisfy({ point[$0].double?.isFinite == true }) else {
                    throw PlatformError.invalidResponse("Некорректные точки сравнения")
                }
            }
        } else {
            guard PlatformExactID.same(try MonitoringHistoryRow(result, raw: true).id, sweepID) else {
                throw PlatformError.invalidResponse("Ядро вернуло другой свип")
            }
        }
        return result
    }

    public func monitoringEventSources(projectID: String, eventID: String, limit: Int = 32, after: String? = nil) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !eventID.isEmpty, (1...64).contains(limit) else {
            throw PlatformError.invalidCommand("Нужны событие и размер страницы 1…64")
        }
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let after, !after.isEmpty { query.append(.init(name: "after", value: after)) }
        let result: MonitoringValue = try await get("/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))/sources", query: query)
        guard result["items"].items.count <= limit else { throw PlatformError.invalidResponse("Слишком большая страница источников") }
        return result
    }

    /// Settings never enter the diagnostic JSON path. Unknown fields, including secrets, are discarded.
    public func monitoringSettings(projectID: String, body: [String: MonitoringValue]? = nil) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        do {
            let path = "/monitoring-projects/\(Self.segment(projectID))/settings"
            let result: MonitoringValue
            if var body {
                if case .object(var webhook)? = body["webhook"], webhook["secret"] == .string("") {
                    webhook.removeValue(forKey: "secret"); body["webhook"] = .object(webhook)
                }
                result = try await post(path, body: body)
            } else { result = try await get(path) }
            guard result["autostart"].bool != nil, result["archive"]["minimum_free_bytes"].integer != nil,
                  result["archive"]["minimum_retention_seconds"].integer != nil,
                  result["webhook"]["enabled"].bool != nil, result["webhook"]["secret_configured"].bool != nil,
                  case .string(let url) = result["webhook"]["url"] else {
                throw PlatformError.invalidResponse("Некорректные настройки проекта")
            }
            let detectorKeys = ["margin_db", "impulse_db", "hysteresis_db", "confirmations", "window_sweeps", "dwell_seconds"]
            let detector = Dictionary(uniqueKeysWithValues: detectorKeys.compactMap { key -> (String, MonitoringValue)? in
                let value = result["detector"][key]
                if ["confirmations", "window_sweeps"].contains(key), value.integer == nil { return nil }
                return value.double?.isFinite == true ? (key, value) : nil
            })
            // The API contract returns only public webhook configuration, never the credential.
            return .object(["autostart": result["autostart"],
                "archive": .object(["minimum_free_bytes": result["archive"]["minimum_free_bytes"],
                                    "minimum_retention_seconds": result["archive"]["minimum_retention_seconds"]]),
                "webhook": .object(["enabled": result["webhook"]["enabled"], "url": .string(url),
                                     "secret_configured": result["webhook"]["secret_configured"]]),
                "detector": .object(detector)])
        } catch is CancellationError { throw CancellationError() }
        catch let error as PlatformError {
            // Neither a reflected server message nor the request body may expose the credential.
            if case .http(let status, _, _) = error {
                throw PlatformError.http(status: status, code: "monitoring_settings_failed",
                    message: "Не удалось прочитать или сохранить настройки (HTTP \(status)). Проверьте поля и подключение.")
            }
            throw PlatformError.connection("Не удалось прочитать или сохранить настройки проекта")
        } catch { throw PlatformError.connection("Не удалось прочитать или сохранить настройки проекта") }
    }

    public func monitoringStorage(projectID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        return try await get("/monitoring-projects/\(Self.segment(projectID))/storage")
    }

    public func monitoringSavePreparation(projectID: String, antennaNotes: String, receivePathConfirmed: Bool, locationConfirmed: Bool) async throws -> MonitoringValue {
        let notes = antennaNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !projectID.isEmpty, !notes.isEmpty, notes.unicodeScalars.count <= 1000 else {
            throw PlatformError.invalidCommand("Выберите проект и опишите антенну/тракт: 1…1000 символов")
        }
        let body: [String: MonitoringValue] = [
            "antenna_notes": .string(notes), "receive_path_confirmed": .boolean(receivePathConfirmed),
            "location_confirmed": .boolean(locationConfirmed)]
        return try await post("/monitoring-projects/\(Self.segment(projectID))/preparation", body: body)
    }

    public func monitoringStartPreparation(projectID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        return try await post("/monitoring-projects/\(Self.segment(projectID))/preparation/start", body: [String: MonitoringValue](), timeoutInterval: 20)
    }

    public func monitoringStopPreparation(projectID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty else { throw PlatformError.invalidCommand("Выберите проект") }
        return try await post("/monitoring-projects/\(Self.segment(projectID))/preparation/stop", body: [String: MonitoringValue](), timeoutInterval: 45)
    }

    public func monitoringAlerts(projectID: String, limit: Int = 100, after: String? = nil) async throws -> MonitoringValue {
        guard !projectID.isEmpty, (1...100).contains(limit) else { throw PlatformError.invalidCommand("Выберите проект и размер страницы 1…100") }
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let after, !after.isEmpty { query.append(.init(name: "after", value: after)) }
        let result: MonitoringValue = try await get("/monitoring-projects/\(Self.segment(projectID))/alerts", query: query)
        guard result["items"].items.count <= limit,
              result["items"].items.allSatisfy({ !$0["id"].text.isEmpty }) else {
            throw PlatformError.invalidResponse("Некорректная страница оповещений")
        }
        return result
    }

    public func monitoringAcknowledgeAlert(projectID: String, alertID: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !alertID.isEmpty else { throw PlatformError.invalidCommand("Выберите оповещение проекта") }
        return try await post("/monitoring-projects/\(Self.segment(projectID))/alerts/\(Self.segment(alertID))/ack", body: [String: MonitoringValue]())
    }

    public func monitoringReviewEvent(projectID: String, eventID: String, disposition: String, comment: String) async throws -> MonitoringValue {
        guard !projectID.isEmpty, !eventID.isEmpty, ["unreviewed", "authorized", "false_positive", "investigate"].contains(disposition) else {
            throw PlatformError.invalidCommand("Выберите событие и решение оператора")
        }
        let body: [String: MonitoringValue] = ["disposition": .string(disposition), "comment": .string(comment)]
        return try await post("/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))/review", body: body)
    }

    /// A complete, bounded WAV artifact from the core, never a local capture device.
    public func monitoringEventAudio(
        projectID: String, eventID: String, maximumBytes: Int = 16 * 1024 * 1024
    ) async throws -> Data {
        guard !projectID.isEmpty, !eventID.isEmpty, (44...(32 * 1024 * 1024)).contains(maximumBytes) else {
            throw PlatformError.invalidCommand("Нужны проект, событие и допустимый предел WAV")
        }
        var request = URLRequest(url: try baseURL.endpoint(
            path: "/monitoring-projects/\(Self.segment(projectID))/events/\(Self.segment(eventID))/audio"))
        request.httpMethod = "GET"
        request.timeoutInterval = 15
        request.setValue("audio/wav", forHTTPHeaderField: "Accept")
        // The extra byte distinguishes a complete artifact from a truncated prefix.
        let (data, response) = try await receivePrefix(request, maxBytes: maximumBytes + 1)
        try Task.checkCancellation()
        guard data.count <= maximumBytes else { throw PlatformError.responseTooLarge }
        guard let http = response as? HTTPURLResponse else {
            throw PlatformError.invalidResponse("Нет ответа на запрос WAV")
        }
        guard http.statusCode == 200 else {
            if let error = try? JSONDecoder().decode(MonitoringValue.self, from: data),
               !error["error"]["message"].text.isEmpty {
                throw PlatformError.http(status: http.statusCode, code: error["error"]["code"].text,
                                         message: error["error"]["message"].text)
            }
            throw PlatformError.http(status: http.statusCode, code: "http_error", message: "HTTP \(http.statusCode)")
        }
        let mime = (http.value(forHTTPHeaderField: "Content-Type") ?? "").lowercased().split(separator: ";").first.map(String.init) ?? ""
        guard ["audio/wav", "audio/x-wav", "audio/wave"].contains(mime),
              data.count >= 44, data.prefix(4) == Data("RIFF".utf8),
              data.subdata(in: 8..<12) == Data("WAVE".utf8) else {
            throw PlatformError.invalidResponse("Сервер не вернул WAV RIFF/WAVE")
        }
        let declaredSize = data[4..<8].enumerated().reduce(UInt64(0)) { $0 | (UInt64($1.element) << ($1.offset * 8)) } + 8
        guard declaredSize == UInt64(data.count) else {
            throw PlatformError.invalidResponse("Неполный WAV: размер RIFF не совпадает")
        }
        return data
    }
}
