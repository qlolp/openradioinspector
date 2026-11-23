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
    private struct ScopeTarget: Identifiable {
        let id = UUID()
        let client: PlatformClient
        let port: String
    }

    @Bindable var model: RemoteDualModel
    @State private var scopeTarget: ScopeTarget?

    private var receiverPort: String? {
        guard let receiver = model.receiver, ["AR-DV1", "AOR AR-DV1"].contains(receiver.device),
              receiver.levelUnit == "s_meter_count", let port = receiver.port,
              !port.isEmpty, !port.lowercased().hasPrefix("sim://") else { return nil }
        return port
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text("AOR AR-DV1 · через ядро").font(.title2.weight(.semibold))
                Text("Уровень показан в исходных отсчётах S-метра. Звук и декодирование выполняет приёмник.")
                    .foregroundStyle(.secondary)
                if model.receiver?.capabilities.contains("ardv1_sampled_scope") != true {
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
                }
                if let receiverPort {
                    Button("Обзор диапазона…") {
                        guard let client = model.client else { return }
                        scopeTarget = ScopeTarget(client: client, port: receiverPort)
                    }
                    .disabled(!model.coreReachable || model.busy || model.worker(for: model.receiver?.id)?.alive == true)
                    .help("Для обзора завершите текущий приём AR-DV1. tinySA может продолжать измерения.")
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
        .sheet(item: $scopeTarget) { target in
            NativeARDV1ScopePane(client: target.client, port: target.port) {
                guard model.client === target.client else { return }
                await model.reloadSessions()
            }
        }
    }

    private func frequency(_ hertz: Int64) -> String {
        String(format: "%.6f МГц", Double(hertz) / 1_000_000) + " · \(hertz) Гц"
    }
}

struct RemoteUltraPane: View {
    @Bindable var model: RemoteDualModel
    var research = true
    var scanBands: [MonitoringValue] = []
    var bandPlanWorkspace: MonitoringWorkspaceModel?
    var compact = false
    @State private var detailsExpanded = false

    private func inspector(openRaw: ((PlatformSweep, Int?) -> Void)? = nil,
                           analyze: ((PlatformSweep) -> Void)? = nil) -> some View {
        NativeSpectrumInspector(sweeps: model.displayInspectorSweeps,
                                    scale: model.displaySpectrumScale, threshold: model.thresholdDbm,
                                    client: model.client, sessionID: model.selectedSessionID, streamID: model.analyzer?.id ?? "",
                                    hasTinySAControls: model.tinySAAnalyzer != nil, rfExplorer: model.analyzer?.isRFExplorer3G == true,
                                    openRFExplorerWindow: openRaw, analyzeRFExplorerSweep: analyze,
                                    liveReceiving: model.followAnalyzerMeasurements && !model.measurementsStale,
                                    measurementsStale: model.measurementsStale,
                                    presentationPaused: model.spectrumPresentation.paused,
                                    setPresentationPaused: { model.setSpectrumPaused($0) },
                                    reviewedSweep: model.reviewedSweep, reviewRequest: model.reviewedSweepRequest,
                                    onFollowFrequency: { model.followFrequency($0) },
                                    bandPlanWorkspace: bandPlanWorkspace,
                                    viewMemory: model.spectrumViewMemory,
                                    viewMemoryScope: [model.serverURL, model.selectedSessionID, model.analyzer?.id ?? "", model.selectedScanBand].map { PlatformExactID($0) })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label(model.analyzer?.device ?? "Спектроанализатор", systemImage: "waveform.path").font(.headline)
                Text(NativeDeviceStatus.title(model.worker(for: model.analyzer?.id)?.state ?? "disconnected"))
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Начать приём") { model.startStream(model.analyzer) }
                    .disabled(model.analyzer == nil || model.busy || model.worker(for: model.analyzer?.id)?.alive == true)
                Button("Завершить приём") { model.stopStream(model.analyzer) }
                    .disabled(model.worker(for: model.analyzer?.id)?.alive != true || model.busy)
                Menu("Ещё") {
                    Button("Прервать измерение", role: .destructive) { model.stopStream(model.analyzer, mode: .force) }
                        .disabled(model.worker(for: model.analyzer?.id)?.alive != true || model.busy)
                }
            }.controlSize(.small)
            if !compact {
                NativeMeasurementStatus(model: model)
                if let analyzer = model.tinySAAnalyzer {
                    NativeAnalyzerControls(model: model)
                        .id([PlatformExactID(model.serverURL), PlatformExactID(model.selectedSessionID), analyzer.exactID])
                }
            }
            if !scanBands.isEmpty || !model.availableScanBandIDs.isEmpty {
                Picker("Поддиапазон графика", selection: Binding(get: { model.selectedScanBand }, set: model.selectScanBand)) {
                    if model.selectedScanBand.isEmpty { Text("Ожидание измерения").tag("") }
                    if scanBands.isEmpty {
                        ForEach(model.availableScanBandIDs, id: \.self) { id in Text(id).tag(id) }
                    } else {
                        ForEach(scanBands, id: \.exactID) { band in
                            Text(band["name"].text).tag(band["id"].text)
                        }
                    }
                }.controlSize(.small).disabled(!model.coreReachable)
            }
            Group {
                if let client = model.client, let analyzer = model.analyzer, analyzer.isRFExplorer3G {
                    NativeRFExplorerWorkbench(client: client, session: model.selectedSessionID, stream: analyzer.id,
                        survey: analyzer.rfExplorerSurveyStepHz != nil || model.displayInspectorSweeps.last?.settings?["survey"] != nil,
                        currentWindow: model.displayInspectorSweeps.last, live: model.followAnalyzerMeasurements,
                        paused: model.spectrumPresentation.paused, setPaused: { model.setSpectrumPaused($0) },
                        memory: model.spectrumViewMemory) { openRaw, analyze in inspector(openRaw: openRaw, analyze: analyze) }
                        .id(ObjectIdentifier(client))
                } else { inspector() }
            }
                .id([PlatformExactID(model.serverURL), PlatformExactID(model.selectedSessionID),
                     PlatformExactID(model.analyzer?.id ?? ""), PlatformExactID(model.selectedScanBand)])
                .frame(minHeight: compact ? 320 : 370).layoutPriority(1)
            HStack {
                Button("Автомасштаб") { model.fitSpectrum() }
                Text("Порог показа, дБм").font(.caption)
                TextField("дБм", text: $model.threshold).frame(width: 65)
                    .help("Линия на графике. Реакции охраны настраиваются отдельно в настройках охраны.")

                Spacer()
                Button(detailsExpanded ? "Скрыть настройки" : "Настройки и анализ") { detailsExpanded.toggle() }
            }.controlSize(.small)
            if let error = model.worker(for: model.analyzer?.id)?.lastError, !error.isEmpty {
                Text(error).font(.caption).foregroundStyle(.red).lineLimit(2).textSelection(.enabled)
            }
            ErrorLine(text: model.errorText)
        }.padding(6)
        .sheet(isPresented: $detailsExpanded) {
            VStack(alignment: .trailing) {
                Button("Готово") { detailsExpanded = false }.keyboardShortcut(.cancelAction)
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        if compact {
                            NativeMeasurementStatus(model: model)
                            if model.tinySAAnalyzer != nil { NativeAnalyzerControls(model: model) }
                        }
                        NativeSpectrumReadout(points: model.displayCurrent)
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
                                Text("Показаны сохранённые ядром отсчёты в исходных единицах.")
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

                    }.padding(4)
                }.frame(minWidth: 720, minHeight: 480)
            }.padding(16)
        }
    }
}

