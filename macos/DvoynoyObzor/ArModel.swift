import Combine
import Foundation
import SpektrKomandy
import SwiftUI

@MainActor
final class ArModel: ObservableObject {
    @Published var portPath = ""
    @Published var baud = 115_200
    @Published var knownPorts: [String] = []
    @Published var connected = false
    @Published var connecting = false
    @Published var scanning = false
    @Published var tuning = false
    @Published var status = "Не подключён"
    @Published var errorText: String?
    @Published var reHeader = "—"
    @Published var firmware = "—"
    @Published var serialNumber = "—"
    @Published var f1 = "144"
    @Published var f2 = "146"
    @Published var stepParameter = ArDv1Step.overviewDefaultParameter
    @Published var mode: ArDv1AnalogMode = .fm
    @Published var pauseMs = String(ArDv1Timing.startingPauseMilliseconds)
    @Published var threshold = "30"
    @Published var stuckLimit = String(StuckMeterWatch.startingLimit)
    @Published var squelchText = "—"
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
    private var tuneTask: Task<Void, Never>?
    private var stopRequested = false
    private var firmwareValue = "не прочитана"
    private var notes: [String] = []

    let baudRates = [115_200, 57_600, 38_400, 19_200, 9_600]

    func refreshPorts() {
        knownPorts = SerialPort.availablePaths()
    }

    func connect() {
        errorText = nil
        let path = portPath
        let rate = baud
        connecting = true
        connectTask?.cancel()
        connectTask = Task { @MainActor in
            defer { self.connecting = false }
            await self.openAndIdentify(path: path, baud: rate)
        }
    }

    func disconnect() {
        stopRequested = true
        let connect = connectTask
        let scan = scanTask
        let tune = tuneTask
        connectTask?.cancel()
        scanTask?.cancel()
        tuneTask?.cancel()
        Task { @MainActor in
            await connect?.value
            await scan?.value
            await tune?.value
            if let port = self.port {
                try? await port.send(ArDv1CommandFactory.leaveRemote().frame())
                _ = try? await port.readLine(timeout: 0.4, label: "EX")
                await port.close()
            }
            self.port = nil
            self.connected = false
            self.scanning = false
            self.connecting = false
            self.status = "Отключён"
        }
    }

    func startScan() {
        errorText = nil
        guard connected, port != nil, !tuning else {
            errorText = tuning ? "Приёмник ещё перестраивается." : SessionProblem.notConnected.errorDescription
            return
        }
        guard let start = FrequencyText.parseMHz(f1), let end = FrequencyText.parseMHz(f2) else {
            errorText = SessionProblem.badNumber("Границы F1 и F2").errorDescription
            return
        }
        guard start <= end else {
            errorText = SessionProblem.frequencyOrder.errorDescription
            return
        }
        guard FrequencyText.arFrequencyAllowed(start), FrequencyText.arFrequencyAllowed(end) else {
            errorText = SessionProblem.frequencyRange.errorDescription
            return
        }
        guard let step = ArDv1Step.all.first(where: { $0.parameter == stepParameter }) else {
            errorText = SessionProblem.badNumber("Шаг").errorDescription
            return
        }
        guard let pause = Int(pauseMs.trimmingCharacters(in: .whitespaces)), (0...5_000).contains(pause) else {
            errorText = SessionProblem.badNumber("Пауза").errorDescription
            return
        }
        guard let level = parseUserNumber(threshold) else {
            errorText = SessionProblem.badNumber("Порог").errorDescription
            return
        }
        guard let stuck = Int(stuckLimit.trimmingCharacters(in: .whitespaces)), (2...10_000).contains(stuck) else {
            errorText = SessionProblem.badNumber("Одинаковые отсчёты").errorDescription
            return
        }
        let points = FrequencyText.grid(from: start, to: end, step: step.unitsOf10Hz)
        if points.count > ScanBudget.maximumPoints {
            errorText = SessionProblem.tooManyPoints(points.count).errorDescription
            return
        }
        let request = ArScanRequest(
            f1: start,
            f2: end,
            step: step,
            mode: mode,
            pauseMs: pause,
            threshold: level,
            stuckLimit: stuck,
            grid: points,
            firmware: firmwareValue
        )
        stopRequested = false
        tuneTask?.cancel()
        scanning = true
        current = []
        minimum = []
        maximum = []
        rows = []
        waterfall = WaterfallHistory()
        waterfallRevision += 1
        scanTask?.cancel()
        scanTask = Task { @MainActor in
            await self.runScan(request)
        }
    }

