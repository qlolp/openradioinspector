import Foundation
import SpektrKomandy

enum NativeDutySource: String, CaseIterable {
    case tinySA = "tinysa", rfExplorer = "rf_explorer", ardv1
    var title: String {
        switch self {
        case .tinySA: "tinySA Ultra"
        case .rfExplorer: PlatformRFExplorerConfiguration.device
        case .ardv1: "AOR AR-DV1"
        }
    }
}

enum NativeDutyAcquisition: Equatable {
    case tinySA(port: String, grid: PlatformSerialReceiveConfiguration, secondary: String?)
    case rfExplorer(PlatformRFExplorerConfiguration, secondary: String?)
    case ardv1(port: String, grid: PlatformARDV1ScopeConfiguration)

    func create(client: PlatformClient, name: String,
                beforePosting: @escaping @Sendable () async throws -> Void) async throws -> PlatformSession {
        switch self {
        case let .tinySA(port, grid, secondary):
            return try await client.createSerialSession(name: name, tinySAPort: port, ardv1Port: secondary ?? "",
                receive: grid, monitorReceiverProfile: secondary != nil, beforePosting: beforePosting)
        case let .rfExplorer(config, secondary):
            return try await client.createRFExplorerSession(config, name: name, receiverPort: secondary, beforePosting: beforePosting)
        case let .ardv1(port, grid):
            return try await client.createARDV1Scope(port: port, configuration: grid, name: name, beforePosting: beforePosting)
        }
    }
}

struct NativeRCPreparationDraft {
    var name = "Радиомониторинг", site = "Объект проверки"
    var source = NativeDutySource.tinySA
    var tinySAPort = "", receiverPort = ""
    var startHz = "88000000", stopHz = "108000000"
    static let defaultReceiverStepKHz = FrequencyText.formatKHzRussian(fromHertz: 10_000)
    var receiverStepKHz = Self.defaultReceiverStepKHz
    var points = 201, rbwHz = 10_000
    var tinyUsesGrid = true
    var explorer = NativeRFExplorerDraft()

    private func port(_ value: String) throws -> String {
        let path = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty, path.utf8.count <= 1024, !path.contains("://"), !path.contains("\0") else {
            throw PlatformError.invalidCommand("Укажите физический последовательный порт на узле ядра.")
        }
        return path
    }

    private func hertz(_ text: String) throws -> Int {
        guard !text.isEmpty, text.utf8.allSatisfy({ (48...57).contains($0) }), let value = Int(text) else {
            throw PlatformError.invalidCommand("Частоты и шаг задаются целыми Гц.")
        }
        return value
    }

    func receiverGrid() throws -> PlatformARDV1ScopeConfiguration {
        guard let hertzValue = FrequencyText.hertz(fromKHz: receiverStepKHz), let step = Int(exactly: hertzValue) else {
            throw PlatformError.invalidCommand("Введите шаг в кГц с точностью до 1 Гц. Минимальный шаг AR-DV1 — 0,01 кГц.")
        }
        return try PlatformARDV1ScopeConfiguration(startHz: hertz(startHz), stopHz: hertz(stopHz), stepHz: step, cyclic: true)
    }

    mutating func restoreReceiverGrid(_ settings: MonitoringValue) {
        guard let start = settings["start_hz"].integer, let stop = settings["stop_hz"].integer,
              let step = settings["step_hz"].integer, step > 0 else { return }
        startHz = String(start); stopHz = String(stop)
        receiverStepKHz = FrequencyText.formatKHzRussian(fromHertz: step)
    }

    func validatedPorts() throws -> (primary: String, secondary: String?) {
        for text in [name, site] {
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.unicodeScalars.count <= 200 else {
                throw PlatformError.invalidCommand("Название и объект должны содержать 1…200 символов.")
            }
        }
        if source == .ardv1 { return (try port(receiverPort), nil) }
        let primary = try port(source == .tinySA ? tinySAPort : explorer.port)
        let secondary = receiverPort.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : try port(receiverPort)
        func normalized(_ serial: String) -> String {
            serial.uppercased().wholeMatch(of: /COM[0-9]+/) != nil ? serial.uppercased() : serial
        }
        guard !PlatformExactID.same(normalized(primary), secondary.map(normalized)) else { throw PlatformError.invalidCommand("Выберите два разных порта узла.") }
        return (primary, secondary)
    }

