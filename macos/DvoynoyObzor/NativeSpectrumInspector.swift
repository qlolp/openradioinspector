import SwiftUI
import SpektrKomandy

/// Both panels use identical frequency coordinates, saved sweep IDs and UTC intervals.
struct NativeSpectrumInspector: View {
    let sweeps: [PlatformSweep]
    let scale: PlatformSpectrumScale?
    let threshold: Double?
    var client: PlatformClient? = nil
    var sessionID = ""
    var streamID = ""
    var liveReceiving = true
    var measurementsStale = false
    var presentationPaused = false
    var setPresentationPaused: (@MainActor (Bool) -> Void)? = nil
    var reviewedSweep: PlatformSweep? = nil
    var reviewRequest = UUID()
    var onFollowFrequency: ((Int64) -> Void)?
    var bandPlanWorkspace: MonitoringWorkspaceModel?
    @AppStorage("native.plot.split") private var split = 0.50
    @AppStorage("native.plot.seconds") private var seconds = 120.0
    @AppStorage("native.plot.minimum") private var showMinimum = true
    @AppStorage("native.plot.maximum") private var showMaximum = true
    @State private var inspectionScale: PlatformSpectrumScale?
    @State private var splitStart: Double?
    @State private var reference: PlatformSweep?
    @State private var referenceVisible = true
    @State private var timeEnd: Date?
    @State private var offset = 0.0
    @State private var archive = NativeArchiveBrowser()
    @State private var archiveTask: Task<Void, Never>?
    @State private var historySweeps: [PlatformSweep] = []
    @State private var archiveWindow: ClosedRange<Date>?

    @State private var frozen: [PlatformSweep]?
    @State private var selectedID: PlatformExactID?
    @State private var cursor: NativeSpectrumSample?
    @State private var inGap = false
    @State private var markers: [NativeSpectrumSample] = []
    @State private var zoom: ClosedRange<Double>?
    @State private var peakIndex = -1
    @State private var threeDimensional = false
    @State private var gestureRange: ClosedRange<Double>?
    @State private var selectionStart: CGPoint?
    @State private var selectionEnd: CGPoint?
    @State private var selectBand = false
    @State private var trackingPeak = false
    @State private var tuning = false
    @State private var tuneTask: Task<Void, Never>?
    @State private var tuneMessage = ""

