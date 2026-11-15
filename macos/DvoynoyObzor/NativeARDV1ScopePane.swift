import Charts
import Foundation
import SpektrKomandy
import SwiftUI

@MainActor
private final class NativeARDV1ScopeRun: ObservableObject {
    @Published private(set) var configuration: PlatformARDV1ScopeConfiguration?
    @Published private(set) var points: [PlatformARDV1ScopePoint] = []
    @Published private(set) var busy = false
    @Published private(set) var stopping = false
    @Published private(set) var status = "Готов к проходу"
    @Published private(set) var failure: String?

    func requestStop() {
        guard busy else { return }
        stopping = true
        status = "Завершаю текущую точку…"
    }

    func run(
        client: PlatformClient, port: String, configuration: PlatformARDV1ScopeConfiguration,
        archiveChanged: @MainActor () async -> Void
    ) async {
        guard !busy else { return }
        busy = true
        stopping = false
        failure = nil
        points = []
        self.configuration = configuration
        status = "Открываю сеанс AR-DV1…"
        defer {
            busy = false
            stopping = false
        }

        var target: (session: String, stream: String)?
        var receiverMayBeRunning = false
        do {
            let session = try await client.createARDV1Scope(port: port, configuration: configuration)
            guard let stream = session.streams.first else {
                throw PlatformError.invalidResponse("Ядро не создало поток обзора AR-DV1.")
            }
            target = (session.id, stream.id)
            if !stopping {
                let started = try await client.startSession(session.id)
                guard PlatformExactID.same(started.id, session.id), started.state == "running" else {
                    throw PlatformError.invalidResponse("Не удалось подтвердить запуск сеанса AR-DV1.")
                }
            }
            if !stopping {
                receiverMayBeRunning = true
                var worker = try await client.startStream(sessionID: session.id, streamID: stream.id)
                var stopSent = false
                while true {
                    guard PlatformExactID.same(worker.streamId, stream.id) else {
                        throw PlatformError.invalidResponse("Ядро вернуло состояние другого потока.")
                    }
                    if !worker.alive { receiverMayBeRunning = false }
                    if stopping && worker.alive && !stopSent {
                        worker = try await client.stopStream(sessionID: session.id, streamID: stream.id, mode: .graceful)
                        stopSent = true
                        continue
                    }
                    let saved = try await client.ardv1ScopePoints(
                        sessionID: session.id, streamID: stream.id, configuration: configuration
                    )
                    guard saved.count >= points.count, zip(saved, points).allSatisfy({ $0.id == $1.id }) else {
                        throw PlatformError.invalidResponse("Архив изменился во время прохода.")
                    }
                    points = saved
                    if !worker.alive {
                        if let error = worker.lastError, !error.isEmpty {
                            throw PlatformError.invalidResponse(error)
                        }
                        if !stopping && points.count != configuration.pointCount {
                            throw PlatformError.invalidResponse("Поток завершился до записи всех точек.")
                        }
                        break
                    }
                    if !stopping { status = "Измеряю уровни…" }
                    try await Task.sleep(for: .milliseconds(350))
                    worker = try await client.ardv1ScopeWorker(sessionID: session.id, streamID: stream.id)
                }
            }
        } catch {
            failure = error.localizedDescription
        }

        if let target {
            do {
                if receiverMayBeRunning {
                    try await finishReceiver(client: client, sessionID: target.session, streamID: target.stream)
                }
                let session = try await client.getSession(target.session)
                guard PlatformExactID.same(session.id, target.session) else {
                    throw PlatformError.invalidResponse("Ядро вернуло состояние другого сеанса.")
                }
                if session.state == "running" {
                    let stopped = try await client.stopSession(target.session)
                    guard PlatformExactID.same(stopped.id, target.session), stopped.state == "stopped" else {
                        throw PlatformError.invalidResponse("Ядро не подтвердило закрытие сеанса обзора.")
                    }
                }
                await archiveChanged()
            } catch {
                let closeError = "Не удалось подтвердить закрытие обзора. \(error.localizedDescription)"
                failure = [failure, closeError].compactMap { $0 }.joined(separator: "\n")
            }
        }
        if failure != nil {
            status = "Ошибка прохода"
        } else {
            status = points.count == configuration.pointCount ? "Проход завершён" : "Проход остановлен"
        }
    }

    private func finishReceiver(client: PlatformClient, sessionID: String, streamID: String) async throws {
        let response = try await client.stopStream(sessionID: sessionID, streamID: streamID, mode: .graceful)
        guard PlatformExactID.same(response.streamId, streamID) else {
            throw PlatformError.invalidResponse("Ядро вернуло состояние другого потока.")
        }
        guard response.alive else { return }
        let clock = ContinuousClock()
        let deadline = clock.now + .seconds(10)
        while clock.now < deadline {
            let worker = try await client.ardv1ScopeWorker(sessionID: sessionID, streamID: streamID)
            if !worker.alive { return }
            try await Task.sleep(for: .milliseconds(250))
        }
        throw PlatformError.invalidResponse("Приёмник ещё завершает измерение.")
    }
}