    func tinyScanBand() throws -> ScanBandDraft {
        _ = try hertz(startHz)
        _ = try hertz(stopHz)
        var band = ScanBandDraft()
        band.id = "duty"; band.name = name
        band.usesGrid = true; band.start = startHz; band.stop = stopHz
        band.rbw = String(rbwHz); band.revisit = "3600"
        guard band.payload != nil else {
            throw PlatformError.invalidCommand("Выберите целое число шагов 10 кГц в пределах 0,1–1300 МГц и RBW 10 или 30 кГц.")
        }
        return band
    }

    func scanPlan() throws -> MonitoringValue? {
        guard source == .tinySA, tinyUsesGrid else { return nil }
        guard let band = try tinyScanBand().payload else { throw PlatformError.invalidCommand("Проверьте сетку tinySA.") }
        return .object(["name": .string(name), "bands": .array([band])])
    }

    func validated() throws -> NativeDutyAcquisition {
        let (primary, secondary) = try validatedPorts()
        if source == .ardv1 { return .ardv1(port: primary, grid: try receiverGrid()) }
        if source == .rfExplorer { return .rfExplorer(try explorer.configuration(), secondary: secondary) }
        if tinyUsesGrid, let count = try tinyScanBand().gridPoints {
            let first = try PlatformSerialReceiveConfiguration(scanPlanStartHz: hertz(startHz), totalPoints: Int(count), rbwHz: rbwHz)
            return .tinySA(port: primary, grid: first, secondary: secondary)
        }
        let grid = try PlatformSerialReceiveConfiguration(startHz: hertz(startHz), stopHz: hertz(stopHz), points: points, rbwHz: rbwHz)
        return .tinySA(port: primary, grid: grid, secondary: secondary)
    }
    static func validateCapabilities(_ value: MonitoringValue) throws {
        guard value["candidate"]["mode"].text == "live", value["features"]["explicit_serial_adapters"].bool == true,
              value["features"]["monitoring_backup_reserve"].bool == true else {
            throw PlatformError.invalidResponse("Мастеру нужна live Rust-служба с serial и проверкой резерва backup. Переключение на replay или Python не выполняется.")
        }
    }
}

/// Bound to a concrete run: preparation/start clones both the session and its streams.
struct NativeDutyRun: Equatable {
    let projectID, session, stream: PlatformExactID
    let source: NativeDutySource

    init?(_ project: MonitoringValue) {
        let kind: NativeDutySource
        if let explicit = NativeDutySource(rawValue: project["primary_source_kind"].text) {
            kind = explicit
        } else if project["primary_source_kind"] == .null,
                  project["profile"]["analyzer"]["settings"]["adapter"].text == "tinysa-serial" {
            kind = .tinySA
        } else { return nil }
        let fallback = project[kind == .ardv1 ? "receiver_stream_id" : "analyzer_stream_id"].text
        let primary = project["primary_stream_id"].text
        let stream = primary.isEmpty ? fallback : primary
        guard !project["id"].text.isEmpty, !project["active_session_id"].text.isEmpty, !stream.isEmpty else { return nil }
        projectID = project.exactID; session = PlatformExactID(project["active_session_id"].text)
        self.stream = PlatformExactID(stream); source = kind
    }
}

struct NativeDutyOriginal {
    let run: NativeDutyRun
    let id, started, ended, unit, reading, readback, data: String

