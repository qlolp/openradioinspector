import SwiftUI
import SpektrKomandy

/// Both panels use identical frequency coordinates, saved sweep IDs and UTC intervals.
struct NativeSpectrumInspector: View {
    let sweeps: [PlatformSweep]
    let scale: PlatformSpectrumScale?
    let threshold: Double?
    @State private var frozen: [PlatformSweep]?
    @State private var selectedID: String?
    @State private var cursor: NativeSpectrumSample?
    @State private var inGap = false
    @State private var markers: [NativeSpectrumSample] = []
    @State private var zoom: ClosedRange<Double>?
    @State private var peakIndex = 0

    private var displayed: [PlatformSweep] { frozen ?? sweeps }
    private var selected: PlatformSweep? { displayed.first { $0.id == selectedID } ?? displayed.last }
    private var fullRange: ClosedRange<Double> {
        let first = Double(displayed.last?.frequenciesHz.first ?? 0)
        return first...max(first + 1, Double(displayed.last?.frequenciesHz.last ?? 1))
    }
    private var range: ClosedRange<Double> { zoom ?? fullRange }
    private var bounds: PlatformSpectrumScale? { scale ?? PlatformSpectrumScale.fit(displayed.flatMap(\.levels)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            NativeSpectrumRangeHeader(sweep: selected, range: range)
            HStack(spacing: 8) {
                Text(frozen == nil ? "ТЕКУЩИЙ СПЕКТР" : "ИСТОРИЯ · показ зафиксирован").font(.caption.bold())
                    .foregroundStyle(frozen == nil ? Color.secondary : .yellow)
                Spacer()
                Button("На максимум") { choosePeak(next: false) }
                Button("Следующий пик") { choosePeak(next: true) }
                Button("＋") { magnify(0.5) }.help("Приблизить обе частотные шкалы вокруг курсора")
                Button("−") { magnify(2) }
                Button("Весь диапазон") { zoom = nil }
                Button("К текущим данным") { frozen = nil; selectedID = nil; cursor = nil; markers = []; inGap = false }
                    .disabled(frozen == nil && markers.isEmpty)
            }.controlSize(.small)
            HStack {
                Picker("Свип UTC", selection: Binding(get: { selectedID ?? "" }, set: { id in
                    if id.isEmpty { frozen = nil; selectedID = nil; cursor = nil; markers = [] }
                    else { if frozen == nil { frozen = displayed }; selectedID = id }
                })) {
                    Text("Последний / LIVE").tag("")
                    ForEach(displayed.reversed()) { sweep in
                        Text(NativeSpectrumInspection.interval(sweep)).tag(sweep.id)
                    }
                }.frame(maxWidth: 440)
                Spacer()
                Text("Белый: выбранный · голубой: min · оранжевый: max").foregroundStyle(.secondary)
            }.font(.caption).controlSize(.small)
            GeometryReader { geometry in
                let layout = NativeSpectrumPlotLayout(size: geometry.size)
                NativeSpectrumPlot(sweeps: displayed, selected: selected, bounds: bounds, range: range,
                                   threshold: threshold, cursor: cursor, markers: markers, layout: layout)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let point): inspect(point, layout: layout, pin: false)
                        case .ended: break
                        }
                    }
                    .onTapGesture { point in inspect(point, layout: layout, pin: true) }
                    .accessibilityLabel("Спектр и водопад. Частота по горизонтали, UTC по вертикали водопада.")
            }.frame(minHeight: 210)
            NativeSpectrumCursorReadout(sample: cursor, inGap: inGap, markers: markers)
            HStack {
                if let bounds {
                    Text(String(format: "Цвет: %.1f", bounds.lower))
                    LinearGradient(colors: WaterfallPalette.stops.map { Color(red: $0.red, green: $0.green, blue: $0.blue) }, startPoint: .leading, endPoint: .trailing)
                        .frame(width: 90, height: 7)
                    Text(String(format: "%.1f дБм", bounds.upper))
                }
                Spacer()
                Text("Клик: M1 / M2 · выбор истории · пробелы = нет измерений")
                Button("Сброс маркеров") { markers = [] }.disabled(markers.isEmpty)
            }.font(.caption2).controlSize(.mini)
        }
        .padding(10)
        .background(Color(red: 0.055, green: 0.075, blue: 0.10), in: RoundedRectangle(cornerRadius: 10))
        .environment(\.colorScheme, .dark)
        .onChange(of: sweeps.isEmpty) { _, empty in
            if empty { frozen = nil; selectedID = nil; cursor = nil; markers = []; zoom = nil; inGap = false }
        }
        .onChange(of: sweeps.last?.id) { _, _ in
            if frozen == nil, let cursor, !sweeps.contains(where: { $0.id == cursor.sweep.id }) { self.cursor = nil }
        }
        .onChange(of: sweeps.last?.frequenciesHz) { old, new in
            if old != nil && old != new { frozen = nil; selectedID = nil; cursor = nil; markers = []; zoom = nil }
        }
    }

    private func inspect(_ point: CGPoint, layout: NativeSpectrumPlotLayout, pin: Bool) {
        let row: PlatformSweep?
        if layout.waterfall.contains(point) {
            row = NativeSpectrumTimeline(displayed).row(at: (point.y - layout.waterfall.minY) / layout.waterfall.height)?.sweep
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
        if frozen == nil { frozen = displayed }
        selectedID = sample.sweep.id
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
        let full = fullRange.upperBound - fullRange.lowerBound
        let width = min(full, max(full / 32, (range.upperBound - range.lowerBound) * factor))
        let center = cursor.map { Double($0.frequency) } ?? (range.lowerBound + range.upperBound) / 2
        let low = max(fullRange.lowerBound, min(fullRange.upperBound - width, center - width / 2))
        zoom = low...(low + width)
    }
}

