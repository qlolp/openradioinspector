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

    func create(client: PlatformClient, name: String) async throws -> PlatformSession {
        switch self {
        case let .tinySA(port, grid, secondary):
            return try await client.createSerialSession(name: name, tinySAPort: port, ardv1Port: secondary ?? "",
                receive: grid, monitorReceiverProfile: secondary != nil)
        case let .rfExplorer(config, secondary):
            return try await client.createRFExplorerSession(config, name: name, receiverPort: secondary)
        case let .ardv1(port, grid):
            return try await client.createARDV1Scope(port: port, configuration: grid, name: name)
        }
    }
}

struct NativeRCPreparationDraft {
    var name = "Радиомониторинг", site = "Объект проверки"
    var source = NativeDutySource.tinySA
    var tinySAPort = "", receiverPort = ""
    var startHz = "88000000", stopHz = "108000000", stepHz = "10000"
    var points = 201, rbwHz = 10_000
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
        try PlatformARDV1ScopeConfiguration(startHz: hertz(startHz), stopHz: hertz(stopHz), stepHz: hertz(stepHz), cyclic: true)
    }

    func validated() throws -> NativeDutyAcquisition {
        for text in [name, site] {
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.unicodeScalars.count <= 200 else {
                throw PlatformError.invalidCommand("Название и объект должны содержать 1…200 символов.")
            }
        }
        if source == .ardv1 { return .ardv1(port: try port(receiverPort), grid: try receiverGrid()) }
        let primary = try port(source == .tinySA ? tinySAPort : explorer.port)
        let secondary = receiverPort.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : try port(receiverPort)
        guard !PlatformExactID.same(primary, secondary) else { throw PlatformError.invalidCommand("Выберите два разных порта узла.") }
        if source == .rfExplorer { return .rfExplorer(try explorer.configuration(), secondary: secondary) }
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