    static func read(run: NativeDutyRun, client: PlatformClient) async throws -> Self? {
        let session = run.session.rawValue, stream = run.stream.rawValue
        if run.source == .ardv1 {
            let panorama = try await client.receiverPanorama(sessionID: session, streamID: stream, pass: 1, columns: 2)
            guard let bin = panorama.bins.first else { return nil }
            let row = try await client.receiverOriginal(sessionID: session, streamID: stream, pass: panorama.pass,
                frequencyHz: bin.frequencyHz, observationID: bin.observationId, fromEvent: true)
            guard row["metadata"]["simulated"].bool == false, row["metadata"]["adapter"].text == "ardv1-serial",
                  row["mode"].text.uppercased() == "FM" else {
                throw PlatformError.invalidResponse("Исходное наблюдение не подтвердило физический FM-приём AR-DV1.")
            }
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            return Self(run: run, id: row["id"].text, started: row["interval"]["started_at"].text,
                ended: row["interval"]["ended_at"].text, unit: row["level_unit"].text,
                reading: "\(row["frequency_hz"].scalar) Гц · \(row["level"].scalar) s_meter_count",
                readback: "FM · проверка частоты RX · прошивка " + row["metadata"]["receiver_firmware"].text,
                data: String(decoding: try encoder.encode(row), as: UTF8.self))
        }
        let page = try await client.archiveSweepPage(sessionID: session, streamID: stream)
        guard let summary = page.items.first else { return nil }
        let sweep = try await client.archiveOriginalSweep(sessionID: session, streamID: stream, sweepID: summary.id)
        let expected = run.source == .tinySA ? "tinysa-serial" : "rf-explorer-3g-serial"
        guard sweep.settings?["simulated"] == .boolean(false), sweep.settings?["adapter"] == .string(expected) else {
            throw PlatformError.invalidResponse("Архивный свип не подтвердил физический адаптер основного прибора.")
        }
        var description = run.source.title
        if let rfe = NativeRFExplorerReadback(sweep.settings) { description += " · " + rfe.label }
        if case .string(let detector)? = sweep.settings?["detector"] { description += " · " + detector }
        if case .object(let identity)? = sweep.settings?["device_identity"], case .string(let firmware)? = identity["firmware"] {
            description += " · прошивка " + firmware
        }
        let data = zip(sweep.frequenciesHz, sweep.levels).map { "\($0) Гц · \($1) dBm" }.joined(separator: "\n")
        return Self(run: run, id: sweep.id, started: sweep.interval.startedAt, ended: sweep.interval.endedAt, unit: sweep.levelUnit,
            reading: "\(sweep.frequenciesHz.count) точек · \(sweep.levelUnit)", readback: description, data: data)
    }
}

struct NativeRCReadiness {
    let preflight: MonitoringValue
    var baselineCollected: Bool {
        NativeBaselineProgress(preflight: preflight).ready
    }
    var budgetReady: Bool {
        let storage = preflight["storage"]
        guard let rate = storage["bytes_per_second"].double, rate.isFinite, rate > 0 else { return false }
        return storage["ready"].bool == true && storage["write_probe"].bool == true && storage["backup_reserve_requested"].bool == true
            && (storage["required_retention_seconds"].integer ?? 0) >= 43_200
    }
}

struct NativeDutyScheduleDraft {
    var mode = "continuous"
    var until = Date().addingTimeInterval(8 * 3600)
    var timezone = TimeZone.current.identifier
    var weekdays: Set<Int> = [1, 2, 3, 4, 5]
    var start = "08:00", end = "18:00"
    var resume = false

    func value(now: Date = Date()) throws -> MonitoringValue {
        switch mode {
        case "continuous": return .object(["mode": .string(mode)])
        case "until":
            guard until > now else { throw PlatformError.invalidCommand("Конец дежурства должен быть в будущем.") }
            return .object(["mode": .string(mode), "until_at": .string(ISO8601DateFormatter().string(from: until))])
        case "weekly":
            func valid(_ text: String) -> Bool { text.wholeMatch(of: /([01][0-9]|2[0-3]):[0-5][0-9]/) != nil }
            guard !weekdays.isEmpty, weekdays.isSubset(of: Set(1...7)), TimeZone(identifier: timezone) != nil,
                  valid(start), valid(end), start != end else {
                throw PlatformError.invalidCommand("Выберите дни, часовой пояс и разное время начала и окончания в формате ЧЧ:ММ.")
            }
            return .object(["mode": .string(mode), "timezone": .string(timezone),
                "weekdays": .array(weekdays.sorted().map { .integer(Int64($0)) }), "start_local": .string(start), "end_local": .string(end)])
        default: throw PlatformError.invalidCommand("Неизвестный режим дежурства.")
        }
    }
}

struct NativeReceiverOriginalLink: Identifiable, Equatable {
    let session, stream, observation: PlatformExactID
    let pass: Int
    let frequencyHz: Int64
    var archivedEvent = false
    var id: PlatformExactID { observation }
}