struct NativeSpectrumRangeHeader: View {
    let sweep: PlatformSweep?
    let range: ClosedRange<Double>
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("\(NativeSpectrumInspection.frequency(range.lowerBound)) — \(NativeSpectrumInspection.frequency(range.upperBound))")
                .font(.system(.title3, design: .monospaced, weight: .semibold))
            if let sweep {
                let count = sweep.frequenciesHz.count
                let steps = zip(sweep.frequenciesHz, sweep.frequenciesHz.dropFirst()).map { $1 - $0 }
                let uniform = Set(steps).count == 1
                let step = uniform ? String(format: "%.3f кГц", Double(steps.first ?? 0) / 1000) : "неравномерный"
                Text("Центр \(NativeSpectrumInspection.frequency((range.lowerBound + range.upperBound) / 2)) · ширина \(NativeSpectrumInspection.frequency(range.upperBound - range.lowerBound)) · \(count) точек · шаг \(step)")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Text(NativeSpectrumInspection.interval(sweep))
                    if let start = NativeSpectrumTimeline.date(sweep.interval.startedAt), let end = NativeSpectrumTimeline.date(sweep.interval.endedAt) {
                        Text(String(format: "· свип %.2f с", end.timeIntervalSince(start)))
                    }
                    Text("· RBW: \(rbw(sweep))")
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
            } else { Text(inGap ? "Курсор: промежуток между свипами — измерений нет" : "Наведите курсор на спектр или водопад; клик закрепляет отсчёт") }
            ForEach(Array(markers.enumerated()), id: \.offset) { index, marker in
                Text("M\(index + 1): \(marker.frequency) Гц · \(String(format: "%.1f", marker.level)) дБм · \(NativeSpectrumInspection.interval(marker.sweep))")
                    .foregroundStyle(.yellow)
            }
            if markers.count == 2 { Text(NativeSpectrumInspection.delta(markers[0], markers[1])).foregroundStyle(.yellow) }
            else { Text("Время относится ко всему свипу; время отдельной точки неизвестно.").foregroundStyle(.secondary) }
        }.font(.system(.caption, design: .monospaced)).textSelection(.enabled)
    }
}

struct NativeSpectrumPlotLayout {
    let spectrum: CGRect
    let waterfall: CGRect
    init(size: CGSize) {
        let width = max(1, size.width - 94)
        let available = max(100, size.height - 62)
        spectrum = CGRect(x: 76, y: 20, width: width, height: available * 0.48)
        waterfall = CGRect(x: 76, y: spectrum.maxY + 35, width: width, height: available * 0.52)
    }
}
