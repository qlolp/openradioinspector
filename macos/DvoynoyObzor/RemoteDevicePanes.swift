import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

struct RemoteStreamControls: View {
    var title: String
    var stream: PlatformStream?
    var worker: PlatformWorker?
    var busy: Bool
    var onStart: () -> Void
    var onStop: () -> Void
    var onForceStop: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(stream?.device ?? "Прибор не добавлен в сеанс")
                .font(.caption)
                .textSelection(.enabled)
            VStack(alignment: .leading, spacing: 8) {
                Label(worker?.stopping == true ? "Завершаю текущее измерение…" : NativeDeviceStatus.title(worker?.state ?? stream?.state ?? "disconnected"), systemImage: worker?.alive == true ? "circle.fill" : "circle")
                    .foregroundStyle(worker?.alive == true ? .green : .secondary)
                HStack {
                    Button("Начать приём", action: onStart)
                        .buttonStyle(.borderedProminent)
                        .disabled(stream == nil || busy || worker?.alive == true)
                    Button("Завершить приём", action: onStop)
                        .disabled(stream == nil || busy || worker?.alive != true || worker?.stopping == true)
                    Menu("Ещё") {
                        Button("Прервать измерение", role: .destructive, action: onForceStop)
                            .disabled(stream == nil || busy || worker?.alive != true)
                    }
                }
            }
            if let error = worker?.lastError, !error.isEmpty {
                Label(worker?.alive == true ? "Ошибка приёма. Исполнитель ещё работает." : "Приём остановлен. Проверьте подключение прибора.", systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.red)
                DisclosureGroup("Технические подробности") {
                    Text(error).font(.caption).textSelection(.enabled)
                }
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
                Text("Уровень показан в исходных отсчётах S-метра. Звук и декодирование выполняет приёмник.")
                    .foregroundStyle(.secondary)
                RemoteStreamControls(
                    title: "AR-DV1", stream: model.receiver,
                    worker: model.worker(for: model.receiver?.id), busy: model.busy,
                    onStart: { model.startStream(model.receiver) },
                    onStop: { model.stopStream(model.receiver) },
                    onForceStop: { model.stopStream(model.receiver, mode: .force) }
                )
                HStack {
                    Picker("Режим", selection: $model.mode) {
                        ForEach(model.receiver?.tuneModes ?? ["FM", "AM"], id: \.self) { Text($0) }
                    }
                    .frame(width: 180)
                    Button("Перестроить на фактический пик") { model.tuneLatestPeak() }
                        .disabled(model.receiver == nil || model.current.isEmpty || model.busy || model.spectrumPresentation.paused)
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
                    Text("Измерений приёмника пока нет").foregroundStyle(.secondary)
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
    var research = true

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Label("Спектральный обзор", systemImage: "waveform.path").font(.title3.weight(.semibold))
                    Spacer()
                    Text("tinySA ULTRA").font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                }
                RemoteStreamControls(
                    title: "tinySA", stream: model.analyzer,
                    worker: model.worker(for: model.analyzer?.id), busy: model.busy,
                    onStart: { model.startStream(model.analyzer) },
                    onStop: { model.stopStream(model.analyzer) },
                    onForceStop: { model.stopStream(model.analyzer, mode: .force) }
                )
                NativeMeasurementStatus(model: model)
                NativeSpectrumReadout(points: model.displayCurrent)
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("СПЕКТР").font(.caption.weight(.semibold)).tracking(1.5)
                        Spacer()
                        Text(model.spectrumPresentation.paused ? "Показ зафиксирован" : "Последний сохранённый свип")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    TraceChart(
                        current: model.displayCurrent, minimum: model.displayMinimum, maximum: model.displayMaximum,
                        threshold: model.thresholdDbm, yTitle: "дБм · исходные", displayScale: model.displaySpectrumScale, plotHeight: 180, showsGrid: true
                    )
                    HStack {
                        Text(model.displayCurrent.first.map { String(format: "%.5f", $0.x) } ?? "—")
                        Spacer()
                        Text("МГц")
                        Spacer()
                        Text(model.displayCurrent.last.map { String(format: "%.5f", $0.x) } ?? "—")
                    }.font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
                    if model.displayCurrent.isEmpty {
                        Label("Нет измерений · выберите сеанс и начните приём", systemImage: "waveform.path")
                            .font(.callout).foregroundStyle(.secondary)
                    }
                    Divider()
                    HStack {
                        Text("ВОДОПАД").font(.caption.weight(.semibold)).tracking(1.5)
                        Spacer()
                        Text("Новые свипы сверху").font(.caption).foregroundStyle(.secondary)
                    }
                    WaterfallView(history: model.displayWaterfall, revision: model.displayWaterfallRevision,
                        unitTitle: "дБм", displayScale: model.displaySpectrumScale, plotHeight: 110)
                    Text("Строки — порядок свипов, не шкала времени; столбцы — отсчёты частоты.")
                        .font(.caption2).foregroundStyle(.secondary)
                }
                .padding(14)
                .background(Color(red: 0.055, green: 0.075, blue: 0.10), in: RoundedRectangle(cornerRadius: 10))
                .environment(\.colorScheme, .dark)
                HStack {
                    Button { model.toggleSpectrumPause() } label: {
                        Label(model.spectrumPresentation.paused ? "Продолжить показ" : "Пауза показа",
                              systemImage: model.spectrumPresentation.paused ? "play.fill" : "pause.fill")
                    }
                    Button("Автомасштаб") { model.fitSpectrum() }
                }.disabled(model.spectrumPresentation.sweeps.isEmpty)
                DisclosureGroup("Порог и ручная шкала") {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("Порог, дБм")
                            TextField("Порог, дБм", text: $model.threshold).frame(width: 85)
                        }
                        HStack {
                            TextField("Низ, дБм", text: $model.spectrumLower).frame(width: 85)
                            TextField("Верх, дБм", text: $model.spectrumUpper).frame(width: 85)
                            Button("Применить") { model.applySpectrumBounds() }
                        }.disabled(model.spectrumPresentation.sweeps.isEmpty)
                        Text("Обрезано шкалой: \(model.spectrumClippedCount) отсчётов. Для границ используйте десятичную точку.")
                            .font(.caption).foregroundStyle(.secondary)
                        ErrorLine(text: model.spectrumPresentation.error)
                    }.textFieldStyle(.roundedBorder).padding(.top, 6)
                }
                DisclosureGroup("Источник измерения") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(model.live)
                        if let sweep = model.spectrumPresentation.sweeps.last {
                            Text("Свип: \(sweep.id)")
                            Text("UTC: \(sweep.interval.startedAt) → \(sweep.interval.endedAt)")
                        }
                        Text("Показаны сохранённые ядром отсчёты в исходных единицах. Генератор из этой панели не включается.")
                    }.font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if research {
                RemoteSweepComparisonPane(model: model)
                RemoteSweepAveragePane(model: model.sweepAverage)
                RemoteThreeSweepThresholdsPane(model: model.threeSweepThresholds)
                RemoteSweepContextPane(model: model.sweepContext)
                RemoteSweepThresholdPane(model: model.sweepThreshold)
                RemoteSweepQualityPane(model: model.sweepQuality)
                RemoteSpectralMaskPane(model: model.spectralMask)
                RemoteFrequencyRasterPane(model: model.frequencyRaster)
                RemoteMeasurementResultsPane(model: model.measurementResults)
                RemoteFrequencyCounterPane(model: model.frequencyCounter)
                RemoteFieldStrengthPane(model: model.fieldStrength)
                RemoteDvbEvaluationPane(model: model.dvbEvaluation)
                Picker("Поток фонограммы / обмена", selection: $model.auxiliaryStreamID.exactID) {
                    ForEach(model.session?.streams ?? [], id: \.exactID) { stream in
                        Text("\(stream.device) · \(stream.id) · \(stream.levelUnit)").tag(stream.exactID)
                    }
                }.disabled(model.session?.streams.isEmpty ?? true)
                RemotePhonogramPane(model: model.phonogram)
                RemoteSignalListPane(model: model.signalList)
                RemoteMeasurementMapPane(model: model.measurementMap)
                RemoteAudioStreamPane(model: model.audioStream)
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
