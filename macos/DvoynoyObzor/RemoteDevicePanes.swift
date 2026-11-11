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
    var research = true

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
                    threshold: model.thresholdDbm, yTitle: "дБм (исходные)", displayScale: model.spectrumPresentation.scale
                )
                HStack {
                    Button(model.spectrumPresentation.paused ? "Live" : "Pause view") { model.toggleSpectrumPause() }
                    Button("Fit") { model.fitSpectrum() }
                    TextField("Min", text: $model.spectrumLower).frame(width: 85)
                    TextField("Max", text: $model.spectrumUpper).frame(width: 85)
                    Button("Применить") { model.applySpectrumBounds() }
                }.disabled(model.spectrumPresentation.sweeps.isEmpty)
                Text("Границы: десятичная точка / e; обрезано отсчётов: \(model.spectrumClippedCount)").font(.caption)
                ErrorLine(text: model.spectrumPresentation.error)
                if let scale = model.spectrumPresentation.scale {
                    Text("Шкала: \(String(scale.lower)) … \(String(scale.upper)) dBm").font(.caption)
                }
                if let sweep = model.spectrumPresentation.sweeps.last {
                    Text("\(model.spectrumPresentation.paused ? "Pause view" : "Live") · \(sweep.id) · \(sweep.interval.startedAt) → \(sweep.interval.endedAt)").font(.caption)
                    Text("Исходные уровни: \(sweep.levels.min().map { String($0) } ?? "—") … \(sweep.levels.max().map { String($0) } ?? "—") dBm").font(.caption)
                } else { Text("Спектр недоступен") }
                WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "дБм", displayScale: model.spectrumPresentation.scale)
                Text("Строки: порядок свипов, новые сверху, не шкала времени. Столбцы: порядок bins; интервалы частот могут быть неравными.").font(.caption)
                Text("Пауза замораживает только графики. Опрос и состояние сбора/записи не меняются; панели анализа независимы.").font(.caption)
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
                RemoteDvbEvaluationPane(model: model.dvbEvaluation)
                Picker("Поток фонограммы / обмена", selection: $model.auxiliaryStreamID) {
                    ForEach(model.session?.streams ?? [], id: \.id) { stream in
                        Text("\(stream.device) · \(stream.id) · \(stream.levelUnit)").tag(stream.id)
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
