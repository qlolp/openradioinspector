import SwiftUI
import SpektrKomandy

struct NativeReceiverOriginalPane: View {
    let client: PlatformClient
    let source: NativeReceiverOriginalLink
    var hold: ((Int64) -> Void)?
    @Environment(\.dismiss) private var dismiss
    @State private var row: MonitoringValue = .null
    @State private var error = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Исходное наблюдение AR-DV1").font(.title2)
                Spacer()
                Button("Готово") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if row != .null {
                Text("\(row["frequency_hz"].scalar) Гц · \(row["level"].scalar) s_meter_count · \(row["mode"].text)").font(.headline)
                Text("UTC \(row["interval"]["started_at"].text) — \(row["interval"]["ended_at"].text)")
                if let measured = row["interval"]["ended_at"].utcDate {
                    TimelineView(.periodic(from: .now, by: 1)) { tick in
                        let age = tick.date.timeIntervalSince(measured)
                        Text(age >= 0 ? "Возраст точки · \(Int(min(age, Double(Int.max / 2)))) с" : "Часы узла опережают Mac")
                    }
                }
                Text("Ответ RX · " + row["metadata"]["raw_receiver_status"].scalar)
                if let hold {
                    Button("Удерживать эту частоту · приостановить обзор") { hold(source.frequencyHz); dismiss() }
                }
                if row["metadata"]["simulated"].bool == true { Text("Синтетические данные").foregroundStyle(.orange) }
                ScrollView { Text(row.pretty).font(.caption.monospaced()).frame(maxWidth: .infinity, alignment: .leading) }
            } else if !error.isEmpty { ErrorLine(text: error) }
            else { ProgressView("Чтение исходной записи…") }
        }
        .textSelection(.enabled).padding(18).frame(minWidth: 580, minHeight: 420)
        .task(id: source) {
            row = .null; error = ""
            do {
                let value = try await client.receiverOriginal(sessionID: source.session.rawValue, streamID: source.stream.rawValue,
                    pass: source.pass, frequencyHz: source.frequencyHz, observationID: source.observation.rawValue, fromEvent: source.archivedEvent)
                try Task.checkCancellation()
                row = value
            } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
    }
}

