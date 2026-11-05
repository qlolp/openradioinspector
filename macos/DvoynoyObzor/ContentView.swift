import SwiftUI

struct ContentView: View {
    @StateObject private var model = RemoteDualModel()

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Двойной обзор · общее ядро")
                            .font(.title.weight(.semibold))
                        Text("macOS — тонкий клиент /api/v1. Приборами и архивом владеет сервер; слева исходные отсчёты S-метра, справа dBm.")
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Circle()
                        .fill(model.connected ? Color.green : Color.secondary)
                        .frame(width: 10, height: 10)
                }
                HStack {
                    TextField("http://127.0.0.1:8770", text: $model.serverURL)
                        .textFieldStyle(.roundedBorder)
                        .disabled(model.connected)
                    if model.connected {
                        Button("Отключить UI") { model.disconnect() }
                        Button("Обновить") { Task { await model.reloadSessions() } }
                    } else {
                        Button("Подключить") { model.connect() }
                    }
                }
                HStack {
                    Picker("Сеанс", selection: Binding(
                        get: { model.selectedSessionID },
                        set: { model.selectSession($0) }
                    )) {
                        Text("Не выбран").tag("")
                        ForEach(model.sessions) { session in
                            Text("\(session.name) · \(session.state)").tag(session.id)
                        }
                    }
                    .disabled(!model.connected || model.busy)
                    Button("Новый draft") { model.createSession(simulated: false) }
                        .disabled(!model.connected || model.busy)
                    Button("Новый с симуляторами") { model.createSession(simulated: true) }
                        .disabled(!model.connected || model.busy)
                }
                HStack {
                    Button("Запустить сеанс") { model.startSession() }
                        .disabled(model.session == nil || model.busy)
                    Button("Остановить сеанс") { model.stopSession() }
                        .disabled(model.session == nil || model.busy)
                    Button("Запустить оба потока") { model.startWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Button("Остановить оба потока") { model.stopWorkers() }
                        .disabled(model.session == nil || model.busy)
                    Spacer()
                    Text(model.status).font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            Divider()
            HSplitView {
                RemoteArPane(model: model).frame(minWidth: 460)
                RemoteUltraPane(model: model).frame(minWidth: 460)
            }
        }
        .frame(minWidth: 980, minHeight: 700)
        .background(WindowCloseObserver {
            // Закрытие UI не останавливает измерения и не закрывает приборы ядра.
            model.disconnect()
        })
    }
}

import Combine
import Foundation
import SpektrKomandy
import SwiftUI

@MainActor
final class RemoteDualModel: ObservableObject {
    @Published var serverURL = "http://127.0.0.1:8770"
    @Published var sessions: [PlatformSession] = []
    @Published var selectedSessionID = ""
    @Published var session: PlatformSession?
    @Published var workers: [PlatformWorker] = []
    @Published var connected = false
    @Published var busy = false
    @Published var status = "Сервер не подключён"
    @Published var errorText: String?
    @Published var mode = "FM"
    @Published var threshold = "-80"
    @Published var current: [PlotPoint] = []
    @Published var minimum: [PlotPoint] = []
    @Published var maximum: [PlotPoint] = []
    @Published var rows: [Exceedance] = []
    @Published var waterfall = WaterfallHistory()
    @Published var waterfallRevision = 0
    @Published var latestObservation: PlatformObservation?
    @Published var live = "Данные ядра ещё не получены"

    private var client: PlatformClient?
    private var pollTask: Task<Void, Never>?
    private var generation = 0
    private var absorbedSweepIDs: Set<String> = []

    var analyzer: PlatformStream? { session?.streams.first(where: { $0.isDbmAnalyzer }) }
    var receiver: PlatformStream? { session?.streams.first(where: { $0.isReceiver }) }
    var thresholdDbm: Double? { parseUserNumber(threshold) }

    func worker(for streamID: String?) -> PlatformWorker? {
        guard let streamID else { return nil }
        return workers.first(where: { $0.streamId == streamID })
    }

    func connect() {
        do {
            client = try PlatformClient(baseURL: serverURL)
            connected = true
            errorText = nil
            status = "Сервер подключён; приборные порты остаются у ядра"
            generation += 1
            startPolling()
            Task { await reloadSessions() }
        } catch {
            connected = false
            errorText = describeSession(error)
        }
    }

    func disconnect() {
        generation += 1
        pollTask?.cancel()
        pollTask = nil
        client = nil
        connected = false
        busy = false
        session = nil
        sessions = []
        selectedSessionID = ""
        clearMeasurements()
        status = "Отключено от сервера; сеанс ядра не остановлен"
    }

    func reloadSessions() async {
        guard let client else { return }
        do {
            let loaded = try await client.listSessions()
            sessions = loaded
            if selectedSessionID.isEmpty || !loaded.contains(where: { $0.id == selectedSessionID }) {
                selectedSessionID = loaded.first?.id ?? ""
                clearMeasurements()
            }
            if !selectedSessionID.isEmpty { await refresh() }
        } catch {
            errorText = describeSession(error)
        }
    }