    func stopScan() {
        stopRequested = true
        scanTask?.cancel()
    }

    func stopScanAndSetFrequency(units: Int64) {
        stopRequested = true
        tuning = true
        let running = scanTask
        scanTask?.cancel()
        scanTask = nil
        tuneTask?.cancel()
        tuneTask = Task { @MainActor in
            defer { self.tuning = false }
            await running?.value
            if Task.isCancelled { return }
            await self.sendFrequency(units)
        }
    }

    private func openAndIdentify(path: String, baud: Int) async {
        let opened = makePort()
        do {
            try await opened.open(path: path, baud: baud)
            port = opened
            connected = true
            status = "Порт открыт"
            try await exchange(opened, ArDv1CommandFactory.resultCodesOn().frame(), label: "RE1") { reply in
                let line = ArDv1Reply.parse(raw: reply)
                if ArDv1Reply.isRejection(line) {
                    throw SessionProblem.deviceReply(reply)
                }
                if let code = line.resultCode {
                    reHeader = "RE1 · \(code.kind)\(code.continued ? 1 : 0)"
                } else {
                    reHeader = "RE1"
                }
            }
            try await exchange(opened, ArDv1CommandFactory.readFirmware().frame(), label: "VR") { reply in
                let line = ArDv1Reply.parse(raw: reply)
                if ArDv1Reply.isRejection(line) {
                    throw SessionProblem.deviceReply(reply)
                }
                if let identity = ArDv1Reply.parseFirmware(body: line.body) {
                    firmware = identity.patternMatched ? identity.value : "\(identity.value) (формат не совпал)"
                    firmwareValue = identity.value
                } else {
                    firmware = clipReply(reply, limit: 80)
                    firmwareValue = firmware
                }
            }
            try await exchange(opened, ArDv1CommandFactory.readSerial().frame(), label: "RN") { reply in
                let line = ArDv1Reply.parse(raw: reply)
                if ArDv1Reply.isRejection(line) {
                    throw SessionProblem.deviceReply(reply)
                }
                if let identity = ArDv1Reply.parseSerial(body: line.body) {
                    serialNumber = identity.value
                } else {
                    serialNumber = clipReply(reply, limit: 80)
                }
            }
            status = "Сеанс открыт"
        } catch is CancellationError {
            return
        } catch {
            if port == nil {
                connected = false
                status = "Не подключён"
            }
            await refreshConnectionState()
            errorText = describeSession(error)
        }
    }