    private var source: [PlatformSweep] { frozen ?? sweeps }
    private var holdingPresentation: Bool { frozen != nil || presentationPaused }
    private var presentationStatus: String {
        if frozen != nil { return "ИСТОРИЯ" }
        if presentationPaused { return "ПАУЗА" }
        if liveReceiving { return "LIVE" }
        if measurementsStale && !sweeps.isEmpty { return "ПОСЛЕДНИЕ ДАННЫЕ" }
        return "ПРИЁМ ОСТАНОВЛЕН"
    }
    private var window: ClosedRange<Date> {
        NativeTimeWindow.range(end: timeEnd ?? (source.isEmpty ? Date() : NativeSpectrumTimeline(source).end), seconds: seconds)
    }
    private var displayed: [PlatformSweep] { NativeTimeWindow.rows(source, within: window) }
    private var timeline: NativeSpectrumTimeline { NativeSpectrumTimeline(displayed, window: window) }
    private var selected: PlatformSweep? { NativeSpectrumInspection.selectedSweep(selectedID, in: displayed) }
    private var fullRange: ClosedRange<Double> {
        let first = Double(source.last?.frequenciesHz.first ?? 0)
        return first...max(first + 1, Double(source.last?.frequenciesHz.last ?? 1))
    }
    private var range: ClosedRange<Double> { zoom ?? fullRange }
    private var bounds: PlatformSpectrumScale? { inspectionScale ?? scale ?? PlatformSpectrumScale.fit(displayed.flatMap(\.levels)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                NativeSpectrumRangeHeader(sweep: selected, range: range).layoutPriority(1)
                Spacer(minLength: 6)
                if let bandPlanWorkspace {
                    NativeBandPlanButton(workspace: bandPlanWorkspace, range: source.isEmpty ? nil : range)
                }
            }
            HStack(spacing: 6) {
                Picker("Вид", selection: $threeDimensional) {
                    Text("2D").tag(false); Text("3D").tag(true)
                }.pickerStyle(.segmented).labelsHidden().frame(width: 70)
                Button("＋") { magnify(0.5) }.help("Масштаб графика; прибор не перестраивается")
                Button("−") { magnify(2) }
                Button("←") { pan(-0.25) }
                Button("→") { pan(0.25) }
                Button("Весь диапазон") { zoom = nil }
                Toggle("Полоса", isOn: $selectBand).toggleStyle(.button).disabled(threeDimensional)
                Menu("Измерить") {
                    Button("Измерить видимый участок") { measureRange(range) }
                    Button("Маркер → центр") {
                        if let marker = markers.last ?? cursor {
                            let half = (range.upperBound - range.lowerBound) / 2
                            measureRange(max(100_000, Double(marker.frequency) - half)...min(6_000_000_000, Double(marker.frequency) + half))
                        }
                    }.disabled(markers.isEmpty && cursor == nil)
                }.disabled(client == nil || tuning || !liveReceiving || holdingPresentation)
                Menu("История") {
                    Button("Следить за уровнем маркера") {
                        if let marker = markers.last ?? cursor { onFollowFrequency?(marker.frequency) }
                    }.disabled(onFollowFrequency == nil || (markers.isEmpty && cursor == nil))
                }
                Button("Пик") { choosePeak(next: true) }
                Toggle("Следить", isOn: $trackingPeak).toggleStyle(.button)
                Toggle("Min", isOn: $showMinimum).toggleStyle(.button)
                Toggle("Max", isOn: $showMaximum).toggleStyle(.button)
                Button("Шкала") { inspectionScale = PlatformSpectrumScale.fit(displayed.flatMap(\.levels)) }
                Menu("Эталон") {
                    Button("Закрепить выбранный свип") { reference = selected; referenceVisible = true }
                        .disabled(selected == nil)
                    Toggle("Показывать", isOn: $referenceVisible)
                    Button("Удалить с графика") { reference = nil }
                }
                Spacer(minLength: 0)
                Text(presentationStatus).foregroundStyle(!holdingPresentation && liveReceiving ? .green : .yellow)
            }.controlSize(.small).font(.caption)
            if !tuneMessage.isEmpty { Text(tuneMessage).font(.caption).foregroundStyle(.orange) }
            GeometryReader { geometry in
                let layout = NativeSpectrumPlotLayout(size: geometry.size, spectrumFraction: split)
                if threeDimensional {
                    NativeWaterfall3D(sweeps: displayed, range: range, bounds: bounds, window: window)
                        .simultaneousGesture(MagnifyGesture().onChanged { value in
                            if gestureRange == nil { gestureRange = range }
                            if let base = gestureRange { zoom = NativeSpectrumNavigation.zoom(base, factor: 1 / value.magnification, anchor: (base.lowerBound + base.upperBound) / 2, within: fullRange) }
                        }.onEnded { _ in gestureRange = nil })
                } else {
                NativeSpectrumPlot(sweeps: displayed, selected: selected, bounds: bounds, range: range,
                                   threshold: threshold, cursor: cursor, markers: markers, layout: layout,
                                   window: window, showMinimum: showMinimum, showMaximum: showMaximum,
                                   reference: referenceVisible ? reference : nil)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let point): inspect(point, layout: layout, pin: false)
                        case .ended: break
                        }
                    }
                    .onTapGesture { point in inspect(point, layout: layout, pin: true) }
                    .simultaneousGesture(MagnifyGesture().onChanged { value in
                        if gestureRange == nil { gestureRange = range }
                        if let base = gestureRange {
                            let anchor = layout.pinchAnchor(value.startAnchor.x, viewWidth: geometry.size.width, in: base)
                            zoom = NativeSpectrumNavigation.zoom(base, factor: 1 / value.magnification, anchor: anchor, within: fullRange)
                        }
                    }.onEnded { _ in gestureRange = nil })
                    .simultaneousGesture(DragGesture(minimumDistance: 5).onChanged { value in
                        guard layout.spectrum.contains(value.startLocation) || layout.waterfall.contains(value.startLocation) else { return }
                        if gestureRange == nil { gestureRange = range }
                        if selectBand { selectionStart = value.startLocation; selectionEnd = value.location }
                        else if let base = gestureRange {
                            let width = base.upperBound - base.lowerBound
                            zoom = NativeSpectrumNavigation.fitted(center: (base.lowerBound + base.upperBound) / 2 - value.translation.width / layout.spectrum.width * width, width: width, within: fullRange)
                        }
                    }.onEnded { value in
                        defer { gestureRange = nil; selectionStart = nil; selectionEnd = nil }
                        if selectBand, let base = gestureRange {
                            let a = min(1, max(0, (value.startLocation.x - layout.spectrum.minX) / layout.spectrum.width))
                            let b = min(1, max(0, (value.location.x - layout.spectrum.minX) / layout.spectrum.width))
                            if abs(a - b) > 0.005 {
                                let width = base.upperBound - base.lowerBound
                                zoom = NativeSpectrumNavigation.fitted(center: base.lowerBound + (a + b) / 2 * width, width: abs(a - b) * width, within: fullRange)
                            }
                        }
                    })
                    .overlay(alignment: .topLeading) {
                        if let a = selectionStart, let b = selectionEnd {
                            Rectangle().fill(.cyan.opacity(0.2)).overlay(Rectangle().stroke(.cyan))
                                .frame(width: abs(a.x - b.x), height: layout.waterfall.maxY - layout.spectrum.minY)
                                .offset(x: min(a.x, b.x), y: layout.spectrum.minY).allowsHitTesting(false)
                        }
                    }
                    .accessibilityLabel("Спектр и водопад. Частота по горизонтали, UTC по вертикали водопада.")
                    .overlay(alignment: .top) {
                        Capsule().fill(.secondary.opacity(0.7)).frame(width: 60, height: 5)
                            .padding(.vertical, 6).contentShape(Rectangle())
                            .offset(y: layout.spectrum.maxY + 18)
                            .gesture(DragGesture().onChanged { value in
                                if splitStart == nil { splitStart = split }
                                split = min(0.75, max(0.25, (splitStart ?? split) + value.translation.height / max(40, geometry.size.height - 62)))
                            }.onEnded { _ in splitStart = nil })
                            .accessibilityLabel("Соотношение спектра и водопада")
                            .accessibilityAdjustableAction { direction in
                                split = min(0.75, max(0.25, split + (direction == .increment ? 0.05 : -0.05)))
                            }
                    }
                }
            }.frame(minHeight: 120)
            HStack(spacing: 6) {
                Picker("Окно", selection: $seconds) {
                    Text("30 с").tag(30.0); Text("2 мин").tag(120.0); Text("10 мин").tag(600.0)
                }.frame(width: 130)
                Button("Раньше") { moveTime(-seconds / 2) }
                Button("Позже") { moveTime(seconds / 2) }
                Slider(value: $offset, in: 0...3600, onEditingChanged: { editing in
                    if !editing {
                        timeEnd = NativeSpectrumTimeline(sweeps).end.addingTimeInterval(-offset)
                        frozen = sweeps; loadHistory()
                    }
                }).help("Сдвиг до часа назад; отпускание загружает архивное окно")
                Button("Загрузить окно") { if timeEnd == nil { timeEnd = window.upperBound }; frozen = source; loadHistory() }
                    .disabled(client == nil || archive.busy)
                Button("Ещё") { loadHistory(next: true) }.disabled(archive.nextCursor == nil || archive.busy || historySweeps.count >= 128)
                Button("LIVE") { resumeLive() }
                    .tint(.green)
                if let setPresentationPaused {
                    Button(holdingPresentation ? "Продолжить показ" : "Пауза показа") {
                        if holdingPresentation { resumeLive() }
                        else { setPresentationPaused(true) }
                    }
                }
            }.font(.caption).controlSize(.small)
            if !archive.message.isEmpty {
                Text(archive.message + (archive.nextCursor == nil ? "" : (historySweeps.count >= 128 ? " · лимит 128 свипов; сузьте окно" : " · окно загружено частично")))
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            if let reference, referenceVisible {
                Text("Эталон: \(reference.id) · \(reference.frequenciesHz == selected?.frequenciesHz ? "совпадающая сетка; фиолетовая трасса" : "другая сетка — трасса скрыта")")
                    .font(.caption2).foregroundStyle(.purple).lineLimit(1)
            }
            NativeSpectrumCursorReadout(sample: cursor, inGap: inGap, markers: markers)
            HStack {
                if let bounds {
                    Text(String(format: "Цвет: %.1f", bounds.lower))
                    LinearGradient(colors: WaterfallPalette.stops.map { Color(red: $0.red, green: $0.green, blue: $0.blue) }, startPoint: .leading, endPoint: .trailing)
                        .frame(width: 90, height: 7)
                    Text(String(format: "%.1f дБм", bounds.upper))
                }
                Spacer()
                Text("\(displayed.count) свипов · клик: M1 / M2 · полный интервал: «Загрузить окно»")
                Button("Сброс маркеров") { markers = [] }.disabled(markers.isEmpty)
            }.font(.caption2).controlSize(.mini)
        }
        .padding(10)
        .background(Color(red: 0.055, green: 0.075, blue: 0.10), in: RoundedRectangle(cornerRadius: 10))
        .environment(\.colorScheme, .dark)
        .onChange(of: reviewRequest) { _, _ in
            guard let reviewedSweep else { returnToLive(); reference = nil; return }
            guard PlatformExactID.same(reviewedSweep.sessionId, sessionID), PlatformExactID.same(reviewedSweep.streamId, streamID) else { return }
            archiveTask?.cancel(); archive.reset(); historySweeps = []
            frozen = [reviewedSweep]; selectedID = reviewedSweep.exactID
            timeEnd = NativeSpectrumTimeline.date(reviewedSweep.interval.endedAt)
            cursor = nil; markers = []; zoom = nil; inspectionScale = PlatformSpectrumScale.fit(reviewedSweep.levels)
        }
        .onChange(of: seconds) { _, _ in
            archiveTask?.cancel(); archive.reset(); historySweeps = []; archiveWindow = nil
        }
        .task(id: !streamID.isEmpty && sweeps.last?.sessionId==sessionID && sweeps.last?.streamId==streamID) {
            guard let evidence=NativeAcceptanceTrace.shared,evidence.profile.phase=="load",
                  sessionID==evidence.profile.sessionID else{return}
            guard let token=NativeAcceptanceScenario.attach(session:sessionID,stream:streamID,
                sourceSession:sweeps.last?.sessionId,sourceStream:sweeps.last?.streamId,
                pan:{pan($0)},zoom:{magnify($0)}) else{return}
            defer {NativeAcceptanceScenario.detach(token)}
            while !Task.isCancelled {do {try await Task.sleep(for:.seconds(60))}catch{return}}
        }
        .onDisappear { archiveTask?.cancel(); tuneTask?.cancel() }
        .onChange(of: trackingPeak) { _, enabled in
            if enabled {
                let frequencyZoom = zoom
                resumeLive()
                zoom = frequencyZoom
            }
        }
        .onChange(of: frozen != nil) { _, viewingHistory in
            if viewingHistory { trackingPeak = false }
        }
        .onChange(of: sweeps.isEmpty) { _, empty in
            if empty { archiveTask?.cancel(); archive.reset(); timeEnd = nil; reference = nil; frozen = nil; selectedID = nil; cursor = nil; markers = []; zoom = nil; inGap = false }
        }
        .onChange(of: sweeps.last?.exactID) { _, _ in
            if trackingPeak, frozen == nil, let latest = sweeps.last,
               let peak = NativeSpectrumInspection.peaks(in: latest, range: range, limit: 1).first {
                cursor = NativeSpectrumSample(sweep: latest, bin: peak)
            }
            if frozen == nil, let cursor, !sweeps.contains(where: { $0.exactID == cursor.sweep.exactID }) { self.cursor = nil }
        }
        .onChange(of: sweeps.last?.frequenciesHz) { old, new in
            if frozen == nil, old != nil && old != new { frozen = nil; selectedID = nil; cursor = nil; markers = []; zoom = nil; inspectionScale = PlatformSpectrumScale.fit(sweeps.last?.levels ?? []) }
        }
    }

    private func measureRange(_ target: ClosedRange<Double>) {
        guard let client, !tuning else { return }
        let low = Int64(target.lowerBound.rounded()), high = Int64(target.upperBound.rounded())
        guard low < high else { return }
        tuning = true; tuneMessage = "Ожидание перестройки…"
        tuneTask = Task { @MainActor in
            defer { tuning = false }
            do {
                let status = try await client.tinySASettings(sessionID: sessionID, streamID: streamID)
                try Task.checkCancellation()
                _ = try await client.tinySAOperation(sessionID: sessionID, streamID: streamID, operation: "receive.configure",
                    parameters: ["start_hz": .integer(low), "stop_hz": .integer(high)], revision: status["revision"].integer)
                try Task.checkCancellation()
                tuneMessage = "Диапазон применён: \(low)–\(high) Гц. Ожидание нового измерения."
            } catch { if !Task.isCancelled { tuneMessage = error.localizedDescription } }
        }
    }

    private func returnToLive() {
        archiveTask?.cancel(); archive.reset(); historySweeps = []; archiveWindow = nil
        inspectionScale = nil; frozen = nil; selectedID = nil; timeEnd = nil; offset = 0; cursor = nil; markers = []; inGap = false; zoom = nil
    }

    private func resumeLive() {
        returnToLive()
        if presentationPaused { setPresentationPaused?(false) }
    }

    private func moveTime(_ delta: Double) {
        timeEnd = window.upperBound.addingTimeInterval(delta)
        frozen = source; cursor = nil; markers = []; selectedID = nil
        loadHistory()
    }
    private func loadHistory(next: Bool = false) {
        guard let client, !sessionID.isEmpty, !streamID.isEmpty else { return }
        if !next { archiveTask?.cancel(); archive.reset(); historySweeps = []; archiveWindow = window }
        guard let query = archiveWindow else { return }
        archiveTask = Task { @MainActor in
            await archive.search(client: client, session: sessionID, stream: streamID,
                start: NativeTimeWindow.iso(query.lowerBound), end: NativeTimeWindow.iso(query.upperBound.addingTimeInterval(0.001)),
                next: next, loadSources: true)
            guard !Task.isCancelled else { return }
            let known = Set(historySweeps.map { PlatformExactID($0.id) })
            historySweeps += archive.sources.filter { !known.contains(PlatformExactID($0.id)) }
            if archive.didLoad {
                frozen = historySweeps.sorted { $0.interval.startedAt < $1.interval.startedAt }
                selectedID = nil
            }
        }
    }

    private func inspect(_ point: CGPoint, layout: NativeSpectrumPlotLayout, pin: Bool) {
        let row: PlatformSweep?
        if layout.waterfall.contains(point) {
            row = timeline.row(at: (point.y - layout.waterfall.minY) / layout.waterfall.height)?.sweep
            inGap = row == nil
        } else if layout.spectrum.contains(point) { row = selected; inGap = false }
        else { return }
        guard let row else { cursor = nil; return }
        let frequency = range.lowerBound + (point.x - layout.spectrum.minX) / layout.spectrum.width * (range.upperBound - range.lowerBound)
        guard let bin = NativeSpectrumInspection.nearestBin(frequency, in: row) else { return }
        let sample = NativeSpectrumSample(sweep: row, bin: bin)
        cursor = sample
        if pin { pinSample(sample) }
    }
    private func pinSample(_ sample: NativeSpectrumSample) {
        if frozen == nil { timeEnd = window.upperBound; frozen = sweeps }
        selectedID = sample.sweep.exactID
        if markers.count == 2 { markers = [] }
        markers.append(sample)
        cursor = sample
    }
    private func choosePeak(next: Bool) {
        guard let selected else { return }
        let peaks = NativeSpectrumInspection.peaks(in: selected, range: range, limit: 20)
        guard !peaks.isEmpty else { return }
        peakIndex = next ? (peakIndex + 1) % peaks.count : 0
        pinSample(NativeSpectrumSample(sweep: selected, bin: peaks[peakIndex]))
    }
    private func magnify(_ factor: Double) {
        let center = cursor.map { Double($0.frequency) } ?? (range.lowerBound + range.upperBound) / 2
        zoom = NativeSpectrumNavigation.zoom(range, factor: factor, anchor: center, within: fullRange)
    }
    private func pan(_ fraction: Double) {
        let width = range.upperBound - range.lowerBound
        zoom = NativeSpectrumNavigation.fitted(center: (range.lowerBound + range.upperBound) / 2 + width * fraction, width: width, within: fullRange)
    }
}

