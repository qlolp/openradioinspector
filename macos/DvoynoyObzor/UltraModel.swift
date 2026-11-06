import Combine
import Foundation
import SpektrKomandy
import SwiftUI

@MainActor
final class UltraModel: ObservableObject {
    @Published var portPath = ""
    @Published var knownPorts: [String] = []
    @Published var connected = false
    @Published var connecting = false
    @Published var scanning = false
    @Published var status = "Не подключён"
    @Published var errorText: String?
    @Published var infoText = "—"
    @Published var versionText = "—"
    @Published var userNumber = "—"
    @Published var identityMatches = false
    @Published var forceDespiteIdentity = false
    @Published var threshold = "-80"
    @Published var live = "Трассы ещё нет"
    @Published var tracePath = ""
    @Published var journal = ""
    @Published var current: [PlotPoint] = []
    @Published var minimum: [PlotPoint] = []
    @Published var maximum: [PlotPoint] = []
    @Published var rows: [Exceedance] = []
    @Published var waterfall = WaterfallHistory()
    @Published var waterfallRevision = 0

    private let makePort: () -> any InstrumentConnection
    private var port: (any InstrumentConnection)?

    init(makePort: @escaping () -> any InstrumentConnection = { InstrumentPort() }) {
        self.makePort = makePort
    }

    private var connectTask: Task<Void, Never>?
    private var scanTask: Task<Void, Never>?
    private var stopRequested = false
    private var notes: [String] = []

    var thresholdDbm: Double? {
        parseUserNumber(threshold)
    }

    var aboveInputCeiling: Bool {
        guard let value = thresholdDbm else { return false }
        return value > UltraInputCeiling.recommendedAutoAttenuatorDbm
    }

    func refreshPorts() {
        knownPorts = SerialPort.availablePaths()
        if portPath.isEmpty {
            if let modem = knownPorts.first(where: { $0.contains("usbmodem") }) {
                portPath = modem
            } else if let first = knownPorts.first {
                portPath = first
            }
        }
    }

    func connect() {
        errorText = nil
        identityMatches = false
        forceDespiteIdentity = false
        infoText = "—"
        versionText = "—"
        userNumber = "—"
        let path = portPath
        connecting = true
        connectTask?.cancel()
        connectTask = Task { @MainActor in
            defer { self.connecting = false }
            await self.openAndIdentify(path: path)
        }
    }

    func disconnect() {
        stopRequested = true
        let connect = connectTask
        let scan = scanTask
        connectTask?.cancel()
        scanTask?.cancel()
        Task { @MainActor in
            await connect?.value
            await scan?.value
            if let port = self.port {
                await port.close()
            }
            self.port = nil
            self.connected = false
            self.scanning = false
            self.connecting = false
            self.status = "Отключён"
        }
    }

    func startOverview() {
        errorText = nil
        guard connected, port != nil else {
            errorText = SessionProblem.notConnected.errorDescription
            return
        }
        guard identityMatches || forceDespiteIdentity else {
            errorText = "Ответ не похож на tinySA Ultra из снимка консоли. Обзор не запущен. Если это нужный прибор, отметьте продолжение."
            return
        }
        guard let level = thresholdDbm else {
            errorText = SessionProblem.badNumber("Порог").errorDescription
            return
        }
        stopRequested = false
        scanning = true
        current = []
        minimum = []
        maximum = []
        rows = []
        waterfall = WaterfallHistory()
        waterfallRevision = 0
        live = "Запуск свипа…"
        scanTask?.cancel()
        let activePort = port
        let versionSnapshot = versionText
        scanTask = Task { @MainActor in
            defer { self.scanning = false }
            await self.runOverview(port: activePort, threshold: level, version: versionSnapshot)
            await self.refreshConnectionState()
        }
    }

    func stopOverview() {
        stopRequested = true
        let scan = scanTask
        scanTask?.cancel()
        Task { @MainActor in
            await scan?.value
            self.scanning = false
            self.live = "Обзор остановлен"
            await self.refreshConnectionState()
        }
    }