    private func runScan(_ request: ArScanRequest) async {
        defer { scanning = false }
        guard let port else {
            errorText = SessionProblem.notConnected.errorDescription
            return
        }
        do {
            try await sendChecked(port, ArDv1CommandFactory.selectVfoA().frame(), label: "VFO-A")
            try await sendChecked(port, ArDv1CommandFactory.analogMode(request.mode).frame(), label: "режим")
            try await sendChecked(port, ArDv1CommandFactory.step(request.step).frame(), label: "шаг")
            let folder = try tracesFolder()
            let fileURL = folder.appendingPathComponent(TraceClock.sessionFilename(instrument: "ardv1"))
            tracePath = fileURL.path
            var envelope = TraceEnvelope()
            var found: [Exceedance] = []
            var watch = StuckMeterWatch(limit: request.stuckLimit)
            var stored: [ArSweep] = []
            var sweepIndex = 0
            while !stopRequested && !Task.isCancelled {
                sweepIndex += 1
                var bins: [LevelBin] = []
                var frequencies: [String] = []
                var levels: [Int] = []
                for (index, units) in request.grid.enumerated() {
                    if stopRequested || Task.isCancelled { return }
                    let frequency = try ArDv1CommandFactory.frequency(units: units)
                    try await sendChecked(port, frequency.frame(), label: "RF")
                    if request.pauseMs > 0 {
                        try await Task.sleep(nanoseconds: UInt64(request.pauseMs) * 1_000_000)
                    }
                    if stopRequested || Task.isCancelled { return }
                    try await port.send(ArDv1CommandFactory.readLevel().frame())
                    let raw = try await port.readLine(timeout: 1.5, label: "LM")
                    note("LM \(clipReply(raw, limit: 160))")
                    let line = ArDv1Reply.parse(raw: raw)
                    if ArDv1Reply.isRejection(line) {
                        throw SessionProblem.deviceReply(raw)
                    }
                    guard let reading = ArDv1Reply.parseLevel(body: line.body) else {
                        throw SessionProblem.badLevel(clipReply(raw, limit: 180))
                    }
                    if watch.observe(reading.level) {
                        throw SessionProblem.stuckMeter(level: reading.level, count: watch.run)
                    }
                    bins.append(LevelBin(frequency: units, level: Double(reading.level)))
                    frequencies.append(FrequencyText.formatMHzMachine(from10Hz: units))
                    levels.append(reading.level)
                    squelchText = reading.squelch.title
                    current = plot10Hz(bins)
                    live = "\(index + 1) из \(request.grid.count) · \(FrequencyText.formatMHzRussian(from10Hz: units)) МГц · свип \(sweepIndex)"
                }
                envelope.absorb(bins)
                waterfall.absorb(bins)
                waterfallRevision += 1
                found = ExceedanceList.update(existing: found, bins: bins, threshold: request.threshold)
                stored.append(ArSweep(time: TraceClock.stamp(Date()), frequenciesMHz: frequencies, levels: levels))
                minimum = plot10Hz(envelope.minimumSeries())
                maximum = plot10Hz(envelope.maximumSeries())
                rows = found
                let document = ArTraceDocument(
                    f1MHz: FrequencyText.formatMHzMachine(from10Hz: request.f1),
                    f2MHz: FrequencyText.formatMHzMachine(from10Hz: request.f2),
                    step: request.step.parameter,
                    mode: request.mode == .am ? "AM" : "FM",
                    firmware: request.firmware,
                    sweeps: stored
                )
                do {
                    try TraceArchive.encode(document).write(to: fileURL, options: .atomic)
                } catch {
                    errorText = SessionProblem.cannotWriteTrace(error.localizedDescription).errorDescription
                }
            }
        } catch is CancellationError {
            return
        } catch {
            await refreshConnectionState()
            errorText = describeSession(error)
        }
    }

    private func sendFrequency(_ units: Int64) async {
        guard let port else {
            let mhz = FrequencyText.formatMHzRussian(from10Hz: units)
            errorText = "Частота \(mhz) МГц. Приёмник не подключён — команда RF не отправлена."
            scanning = false
            return
        }
        do {
            try await sendChecked(port, ArDv1CommandFactory.analogMode(mode).frame(), label: "режим")
            let command = try ArDv1CommandFactory.frequency(units: units)
            try await sendChecked(port, command.frame(), label: "RF")
            status = "Приёмник на \(FrequencyText.formatMHzRussian(from10Hz: units)) МГц. Звук — с динамика приёмника."
            errorText = nil
            scanning = false
        } catch is CancellationError {
            return
        } catch {
            await refreshConnectionState()
            errorText = describeSession(error)
            scanning = false
        }
    }

    private func sendChecked(_ port: any InstrumentConnection, _ frame: Data, label: String) async throws {
        try await port.send(frame)
        let reply = try await port.readLine(timeout: 1.5, label: label)
        note("\(label) \(clipReply(reply, limit: 160))")
        if ArDv1Reply.isRejection(ArDv1Reply.parse(raw: reply)) {
            throw SessionProblem.deviceReply(clipReply(reply, limit: 180))
        }
    }

    private func exchange(
        _ port: any InstrumentConnection,
        _ frame: Data,
        label: String,
        accept: (String) throws -> Void
    ) async throws {
        try await port.send(frame)
        let reply = try await port.readLine(timeout: 1.5, label: label)
        note("\(label) \(clipReply(reply, limit: 160))")
        try accept(reply)
    }

    private func refreshConnectionState() async {
        if let port, !(await port.isOpen) {
            self.port = nil
            connected = false
            status = "Не подключён"
        }
    }

    private func note(_ line: String) {
        notes.append(line)
        if notes.count > 30 {
            notes.removeFirst(notes.count - 30)
        }
        journal = notes.joined(separator: "\n")
    }

    private func plot10Hz(_ bins: [LevelBin]) -> [PlotPoint] {
        bins.map { PlotPoint(x: Double($0.frequency) / 100_000, y: $0.level) }
    }
}

private struct ArScanRequest {
    var f1: Int64
    var f2: Int64
    var step: ArDv1Step
    var mode: ArDv1AnalogMode
    var pauseMs: Int
    var threshold: Double
    var stuckLimit: Int
    var grid: [Int64]
    var firmware: String
}