struct NativeReceiverPanoramaPane: View {
    struct Row: Identifiable {
        let value: PlatformReceiverPanorama
        let coverage: [ClosedRange<Int>]
        var id: Int { value.pass }
        init(_ value: PlatformReceiverPanorama) { self.value = value; coverage = value.measuredRanges() }
    }
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let client: PlatformClient
    let stream: PlatformStream
    let session: String
    @State private var rows: [Row] = []
    @State private var selectedPass: Int?
    @State private var error = ""
    @State private var source: NativeReceiverOriginalLink?
    @State private var heldDisplay = false
    @State private var reload = 0
    @State private var archiveInput = ""
    @State private var archivePass: Int?
    @State private var refreshedThrough = 0
    private var displayed: Row? { selectedPass.flatMap { p in rows.first { $0.id == p } } ?? rows.last }
    private var isCurrentProject: Bool {
        PlatformExactID.same(workspace.project["active_session_id"].text, session) &&
            PlatformExactID.same(workspace.project["primary_stream_id"].text, stream.id)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("Поточечная панорама AR-DV1").font(.headline)
                Spacer()
                Toggle("Пауза экрана", isOn: $heldDisplay).toggleStyle(.button)
                Button("Обновить") { reload += 1 }
            }
            Text("FM · S-метр, s_meter_count · меньший отсчёт означает более сильный сигнал.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                TextField("№ архивного прохода", text: $archiveInput).textFieldStyle(.roundedBorder).frame(width: 150)
                Button("Открыть проход") {
                    guard let number = Int(archiveInput), number > 0 else { return }
                    rows = []; selectedPass = nil; heldDisplay = false; archivePass = number; reload += 1
                }.disabled((Int(archiveInput) ?? 0) < 1)
                if archivePass != nil {
                    Button("К текущему проходу") {
                        rows = []; selectedPass = nil; archivePass = nil; heldDisplay = false; reload += 1
                    }
                }
            }.controlSize(.small)
            if let row = displayed {
                report(row)
                HStack {
                    Text("Сильнее ↑")
                    if let strong = row.value.bins.map(\.level).min(), let weak = row.value.bins.map(\.level).max() {
                        Text("\(strong.formatted())…\(weak.formatted()) s_meter_count")
                    }
                }.font(.caption).foregroundStyle(.secondary)
                spectrum(row)
                HStack {
                    Text("Водопад по проходам").font(.headline)
                    Spacer()
                    Picker("Проход", selection: $selectedPass) {
                        Text("Последний").tag(nil as Int?)
                        ForEach(rows.reversed()) { Text("№\($0.id)").tag(Optional($0.id)) }
                    }.frame(maxWidth: 225)
                }
                waterfall
                Text("Яркость и высота показывают сильнейший сохранённый отсчёт колонки. Пробелы обозначают отсутствие измерений. Нажмите на отметку для просмотра оригинала.")
                    .font(.caption).foregroundStyle(.secondary)
                if let bin = row.value.bins.min(by: { $0.level < $1.level }) {
                    Button("Исходный пик · \(bin.frequencyHz) Гц · \(bin.level.formatted()) s_meter_count") { open(bin, in: row) }
                        .controlSize(.small)
                }
            } else {
                ContentUnavailableView("Ожидание архивных точек", systemImage: "chart.dots.scatter",
                    description: Text("Начните пробный приём в подготовке дежурства."))
            }
            if !error.isEmpty { ErrorLine(text: error) }
            if isCurrentProject, workspace.isArmed {
                Text(workspace.project["coverage"].text == "paused" ? "Широкий обзор приостановлен оператором." :
                    "После события единственный AR-DV1 продолжает план. Удержание частоты включается оператором в карточке события.").font(.caption)
                if workspace.leases["receiver"] != nil {
                    Button("Вернуть приёмник к обзору") { workspace.perform { await workspace.manual("return", role: "receiver") } }
                        .disabled(workspace.researchPending.contains("control"))
                }
            }
        }
        .padding(10)
        .accessibilityElement(children: .contain).accessibilityIdentifier("ardv1.panorama")
        .task(id: [reload, heldDisplay ? 1 : 0]) {
            if heldDisplay && !rows.isEmpty { return }
            repeat {
                do {
                    let latest = try await client.receiverPanorama(sessionID: session, streamID: stream.id, pass: archivePass)
                    try Task.checkCancellation()
                    var recent = rows.filter { $0.id < latest.pass && $0.id >= latest.pass - 7 }
                    // A pass seen in progress may have closed between polls. Re-read it once.
                    if latest.pass > 1 {
                        for n in stride(from: latest.pass - 1, through: max(1, latest.pass - 7), by: -1) {
                            if recent.contains(where: { $0.id == n && ($0.value.complete || n <= refreshedThrough) }) { continue }
                            do {
                                let older = try await client.receiverPanorama(sessionID: session, streamID: stream.id, pass: n)
                                try Task.checkCancellation()
                                recent.removeAll { $0.id == n }
                                recent.append(Row(older))
                            } catch PlatformError.http(404, _, _) { break }
                        }
                    }
                    try Task.checkCancellation()
                    refreshedThrough = latest.pass - 1
                    rows = recent.sorted { $0.id < $1.id } + [Row(latest)]
                    if let selectedPass, !rows.contains(where: { $0.id == selectedPass }) { self.selectedPass = nil }
                    error = ""
                } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
            } while !Task.isCancelled && !heldDisplay && archivePass == nil
        }
        .sheet(item: $source) { link in
            NativeReceiverOriginalPane(client: client, source: link, hold: (isCurrentProject && workspace.isArmed && workspace.projectStatusIsCurrent()) ?
                { hz in workspace.perform { await workspace.holdPrimaryReceiver(frequencyHz: hz) } } : nil)
        }
    }

    private func report(_ row: Row) -> some View {
        let pass = row.value
        let previous = rows.first { $0.id == row.id - 1 }?.value
        return VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Проход №\(pass.pass) · \(pass.measuredPoints) / \(pass.totalPoints) точек · \(pass.complete ? "полный" : "неполный")")
                Spacer()
                Text("Шаг \(pass.stepHz) Гц")
            }
            Text("\(FrequencyText.formatMHzRussian(fromHertz: Int64(pass.startHz)))–\(FrequencyText.formatMHzRussian(fromHertz: Int64(pass.stopHz))) МГц")
            if let revisit = pass.revisitSeconds(after: previous) {
                Text("\(revisit.estimated ? "Оценка периода по измеренным точкам" : "Интервал между началами проходов") · \(revisit.seconds.formatted(.number.precision(.fractionLength(1)))) с")
            } else { Text("Период возврата · недостаточно измерений") }
            if let text = pass.endedAt, let time = try? PlatformTimestamp(text) {
                TimelineView(.periodic(from: .now, by: 1)) { timeline in
                    let age = timeline.date.timeIntervalSince1970 - Double(time.utcMicroseconds) / 1e6
                    Text("Последняя точка · \(text) · " + (age < 0 ? "часы узла опережают Mac" : "\(Int(min(age, Double(Int.max / 2)))) с назад"))
                }
            }
        }.font(.caption).monospacedDigit()
    }

    private func spectrum(_ row: Row) -> some View {
        let bins = row.value.bins
        let low = bins.map(\.level).min() ?? 0, high = bins.map(\.level).max() ?? 0
        return GeometryReader { geo in
            Canvas { context, size in
                for range in row.coverage {
                    let x = Double(range.lowerBound) / Double(row.value.totalPoints) * size.width
                    let w = Double(range.count) / Double(row.value.totalPoints) * size.width
                    context.fill(Path(CGRect(x: x, y: size.height - 4, width: w, height: 4)), with: .color(.cyan.opacity(0.65)))
                }
                for bin in bins {
                    let x = xFraction(bin, in: row) * size.width
                    let y = high == low ? 0.5 : (bin.level - low) / (high - low)
                    context.fill(Path(ellipseIn: CGRect(x: x - 1.5, y: 2 + y * max(0, size.height - 10), width: 3, height: 3)),
                                 with: .color(.cyan))
                }
            }.background(Color.black.opacity(0.9)).contentShape(Rectangle())
                .onTapGesture { point in select(column: point.x / max(1, geo.size.width), row: row) }
        }.frame(minHeight: 96, idealHeight: 150, maxHeight: 245)
        .accessibilityLabel("Панорама измеренных отсчётов, сверху \(low.formatted()), снизу \(high.formatted())")
    }

    private var waterfall: some View {
        let ordered = Array(rows.reversed())
        let all = ordered.flatMap { $0.value.bins.map(\.level) }
        let low = all.min() ?? 0, high = all.max() ?? 0
        return GeometryReader { geo in
            Canvas { context, size in
                let height = size.height / CGFloat(max(1, ordered.count))
                for (i, row) in ordered.enumerated() {
                    let total = Double(row.value.totalPoints)
                    var rangeIndex = 0
                    for bin in row.value.bins {
                        let strength = high == low ? 1 : 1 - (bin.level - low) / (high - low)
                        let color = Color.cyan.opacity(0.2 + 0.8 * strength)
                        while rangeIndex < row.coverage.count && row.coverage[rangeIndex].upperBound < bin.startIndex { rangeIndex += 1 }
                        var j = rangeIndex
                        while j < row.coverage.count, row.coverage[j].lowerBound <= bin.endIndex {
                            let first = max(row.coverage[j].lowerBound, bin.startIndex), last = min(row.coverage[j].upperBound, bin.endIndex)
                            let x = Double(first) / total * size.width
                            let w = Double(last - first + 1) / total * size.width
                            context.fill(Path(CGRect(x: x, y: Double(i) * height, width: w, height: max(1, height - 1))), with: .color(color))
                            j += 1
                        }
                    }
                }
            }.background(Color.black.opacity(0.9)).contentShape(Rectangle())
                .onTapGesture { point in
                    let i = min(ordered.count - 1, max(0, Int(point.y / max(1, geo.size.height) * Double(ordered.count))))
                    guard ordered.indices.contains(i) else { return }
                    select(column: point.x / max(1, geo.size.width), row: ordered[i])
                }
        }.frame(minHeight: 46, idealHeight: 85, maxHeight: 135)
    }

    private func xFraction(_ bin: PlatformReceiverPanorama.Bin, in row: Row) -> Double {
        Double(bin.frequencyHz - Int64(row.value.startHz)) / Double(row.value.stopHz - row.value.startHz)
    }

    private func select(column: CGFloat, row: Row) {
        let index = min(row.value.totalPoints - 1, max(0, Int(column * Double(row.value.totalPoints))))
        guard row.coverage.contains(where: { $0.contains(index) }),
              let bin = row.value.bins.first(where: { ($0.startIndex...$0.endIndex).contains(index) }) else {
            error = "В этой части прохода нет подтверждённого отсчёта."; return
        }
        open(bin, in: row)
    }
    private func open(_ bin: PlatformReceiverPanorama.Bin, in row: Row) {
        error = ""
        source = .init(session: PlatformExactID(session), stream: PlatformExactID(stream.id),
            observation: PlatformExactID(bin.observationId), pass: row.id, frequencyHz: bin.frequencyHz)
    }
}