    func selectSession(_ id: String) {
        guard id != selectedSessionID else { return }
        generation += 1
        selectedSessionID = id
        clearMeasurements()
        Task { await refresh() }
    }

    func createSession(simulated: Bool) {
        guard let client else { return }
        busy = true
        Task {
            do {
                let created = try await client.createSession(
                    name: simulated ? "Двойной обзор · симуляторы" : "Двойной обзор",
                    simulated: simulated
                )
                sessions = try await client.listSessions()
                selectedSessionID = created.id
                clearMeasurements()
                status = simulated ? "Создан сеанс с явно выбранными симуляторами" : "Создан draft без hardware_control"
                await refresh()
            } catch {
                errorText = describeSession(error)
            }
            busy = false
        }
    }

    func refresh() async {
        guard let client, !selectedSessionID.isEmpty else { return }
        let id = selectedSessionID
        let requestGeneration = generation
        do {
            let snapshot = try await client.snapshot(sessionID: id)
            guard requestGeneration == generation, id == selectedSessionID else { return }
            session = snapshot.session
            workers = snapshot.workers
            applyMeasurements(snapshot)
            errorText = nil
            live = "Обновлено от ядра · свипов \(snapshot.sweeps.count) · наблюдений \(snapshot.observations.count)"
        } catch {
            guard requestGeneration == generation, id == selectedSessionID else { return }
            errorText = describeSession(error)
        }
    }

    func startSession() { performSessionAction { try await $0.startSession($1) } }
    func stopSession() { performSessionAction { try await $0.stopSession($1) } }
    func startWorkers() { performSessionAction { try await $0.startWorkers($1) } }
    func stopWorkers() { performSessionAction { try await $0.stopWorkers($1) } }

    func startStream(_ stream: PlatformStream?) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.startStream(sessionID: $1, streamID: stream.id) }
    }

    func stopStream(_ stream: PlatformStream?) {
        guard let stream else { return }
        performStreamAction(stream) { try await $0.stopStream(sessionID: $1, streamID: stream.id) }
    }

    func tuneLatestPeak() {
        guard let receiver, receiver.isReceiver else {
            errorText = "Перестройка доступна только receiver"
            return
        }
        guard let sweep = latestSweep,
              let index = sweep.levels.indices.max(by: { sweep.levels[$0] < sweep.levels[$1] }) else {
            errorText = "Нет фактического пика из архивного sweep"
            return
        }
        tune(frequencyHz: sweep.frequenciesHz[index])
    }

    func tune(frequencyHz: Int64) {
        guard let receiver, receiver.isReceiver else {
            errorText = "Перестройка доступна только receiver"
            return
        }
        performStreamAction(receiver) {
            try await $0.tune(sessionID: $1, streamID: receiver.id, frequencyHz: frequencyHz, mode: mode)
        }
    }

    private var latestSweep: PlatformSweep?

    private func performSessionAction(
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        perform(operation)
    }

    private func performStreamAction(
        _ stream: PlatformStream,
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        perform(operation)
    }

    private func perform(
        _ operation: @escaping (PlatformClient, String) async throws -> some Decodable
    ) {
        guard let client, !selectedSessionID.isEmpty else { return }
        let id = selectedSessionID
        busy = true
        Task {
            do {
                _ = try await operation(client, id)
                await refresh()
            } catch {
                errorText = describeSession(error)
            }
            busy = false
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task { @MainActor in
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    private func clearMeasurements() {
        session = nil
        workers = []
        latestSweep = nil
        latestObservation = nil
        absorbedSweepIDs = []
        current = []
        minimum = []
        maximum = []
        rows = []
        waterfall = WaterfallHistory()
        waterfallRevision += 1
    }

    private func applyMeasurements(_ snapshot: PlatformSnapshot) {
        var envelope = TraceEnvelope()
        var found: [Exceedance] = []
        let level = thresholdDbm ?? -80
        for sweep in snapshot.sweeps where sweep.levelUnit == "dBm" {
            let bins = zip(sweep.frequenciesHz, sweep.levels).map {
                LevelBin(frequency: $0, level: $1)
            }
            envelope.absorb(bins)
            found = ExceedanceList.update(existing: found, bins: bins, threshold: level)
            if !absorbedSweepIDs.contains(sweep.id) {
                waterfall.absorb(bins)
                absorbedSweepIDs.insert(sweep.id)
                waterfallRevision += 1
            }
        }
        latestSweep = snapshot.sweeps.last
        latestObservation = snapshot.observations.last
        if let latestSweep {
            current = plot(latestSweep)
        } else {
            current = []
        }
        minimum = envelope.minimumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        maximum = envelope.maximumSeries().map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) }
        rows = found
    }

    private func plot(_ sweep: PlatformSweep) -> [PlotPoint] {
        zip(sweep.frequenciesHz, sweep.levels).map {
            PlotPoint(x: Double($0) / 1_000_000, y: $1)
        }
    }
}

import SpektrKomandy
import SwiftUI

