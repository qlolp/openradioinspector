import SwiftUI
import SpektrKomandy

struct NativeRFExplorerSweepAnalysis: View {
    let client: PlatformClient
    let target: PlatformSweep
    let features: Set<String>
    @Binding var reference: PlatformSweep?
    @Environment(\.dismiss) private var dismiss

    @State private var wantEnvelope = false
    @State private var startMHz = ""
    @State private var stopMHz = ""
    @State private var upper = "3"
    @State private var lower = "3"
    @State private var archived: PlatformArchiveSweepPage?
    @State private var saved: [PlatformSweepReference] = []
    @State private var band: PlatformRFExplorerBandPower?
    @State private var envelope: PlatformSweepTolerance?
    @State private var selectedBin = 0
    @State private var busy = false
    @State private var message = ""
    @State private var catalogMessage = ""
    @State private var operation: Task<Void, Never>?
    @State private var catalogTask: Task<Void, Never>?

    static func offered(_ features: Set<String>) -> Bool {
        features.contains("rf_explorer_band_power") || features.contains("sweep_tolerance_envelope")
    }
    private var supportsPower: Bool { features.contains("rf_explorer_band_power") }
    private var supportsTolerance: Bool { features.contains("sweep_tolerance_envelope") }
    private var tolerance: Bool { supportsTolerance && (!supportsPower || wantEnvelope) }
    private var deviations: (Double, Double)? {
        guard let up = Double(upper.replacingOccurrences(of: ",", with: ".")),
              let down = Double(lower.replacingOccurrences(of: ",", with: ".")),
              up.isFinite, down.isFinite, up >= 0, down >= 0 else { return nil }
        return (up, down)
    }
    private var bandRange: ClosedRange<Int64>? {
        guard let start = try? NativeHistoryFrequencyInput.hz(startMHz), let stop = try? NativeHistoryFrequencyInput.hz(stopMHz),
              start < stop, target.frequenciesHz.contains(start), target.frequenciesHz.contains(stop) else { return nil }
        return start...stop
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack {
                Text("Анализ исходного свипа").font(.title2)
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            Text("Свип \(target.id) · \(target.interval.startedAt) — \(target.interval.endedAt) UTC").textSelection(.enabled)
            Text(NativeSpectrumRangeHeader.acquisitionDetails(target, rfExplorer: true)).font(.caption).foregroundStyle(.secondary)
            if supportsPower && supportsTolerance {
                Picker("Операция", selection: $wantEnvelope) {
                    Text("Мощность в полосе").tag(false)
                    Text("Допуск к эталону").tag(true)
                }.pickerStyle(.segmented)
            }
            if tolerance { envelopeControls } else if supportsPower { powerControls }
            if busy { ProgressView().controlSize(.small) }
            if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.orange) }
        }
        .padding(16).frame(minWidth: 650).controlSize(.small)
        .onAppear { fullBand() }
        .task(id: tolerance) {
            guard tolerance else { return }
            await loadArchive(after: nil)
            if features.contains("named_sweep_references"), !Task.isCancelled {
                do {
                    let page = try await client.sweepReferences(sessionID: target.sessionId, streamID: target.streamId)
                    try Task.checkCancellation(); saved = page.items
                } catch { if !Task.isCancelled { catalogMessage = error.localizedDescription } }
            }
        }
        .onChange(of: [upper, lower, startMHz, stopMHz]) { _, _ in invalidate() }
        .onChange(of: wantEnvelope) { _, _ in invalidate() }
        .onDisappear { operation?.cancel(); catalogTask?.cancel() }
    }

    private var powerControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Мощность по измеренным отсчётам одного окна и записанной RBW").font(.headline)
            HStack {
                LabeledContent("Начало, МГц") { TextField("МГц", text: $startMHz) }
                LabeledContent("Конец, МГц") { TextField("МГц", text: $stopMHz) }
                Button("Весь свип") { fullBand() }
            }.textFieldStyle(.roundedBorder)
            Button("Рассчитать мощность полосы") {
                guard let range = bandRange else { return }
                perform {
                    let value = try await client.rfExplorerBandPower(source: target, startHz: range.lowerBound, stopHz: range.upperBound)
                    try Task.checkCancellation(); band = value
                }
            }.disabled(busy || bandRange == nil)
            if bandRange == nil {
                Text("Обе границы должны совпадать с частотами исходного свипа.").font(.caption).foregroundStyle(.secondary)
            }
            if let band {
                Text("\(band.value.formatted(.number.precision(.fractionLength(2)))) дБм").font(.title2.monospacedDigit())
                Text("\(band.start_hz)–\(band.stop_hz) Гц · индексы \(band.start_index)…\(band.stop_index) · RBW \(band.rbw_hz) Гц")
                Text("Интегрирование трапециями · ENBW не калибрована · " +
                     (band.uncertainty_db.map { "неопределённость \($0) дБ" } ?? "неопределённость неизвестна"))
                    .font(.caption)
                Text("Аппаратная валидация этого расчёта не проводилась.").font(.caption)
            }
        }
    }

    private var envelopeControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Menu("Эталон из архива") {
                    Button("Закрепить открытый свип") { invalidate(); reference = target }
                    ForEach(saved, id: \.exactID) { item in
                        Button("\(item.name) · \(item.sweepId)") { selectReference(item.sweepId) }
                    }
                    Divider()
                    ForEach(archived?.items ?? [], id: \.exactID) { row in
                        Button("\(row.id) · \(row.interval.startedAt)") { selectReference(row.id) }
                    }
                }
                if let after = archived?.nextCursor {
                    Button("Более ранние свипы") {
                        catalogTask?.cancel(); catalogTask = Task { await loadArchive(after: after) }
                    }
                }
                if let reference { Text("Эталон \(reference.id) · \(reference.interval.startedAt) UTC").textSelection(.enabled) }
            }
            if !catalogMessage.isEmpty { Text(catalogMessage).font(.caption).foregroundStyle(.orange) }
            HStack {
                LabeledContent("Вверх, дБ") { TextField("3", text: $upper).frame(width: 60) }
                LabeledContent("Вниз, дБ") { TextField("3", text: $lower).frame(width: 60) }
                Button("Сопоставить с эталоном") {
                    guard let reference, let (up, down) = deviations else { return }
                    perform {
                        let result = try await client.sweepTolerance(reference: reference, target: target, upperDb: up, lowerDb: down)
                        try Task.checkCancellation()
                        envelope = result
                        selectedBin = result.bins.firstIndex(where: { $0.classification == .above || $0.classification == .below }) ?? 0
                    }
                }.disabled(busy || reference == nil || deviations == nil)
            }.textFieldStyle(.roundedBorder)
            if let envelope {
                let totals = envelope.counts
                Text("Выше \(totals.above) · ниже \(totals.below) · в допуске \(totals.within) · без покрытия \(totals.uncovered)")
                    .fontWeight(.semibold)
                NativeRFExplorerEnvelopePlot(result: envelope, target: target, index: $selectedBin)
                let bin = envelope.bins[selectedBin]
                Text("\(bin.frequency_hz) Гц · индекс \(bin.target_index) · \(target.levels[selectedBin].formatted()) дБм")
                if let low = bin.lower_dbm, let high = bin.upper_dbm, let ref = bin.reference_index, let delta = bin.delta_db {
                    Text("Пределы \(low.formatted())…\(high.formatted()) дБм · эталон [\(ref)] · Δ \(delta.formatted()) дБ")
                } else { Text("Опорный свип не содержит этой частоты.").foregroundStyle(.secondary) }
            }
        }
    }

    private func fullBand() {
        guard let first = target.frequenciesHz.first, let last = target.frequenciesHz.last else { return }
        startMHz = NativeHistoryFrequencyInput.mhz(first); stopMHz = NativeHistoryFrequencyInput.mhz(last)
    }
    private func loadArchive(after: String?) async {
        do {
            let page = try await client.archiveSweepPage(sessionID: target.sessionId, streamID: target.streamId, after: after)
            try Task.checkCancellation(); archived = page; catalogMessage = ""
        } catch { if !Task.isCancelled { catalogMessage = error.localizedDescription } }
    }
    private func selectReference(_ id: String) {
        perform {
            let value = try await client.archiveOriginalSweep(sessionID: target.sessionId, streamID: target.streamId, sweepID: id)
            try Task.checkCancellation(); reference = value
        }
    }
    private func invalidate() {
        operation?.cancel(); busy = false; message = ""; band = nil; envelope = nil
    }
    private func perform(_ action: @escaping @MainActor () async throws -> Void) {
        invalidate(); busy = true
        operation = Task { @MainActor in
            defer { if !Task.isCancelled { busy = false } }
            do { try await action() }
            catch PlatformError.http(status: 403, code: "insufficient_scope", message: _) {
                if !Task.isCancelled { message = "Для запроса расчёта нужны права оператора. Доступ к исходным свипам сохранён." }
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
}

private struct NativeRFExplorerEnvelopePlot: View {
    let result: PlatformSweepTolerance
    let target: PlatformSweep
    @Binding var index: Int

    var body: some View {
        let levels = target.levels + result.bins.flatMap { [$0.lower_dbm, $0.upper_dbm].compactMap { $0 } }
        let scale = PlatformSpectrumScale.fit(levels)
        VStack(alignment: .leading, spacing: 2) {
            Text("Зелёный · границы допуска   Голубой · цель   Красный · превышение   Серый · без покрытия")
                .font(.caption2)
            GeometryReader { geometry in
                Canvas { ctx, size in
                    guard let scale, let first = target.frequenciesHz.first, let last = target.frequenciesHz.last else { return }
                    let width = Double(max(1, last - first))
                    func point(_ i: Int, _ level: Double) -> CGPoint {
                        .init(x: size.width * CGFloat(Double(target.frequenciesHz[i] - first) / width),
                              y: size.height * CGFloat(1 - scale.fraction(level)))
                    }
                    var top = Path(), bottom = Path(), trace = Path(), inBounds = false
                    for (i, bin) in result.bins.enumerated() {
                        let measured = point(i, target.levels[i])
                        if i == 0 { trace.move(to: measured) } else { trace.addLine(to: measured) }
                        if let low = bin.lower_dbm, let high = bin.upper_dbm {
                            if inBounds { top.addLine(to: point(i, high)); bottom.addLine(to: point(i, low)) }
                            else { top.move(to: point(i, high)); bottom.move(to: point(i, low)) }
                            inBounds = true
                        } else { inBounds = false }
                        if bin.classification != .within {
                            let rect = CGRect(x: measured.x - 2, y: measured.y - 2, width: 4, height: 4)
                            ctx.fill(Path(ellipseIn: rect), with: .color(bin.classification == .uncovered ? .gray : .red))
                        }
                    }
                    ctx.stroke(top, with: .color(.green)); ctx.stroke(bottom, with: .color(.green))
                    ctx.stroke(trace, with: .color(.cyan))
                    let p = point(index, target.levels[index])
                    ctx.stroke(Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8)), with: .color(.white))
                }
                .background(Color(red: 0.045, green: 0.065, blue: 0.09))
                .contentShape(Rectangle()).onTapGesture { p in
                    guard let first = target.frequenciesHz.first, let last = target.frequenciesHz.last else { return }
                    let fraction = Double(p.x / max(1, geometry.size.width))
                    index = NativeSpectrumInspection.nearestBin(Double(first) + fraction * Double(last - first), in: target) ?? 0
                }
            }.frame(height: 150)
        }
    }
}