struct NativeSpectrumRangeHeader: View {
    let sweep: PlatformSweep?
    let range: ClosedRange<Double>
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(NativeSpectrumInspection.frequency(range.lowerBound)) — \(NativeSpectrumInspection.frequency(range.upperBound))")
                .font(.system(.callout, design: .monospaced, weight: .semibold))
            if let sweep {
                let count = sweep.frequenciesHz.count
                let steps = zip(sweep.frequenciesHz, sweep.frequenciesHz.dropFirst()).map { $1 - $0 }
                let uniform = Set(steps).count == 1
                let step = uniform ? String(format: "%.3f кГц", Double(steps.first ?? 0) / 1000) : "неравномерный"
                HStack {
                    Text(NativeSpectrumInspection.interval(sweep))
                    if let start = NativeSpectrumTimeline.date(sweep.interval.startedAt), let end = NativeSpectrumTimeline.date(sweep.interval.endedAt) {
                        Text(String(format: "· свип %.2f с", end.timeIntervalSince(start)))
                    }
                    Text("· \(count) точек · шаг \(step) · RBW \(rbw(sweep))")
                }.font(.caption2).foregroundStyle(.secondary)
            } else { Text("Нет сохранённых свипов").font(.caption) }
        }.textSelection(.enabled)
    }
    private func rbw(_ sweep: PlatformSweep) -> String {
        guard let settings = sweep.settings else { return "не подтверждена" }
        // Raw settings retain whether a value was requested or actually read back.
        if case .number(let value) = settings["rbw_applied_hz"] { return String(format: "%.0f Гц (данные ядра)", value) }
        if case .number(let value) = settings["rbw_requested_hz"] ?? settings["rbw_hz"] { return String(format: "%.0f Гц (задано)", value) }
        return "не подтверждена"
    }
}