struct RemoteStreamControls: View {
    var title: String
    var stream: PlatformStream?
    var worker: PlatformWorker?
    var busy: Bool
    var onStart: () -> Void
    var onStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(stream.map { "\($0.device) · \($0.id)" } ?? "Подходящий поток не найден")
                .font(.caption)
                .textSelection(.enabled)
            HStack {
                Label(worker?.state ?? stream?.state ?? "—", systemImage: worker?.alive == true ? "circle.fill" : "circle")
                    .foregroundStyle(worker?.alive == true ? .green : .secondary)
                Spacer()
                Button("Запустить поток", action: onStart)
                    .disabled(stream == nil || busy)
                Button("Остановить поток", action: onStop)
                    .disabled(stream == nil || busy)
            }
            if let error = worker?.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red)
            }
        }
    }
}

struct RemoteArPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("AOR AR-DV1 · через ядро").font(.title2.weight(.semibold))
                Text("Наблюдения и команды поступают через /api/v1. Уровень остаётся в исходных отсчётах S-метра; звук и аппаратное декодирование выполняет приёмник.")
                    .foregroundStyle(.secondary)
                RemoteStreamControls(
                    title: "AR-DV1", stream: model.receiver,
                    worker: model.worker(for: model.receiver?.id), busy: model.busy,
                    onStart: { model.startStream(model.receiver) },
                    onStop: { model.stopStream(model.receiver) }
                )
                HStack {
                    Picker("Режим", selection: $model.mode) {
                        ForEach(["FM", "AM", "DMR", "TETRA"], id: \.self) { Text($0) }
                    }
                    .frame(width: 180)
                    Button("Перестроить на фактический пик") { model.tuneLatestPeak() }
                        .disabled(model.receiver == nil || model.current.isEmpty || model.busy)
                }
                if let observation = model.latestObservation {
                    GroupBox("Последнее наблюдение ядра") {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(frequency(observation.frequencyHz))
                            Text("Режим: \(observation.mode) · декодер: \(observation.decodedMode ?? "—")")
                            Text("Уровень: \(observation.level.map { String(format: "%.1f", $0) } ?? "—") \(observation.levelUnit)")
                            Text("Шумодав: \(observation.squelchOpen.map { $0 ? "открыт" : "закрыт" } ?? "—")")
                            Text("Интервал: \(observation.interval.startedAt) — \(observation.interval.endedAt)")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                } else {
                    Text("Наблюдений receiver пока нет").foregroundStyle(.secondary)
                }
                Text("Перестройка AR-DV1 не останавливает tinySA. Прежняя частота автоматически не восстанавливается.")
                    .font(.caption).foregroundStyle(.secondary)
                ErrorLine(text: model.errorText)
            }
            .padding(16)
        }
    }

    private func frequency(_ hertz: Int64) -> String {
        String(format: "%.6f МГц", Double(hertz) / 1_000_000) + " · \(hertz) Гц"
    }
}

struct RemoteUltraPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("tinySA Ultra · через ядро").font(.title2.weight(.semibold))
                Text("Спектр и водопад строятся только из архивных dBm sweeps. Клиент не отправляет scan и не включает генератор.")
                    .foregroundStyle(.secondary)
                RemoteStreamControls(
                    title: "tinySA", stream: model.analyzer,
                    worker: model.worker(for: model.analyzer?.id), busy: model.busy,
                    onStart: { model.startStream(model.analyzer) },
                    onStop: { model.stopStream(model.analyzer) }
                )
                HStack {
                    TextField("Порог, dBm", text: $model.threshold)
                        .textFieldStyle(.roundedBorder).frame(width: 140)
                    Text(model.live).font(.caption).foregroundStyle(.secondary)
                }
                TraceChart(
                    current: model.current, minimum: model.minimum, maximum: model.maximum,
                    threshold: model.thresholdDbm, yTitle: "дБм (исходные)"
                )
                WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "дБм")
                GroupBox("Превышения порога") {
                    if model.rows.isEmpty {
                        Text("Превышений нет").foregroundStyle(.secondary)
                    } else {
                        ForEach(model.rows.prefix(30)) { row in
                            Button {
                                model.tune(frequencyHz: row.center)
                            } label: {
                                HStack {
                                    Text(String(format: "%.6f МГц", Double(row.center) / 1_000_000))
                                    Spacer()
                                    Text("полоса \(row.width) Гц · \(row.hits)")
                                }
                            }
                            .buttonStyle(.plain)
                            .disabled(model.receiver == nil || model.busy)
                        }
                    }
                }
                ErrorLine(text: model.errorText)
            }
            .padding(16)
        }
    }
}

// Observe this particular window; other WindowGroup windows keep their own sessions.
struct WindowCloseObserver: NSViewRepresentable {
    var onClose: () -> Void

    func makeNSView(context: Context) -> WindowCloseView {
        let view = WindowCloseView()
        view.onClose = onClose
        return view
    }

    func updateNSView(_ view: WindowCloseView, context: Context) {
        view.onClose = onClose
    }
}

final class WindowCloseView: NSView {
    var onClose: (() -> Void)?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: nil)
        if let window {
            NotificationCenter.default.addObserver(self, selector: #selector(windowWillClose),
                name: NSWindow.willCloseNotification, object: window)
        }
    }

    @objc private func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    deinit { NotificationCenter.default.removeObserver(self) }
}