struct NativeARDV1ScopePane: View {
    let client: PlatformClient
    let port: String
    var archiveChanged: @MainActor () async -> Void

    @Environment(\.dismiss) private var dismiss
    @StateObject private var run = NativeARDV1ScopeRun()
    @State private var startMHz = "88"
    @State private var stopMHz = "108"
    @State private var stepKHz = "100"
    @State private var supported = false
    @State private var availabilityError: String?
    @State private var inputError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Обзор диапазона AR-DV1").font(.title2.weight(.semibold))
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction).disabled(run.busy)
            }
            HStack(alignment: .bottom, spacing: 12) {
                field("Начало, МГц", text: $startMHz)
                field("Конец, МГц", text: $stopMHz)
                field("Шаг, кГц", text: $stepKHz)
                Button("Начать проход", action: start).buttonStyle(.borderedProminent)
                    .disabled(!supported || run.busy)
                Button("Остановить") { run.requestStop() }.disabled(!run.busy || run.stopping)
            }
            Text("Один проход в FM. Каждая точка содержит уровень S-метра и частоту из ответа приёмника.")
                .font(.callout).foregroundStyle(.secondary)
            if let error = inputError ?? run.failure ?? availabilityError {
                ErrorLine(text: error)
            }
            plot
            HStack {
                Text(supported ? run.status : availabilityError == nil ? "Проверка поддержки ядром" : "Обзор недоступен")
                if let configuration = run.configuration {
                    Text("\(run.points.count) из \(configuration.pointCount) точек")
                        .monospacedDigit().foregroundStyle(.secondary)
                }
                Spacer()
                if let point = run.points.last {
                    Text("\(mhz(point.observation.frequencyHz).formatted()) МГц · \(point.level.formatted()) S · \(point.tuneCommand)")
                        .monospacedDigit()
                }
            }.font(.callout)
        }
        .padding(20)
        .frame(minWidth: 680, idealWidth: 760, minHeight: 460)
        .interactiveDismissDisabled(run.busy)
        .onDisappear { run.requestStop() }
        .task {
            do {
                supported = try await client.supportsARDV1Scope()
                if !supported { availabilityError = "Подключённое ядро не поддерживает обзор диапазона AR-DV1." }
            } catch {
                availabilityError = error.localizedDescription
            }
        }
    }

    private func field(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(title, text: text)
                .labelsHidden().textFieldStyle(.roundedBorder)
                .frame(width: 104).disabled(run.busy)
        }
    }

    private var plot: some View {
        Group {
            if let configuration = run.configuration, !run.points.isEmpty {
                Chart(run.points) { point in
                    PointMark(
                        x: .value("Частота, МГц", mhz(point.observation.frequencyHz)),
                        y: .value("Отсчёты S-метра", point.level)
                    )
                    .symbolSize(14)
                }
                .chartXScale(domain: mhz(Int64(configuration.startHz))...mhz(Int64(configuration.stopHz)))
                .chartXAxisLabel("МГц")
                .chartYAxisLabel("Отсчёты S-метра")
                .accessibilityLabel("Измеренные точки обзора AR-DV1")
            } else {
                ContentUnavailableView(
                    run.busy ? "Ожидаю точки из архива" : "Нет точек обзора",
                    systemImage: "chart.dots.scatter"
                )
            }
        }.frame(minHeight: 260).frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func start() {
        inputError = nil
        do {
            let configuration = try PlatformARDV1ScopeConfiguration(
                startHz: hertz(startMHz, multiplier: 1_000_000),
                stopHz: hertz(stopMHz, multiplier: 1_000_000),
                stepHz: hertz(stepKHz, multiplier: 1000)
            )
            Task { await run.run(client: client, port: port, configuration: configuration, archiveChanged: archiveChanged) }
        } catch {
            inputError = error.localizedDescription
        }
    }

    private func mhz(_ hz: Int64) -> Double { Double(hz) / 1_000_000 }

    private func hertz(_ value: String, multiplier: Int) throws -> Int {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines).replacingOccurrences(of: ",", with: ".")
        let locale = Locale(identifier: "en_US_POSIX")
        if text.wholeMatch(of: /[0-9]{1,12}(\.[0-9]{1,8})?/) != nil,
           let parsed = Decimal(string: text, locale: locale) {
            let scaled = parsed * Decimal(multiplier)
            if scaled <= 1_300_000_000 {
                let integer = NSDecimalNumber(decimal: scaled).intValue
                if Decimal(integer) == scaled { return integer }
            }
        }
        throw PlatformError.invalidCommand("Введите частоту и шаг с точностью до целых герц.")
    }
}