@MainActor private protocol WorkbenchWindowSizingHost: AnyObject {
    func useFixedWorkbenchSizing(preservingLimits: Bool)
}

extension NSHostingView: WorkbenchWindowSizingHost {
    fileprivate func useFixedWorkbenchSizing(preservingLimits: Bool) {
        guard !sizingOptions.isEmpty else { return }
        if preservingLimits {
            needsUpdateConstraints = true
            updateConstraintsForSubtreeIfNeeded()
        }
        let limits = preservingLimits ? constraints.filter {
            $0.firstItem === self && $0.secondItem == nil && $0.constant.isFinite
                && ($0.firstAttribute == .width || $0.firstAttribute == .height)
                && $0.relation != .equal
        } : []
        sizingOptions = []
        guard preservingLimits else { return }
        updateConstraintsForSubtreeIfNeeded()
        // Keep the pane's declared limits after AppKit removes its automatic
        // constraints. Dividers still honor minima/maxima without fitting the
        // whole pane again for each incoming frame.
        for limit in limits where !limit.isActive {
            let fixed = NSLayoutConstraint(item: self, attribute: limit.firstAttribute,
                relatedBy: limit.relation, toItem: nil, attribute: .notAnAttribute,
                multiplier: 1, constant: limit.constant)
            fixed.priority = limit.priority
            fixed.identifier = "ori.workbench.fixedLimit"
            fixed.isActive = true
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
        view.scheduleWorkbenchSizing()
    }
}

final class WindowCloseView: NSView {
    var onClose: (() -> Void)?
    private var sizingUpdateScheduled = false

    func scheduleWorkbenchSizing() {
        guard let window, !sizingUpdateScheduled else { return }
        sizingUpdateScheduled = true
        DispatchQueue.main.async { [weak self, weak window] in
            guard let self else { return }
            self.sizingUpdateScheduled = false
            guard let window, self.window === window else { return }
            var ancestor = self.superview
            var root: NSView?
            while let view = ancestor {
                if view is any WorkbenchWindowSizingHost { root = view }
                ancestor = view.superview
            }
            guard let root else { return }
            // HSplitView creates additional hosts for its panes. Disabling only
            // the outer host leaves those panes measuring the entire subtree.
            @MainActor func apply(_ view: NSView) {
                (view as? any WorkbenchWindowSizingHost)?.useFixedWorkbenchSizing(preservingLimits: true)
                for child in view.subviews { apply(child) }
            }
            apply(root)
            window.contentMinSize = CGSize(width: 1000, height: 700)
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: nil)
        if let window {
            // ContentView already defines this minimum. Automatic intrinsic
            // constraints otherwise remeasure the entire workbench per frame.
            var ancestor = superview
            var host: (any WorkbenchWindowSizingHost)?
            while let view = ancestor {
                if let value = view as? any WorkbenchWindowSizingHost { host = value }
                ancestor = view.superview
            }
            if let host {
                host.useFixedWorkbenchSizing(preservingLimits: false)
                window.contentMinSize = CGSize(width: 1000, height: 700)
                scheduleWorkbenchSizing()
                // AppKit can finish attaching the hosting view after this hook
                // and reset its minimum; apply it after that transaction too.
                DispatchQueue.main.async { [weak self, weak window] in
                    guard let self, let window, self.window === window else { return }
                    window.contentMinSize = CGSize(width: 1000, height: 700)
                    NativeAcceptanceTrace.shared?.record("window.content_sizing", [
                        "automatic": false, "minimum_width": window.contentMinSize.width,
                        "minimum_height": window.contentMinSize.height])
                }
            }
            NotificationCenter.default.addObserver(self, selector: #selector(windowWillClose),
                name: NSWindow.willCloseNotification, object: window)
        }
    }

    @objc private func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    deinit { NotificationCenter.default.removeObserver(self) }
}