    private func openAndIdentify(path: String) async {
        status = "Подключение…"
        if let port {
            await port.close()
            self.port = nil
        }
        let target = makePort()
        do {
            try await target.open(path: path, baud: UltraPortBaud.termiosPlaceholder)
            self.port = target
            let info = try await transact(
                target,
                UltraCommand(text: UltraSessionScript.infoText),
                maximum: UltraReadTiming.setupMaximumSeconds
            )
            let version = try await transact(
                target,
                UltraCommand(text: UltraSessionScript.versionText),
                maximum: UltraReadTiming.setupMaximumSeconds
            )
            let deviceId = try await transact(
                target,
                UltraCommand(text: UltraSessionScript.userNumberText),
                maximum: UltraReadTiming.setupMaximumSeconds
            )
            infoText = clipReply(info, limit: 120)
            versionText = clipReply(version, limit: 120)
            userNumber = clipReply(deviceId, limit: 80)
            identityMatches = UltraIdentity.matchesConsoleSnapshot(info: info, version: version)
            connected = true
            status = "Сеанс открыт"
            note("tinySA Ultra подключен (\(path))")
        } catch is CancellationError {
            await target.close()
            self.port = nil
            connected = false
            status = "Подключение отменено"
        } catch {
            await target.close()
            self.port = nil
            connected = false
            status = "Ошибка подключения"
            errorText = describeSession(error)
        }
    }

    private func runOverview(
        port: (any InstrumentConnection)?,
        threshold: Double,
        version: String
    ) async {
        guard let port else {
            errorText = SessionProblem.notConnected.errorDescription
            return
        }
        do {
            for command in UltraSessionScript.setupCommands() {
                if stopRequested || Task.isCancelled { return }
                _ = try await transact(port, command, maximum: UltraReadTiming.setupMaximumSeconds)
            }
            let folder = try tracesFolder()
            let fileURL = folder.appendingPathComponent(
                TraceClock.sessionFilename(instrument: "tinysa", date: Date())
            )
            tracePath = fileURL.path
            var envelope = TraceEnvelope()
            var found: [Exceedance] = []
            var stored: [UltraSweep] = []
            var sweepIndex = 0
            while !stopRequested && !Task.isCancelled {
                sweepIndex += 1
                let reply = try await transact(
                    port,
                    UltraSessionScript.scanCommand(),
                    maximum: UltraReadTiming.scanMaximumSeconds
                )
                let parsed = UltraScanParser.parse(reply)
                if parsed.points.isEmpty {
                    throw SessionProblem.unparsedScan(clipReply(reply, limit: 240))
                }
                let bins = parsed.points.map { LevelBin(frequency: $0.hertz, level: $0.dbm) }
                envelope.absorb(bins)
                waterfall.absorb(bins)
                waterfallRevision += 1
                found = ExceedanceList.update(existing: found, bins: bins, threshold: threshold)
                stored.append(
                    UltraSweep(
                        time: TraceClock.stamp(Date()),
                        frequenciesHz: parsed.points.map { String($0.hertz) },
                        levelsDbm: parsed.points.map(\.dbmToken),
                        unparsed: parsed.unparsedLines
                    )
                )
                current = plotHz(bins)
                minimum = plotHz(envelope.minimumSeries())
                maximum = plotHz(envelope.maximumSeries())
                rows = found
                live = "Свип \(sweepIndex) · точек \(parsed.points.count)"
                let document = UltraTraceDocument(version: clipReply(version, limit: 500), sweeps: stored)
                do {
                    try TraceArchive.encode(document).write(to: fileURL, options: .atomic)
                } catch {
                    errorText = SessionProblem.cannotWriteTrace(error.localizedDescription).errorDescription
                }
            }
        } catch is CancellationError {
            return
        } catch {
            errorText = describeSession(error)
        }
    }

    private func transact(
        _ port: any InstrumentConnection,
        _ command: UltraCommand,
        maximum: TimeInterval
    ) async throws -> String {
        try await port.send(command.frame())
        let reply = try await port.readUntilQuiet(
            quiet: UltraReadTiming.quietSeconds,
            maximum: maximum,
            label: command.text
        )
        note("\(command.text) → \(clipReply(reply, limit: 240))")
        if reply.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw SessionProblem.emptyDeviceReply(command.text)
        }
        return reply
    }

    private func refreshConnectionState() async {
        if let port, !(await port.isOpen) {
            self.port = nil
            connected = false
            status = "Отключён"
            errorText = SessionProblem.incompleteReply("Порт был закрыт из-за прерванного ответа").errorDescription
        }
    }

    private func note(_ line: String) {
        notes.append(line)
        if notes.count > 30 {
            notes.removeFirst(notes.count - 30)
        }
        journal = notes.joined(separator: "\n")
    }

    private func plotHz(_ bins: [LevelBin]) -> [PlotPoint] {
        bins.map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
    }
}