struct NativeSpectrumCursorReadout: View {
    let sample: NativeSpectrumSample?
    let inGap: Bool
    let markers: [NativeSpectrumSample]
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if let sample {
                Text("Курсор: \(sample.frequency) Гц · \(String(format: "%.1f", sample.level)) дБм · \(NativeSpectrumInspection.interval(sample.sweep))")
            } else { Text(inGap ? "Курсор: в этом интервале нет загруженного свипа" : "Курсор: частота · уровень · UTC (интервал свипа)") }
            ForEach(Array(markers.enumerated()), id: \.offset) { index, marker in
                Text("M\(index + 1): \(marker.frequency) Гц · \(String(format: "%.1f", marker.level)) дБм · \(NativeSpectrumInspection.interval(marker.sweep))")
                    .foregroundStyle(.yellow)
            }
            if markers.count == 2 { Text(NativeSpectrumInspection.delta(markers[0], markers[1])).foregroundStyle(.yellow) }

        }.font(.system(.caption, design: .monospaced)).textSelection(.enabled)
    }
}

struct NativeSpectrumPlotLayout {
    let spectrum: CGRect
    let waterfall: CGRect
    init(size: CGSize, spectrumFraction: Double = 0.48) {
        let width = max(1, size.width - 94)
        let available = max(40, size.height - 62)
        spectrum = CGRect(x: 76, y: 20, width: width, height: available * min(0.75, max(0.25, spectrumFraction)))
        waterfall = CGRect(x: 76, y: spectrum.maxY + 35, width: width, height: available - spectrum.height)
    }

    func pinchAnchor(_ viewFraction: Double, viewWidth: Double, in range: ClosedRange<Double>) -> Double {
        let plotFraction = min(1, max(0, (viewFraction * viewWidth - spectrum.minX) / spectrum.width))
        return range.lowerBound + plotFraction * (range.upperBound - range.lowerBound)
    }
}
