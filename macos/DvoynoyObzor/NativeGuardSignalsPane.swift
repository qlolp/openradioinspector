import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

struct NativeGuardSignalsPane: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let openGuard: () -> Void
    var compact = false
    var research = false
    var openReceiver: (() -> Void)?
    var remote: RemoteDualModel?
    @State private var confirming = false

    private struct DetailScope: Equatable {
        let context: Int
        let recognition: Bool
        let eventID: PlatformExactID?
    }

    private var selection: Binding<PlatformExactID?> {
        Binding(get: { workspace.signalQueue.selectedID }, set: workspace.selectSignalEvent)
    }

    private var detailScope: DetailScope {
        DetailScope(context: workspace.signalContext, recognition: workspace.signalCatalogAvailable,
            eventID: workspace.signalQueue.selectedID)
    }

    private var detail: NativeGuardSignal? {
        if workspace.signalEvent == .null { return nil }
        return NativeGuardSignal(value: workspace.signalEvent)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if workspace.selected.isEmpty {
                ContentUnavailableView {
                    Label("Выберите проект охраны", systemImage: "waveform.path")
                } actions: {
                    Button("Открыть охрану", action: openGuard)
                }
            } else {
                if compact {
                    HStack {
                        Label("События", systemImage: "waveform.path.badge.magnifyingglass").font(.headline)
                        Spacer()
                        Button("Настройки", action: openGuard).labelStyle(.iconOnly)
                    }
                    freshness
                    compactActions
                } else { actions }
                ErrorLine(text: workspace.signalErrors[.inbox] ?? "")
                if compact { compactPageControls } else { pageControls }
                ErrorLine(text: workspace.signalErrors[.inboxPage] ?? "")
                if !workspace.knownSignalNotice.isEmpty {
                    Text(workspace.knownSignalNotice).font(.caption)
                }
                VSplitView {
                    Group {
                        if compact { compactQueue } else { queueTable }
                    }.frame(minHeight: 130)
                    evidence.frame(minHeight: 160, maxHeight: .infinity)
                }
            }
        }
        .padding(10)
        .task(id: detailScope) {
            if let id = detailScope.eventID { await workspace.loadSignalEvent(id) }
        }
        .onChange(of: detailScope) { _, _ in confirming = false }
        .sheet(isPresented: $confirming) {
            if let id = workspace.signalQueue.selectedID {
                NativeKnownSignalEditor(workspace: workspace, eventID: id)
            }
        }

    }

    private var compactActions: some View {
        HStack {
            Button("Открыть исходник") {
                guard let detail, let source = detail.source else { return }
                workspace.perform { await workspace.openSignalOriginal(eventID: detail.id, source: source) }
            }.disabled(detail?.source == nil || workspace.signalPending.contains(.original))
                .accessibilityIdentifier("observation.open-source")
            Menu("Ещё") {
                Button("Обновить события") { workspace.perform { await workspace.refreshSignalPage() } }
                if workspace.signalCatalogAvailable {
                    Button("Сохранить в каталог…") { confirming = true }.disabled(detail == nil)
                }
                Button("Кандидаты и правила…", action: openGuard)
            }
        }.controlSize(.small)
    }

    private var compactPageControls: some View {
        HStack {
            Button("Последние") { workspace.showLatestSignalEvents() }
                .disabled(workspace.signalQueue.isLatest || workspace.signalPending.contains(.inboxPage))
            Spacer(minLength: 0)
            Button("Новее") { workspace.perform { await workspace.newerSignalEvents() } }
                .disabled(workspace.signalQueue.position == nil || workspace.signalPending.contains(.inboxPage))
            Button("Старше") { workspace.perform { await workspace.olderSignalEvents() } }
                .disabled(workspace.signalQueue.olderPosition == nil || workspace.signalPending.contains(.inboxPage))
        }.controlSize(.small)
    }

    private var compactQueue: some View {
        List(selection: selection) {
            ForEach(workspace.signalQueue.rows) { signal in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        if workspace.signalQueue.unread.contains(signal.id) {
                            Image(systemName: "circle.fill").foregroundStyle(Color.accentColor)
                                .font(.caption2).accessibilityLabel("Не просмотрено")
                        }
                        Text(NativeGuardSignal.frequency(signal.value["center_frequency_hz"]) + " МГц")
                            .font(.callout.weight(.semibold)).monospacedDigit()
                        Spacer(minLength: 0)
                    }
                    Text(signal.detectorKind).font(.caption).lineLimit(2)
                    Text(signal.value["last_seen"].scalar + " UTC").font(.caption2)
                        .foregroundStyle(.secondary).monospacedDigit()
                }.padding(.vertical, 4).tag(signal.id)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("observation.event." + signal.id.rawValue)
            }
        }.overlay {
            if workspace.signalQueue.refreshedAt != nil && workspace.signalQueue.rows.isEmpty {
                Text("Новых событий пока нет").font(.callout).foregroundStyle(.secondary)
                    .allowsHitTesting(false)
            }
        }
    }

    private var actions: some View {
        HStack {
            freshness
            Spacer()
            Button(workspace.signalQueue.isLatest ? "Обновить события" : "Обновить страницу") {
                workspace.perform { await workspace.refreshSignalPage() }
            }.disabled(workspace.signalPending.contains(workspace.signalQueue.isLatest ? .inbox : .inboxPage))
            Button("Исходный свип…") {
                guard let detail, let source = detail.source else { return }
                workspace.perform { await workspace.openSignalOriginal(eventID: detail.id, source: source) }
            }
            .disabled(detail?.source == nil || workspace.signalPending.contains(.original))
            .help(detail?.source.map { "Свип \($0.sweepID.rawValue)" } ?? "Выберите событие с архивной ссылкой")
            if workspace.signalCatalogAvailable {
                Button("Сохранить в каталог…") {
                    workspace.clearSignalError(.confirm)
                    confirming = true
                }.disabled(detail == nil || workspace.signalPending.contains(.confirm))
            }
        }
    }

    private var pageControls: some View {
        let position = workspace.signalQueue.position
        let browsing = workspace.signalPending.contains(.inboxPage)
        let canGoNewer = position?.page == 2 || workspace.signalQueue.newerPosition != nil
        return HStack(spacing: 8) {
            Button("К последним") { workspace.showLatestSignalEvents() }.disabled(position == nil && !browsing)
            Button("Новее") { workspace.perform { await workspace.newerSignalEvents() } }
                .disabled(!canGoNewer || browsing)
            Text(position.map { "Страница \($0.page)" } ?? "Страница 1 · последние события")
                .monospacedDigit().foregroundStyle(.secondary)
            Button("Старше") { workspace.perform { await workspace.olderSignalEvents() } }
                .disabled(workspace.signalQueue.olderPosition == nil || browsing)
            if browsing { ProgressView().controlSize(.small) }
        }.controlSize(.small)
    }

    private var freshness: some View {
        TimelineView(.periodic(from: .now, by: 5)) { time in
            if let refreshedAt = workspace.signalQueue.refreshedAt {
                let stale = workspace.signalErrors[.inbox] != nil || time.date.timeIntervalSince(refreshedAt) > 15
                Label(stale ? "Очередь устарела · \(NativeTimeWindow.iso(refreshedAt))" :
                    "Очередь обновлена · \(NativeTimeWindow.iso(refreshedAt))", systemImage: stale ? "exclamationmark.clock" : "clock")
                    .foregroundStyle(stale ? Color.orange : Color.secondary)
            } else if workspace.signalPending.contains(.inbox) {
                ProgressView("Загрузка событий…").controlSize(.small)
            } else {
                Label("Очередь ещё не обновлялась", systemImage: "clock")
                    .foregroundStyle(.secondary)
            }
        }.font(.caption.monospacedDigit())
    }

    private var queueTable: some View {
        Table(workspace.signalQueue.rows, selection: selection) {
            TableColumn("Распознавание") { signal in
                if workspace.signalQueue.unread.contains(signal.id) {
                    Label(signal.recognition, systemImage: "circle.fill")
                        .labelStyle(.titleAndIcon)
                        .accessibilityLabel("Непросмотренное событие. \(signal.recognition)")
                } else { Text(signal.recognition) }
            }
            .width(min: 145, ideal: 170)
            TableColumn("Частота, МГц") { Text(NativeGuardSignal.frequency($0.value["center_frequency_hz"])).monospacedDigit() }
                .width(min: 115, ideal: 130)
            TableColumn("Пик") { Text(NativeGuardSignal.level($0.value["peak_level"], unit: "дБм")).monospacedDigit() }
                .width(min: 75, ideal: 95)
            TableColumn("Превышение") { Text(NativeGuardSignal.level($0.value["peak_excess_db"], unit: "дБ")).monospacedDigit() }
                .width(min: 90, ideal: 110)
            TableColumn("Приёмник") { Text($0.receiver) }
                .width(min: 160, ideal: 210)
            TableColumn("Последнее наблюдение UTC") { Text($0.value["last_seen"].scalar).monospacedDigit() }
                .width(min: 170, ideal: 220)
        }
        .overlay {
            if workspace.signalQueue.refreshedAt != nil && workspace.signalQueue.rows.isEmpty {
                Text(workspace.signalQueue.isLatest ? "В проекте пока нет событий охраны" : "На этой странице нет событий")
                    .foregroundStyle(.secondary).allowsHitTesting(false)
            }
        }
    }

    private var evidence: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if let summary = workspace.signalQueue.selected {
                    if research {
                        NativePairedRechecksPane(workspace: workspace, eventID: summary.id).id(summary.id)
                    }
                    if compact {
                        NativeConsoleEventCard(workspace: workspace, summary: summary, detail: detail,
                                               openReceiver: openReceiver ?? openGuard, remote: remote)
                    } else { NativeGuardSignalEvidence(summary: summary, detail: detail) }
                    if workspace.signalPending.contains(.detail) && detail == nil {
                        ProgressView("Загрузка карточки…").controlSize(.small)
                    }
                    if workspace.signalPending.contains(.original) {
                        ProgressView("Загрузка исходного свипа…").controlSize(.small)
                    }
                    ErrorLine(text: workspace.signalErrors[.detail] ?? "")
                    ErrorLine(text: workspace.signalErrors[.original] ?? "")
                } else {
                    Text("Выберите событие для просмотра исходника и причин срабатывания.")
                        .foregroundStyle(.secondary)
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(8)
        }
    }
}

private struct NativeGuardSignalEvidence: View {
    let summary: NativeGuardSignal
    let detail: NativeGuardSignal?

    private var signal: NativeGuardSignal { detail ?? summary }
    private var value: MonitoringValue { signal.value }
    private var analysis: MonitoringValue { value["analysis"] }
    private var recognition: MonitoringValue { detail?.value["recognition"] ?? .null }
    private var confirmation: String {
        switch value["disposition"].text {
        case "authorized": "Разрешённый сигнал"
        case "false_positive": "Ложное срабатывание"
        case "investigate": "Требуется исследование"
        default: "Не рассмотрено"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(signal.detectorKind) · \(signal.detectorState)").font(.headline)
            if recognition != .null { Text(signal.recognition).foregroundStyle(.secondary) }
            Text("\(NativeGuardSignal.frequency(value["start_frequency_hz"]))–\(NativeGuardSignal.frequency(value["end_frequency_hz"])) МГц")
                .monospacedDigit()
            Text("Первое наблюдение \(value["first_seen"].scalar) · последнее \(value["last_seen"].scalar)").monospacedDigit()
            Divider()
            evidenceDetails
            if recognition != .null { recognitionDetails }
        }.font(.callout).textSelection(.enabled)
    }

    private var evidenceDetails: some View {
        Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 5) {
            GridRow {
                Text("Исходные свипы tinySA").foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text("Связано \(value["source_count"].scalar) · пик \(NativeGuardSignal.level(value["peak_level"], unit: "дБм"))")
                    if let detail, let source = detail.source {
                        let label = if detail.receiverSource != nil { "Свип сопоставления с AR-DV1" }
                                    else if detail.peakSource != nil { "Свип пика события" }
                                    else { "Последний свип события" }
                        Text("\(label) \(source.sweepID.rawValue)")
                    }
                }
            }
            GridRow {
                Text("Эталон").foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text(value["baseline_id"].scalar)
                    if let p95 = analysis["verification_evidence"]["analyzer"]["baseline_p95_dbm"].double {
                        Text("P95 в точке проверки \(NativeGuardSignal.level(.number(p95), unit: "дБм"))")
                    }
                }
            }
            GridRow {
                Text("Порог детектора").foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text("Превышение над адаптированным фоном \(NativeGuardSignal.level(value["peak_excess_db"], unit: "дБ"))")
                    let parameters = value["detector_parameters"]
                    if parameters != .null {
                        Text("Запас \(NativeGuardSignal.level(parameters["margin_db"], unit: "дБ")) · импульсный порог \(NativeGuardSignal.level(parameters["impulse_db"], unit: "дБ")) · подтверждения \(parameters["confirmations"].scalar) из \(parameters["window_sweeps"].scalar)")
                    }
                }
            }
            GridRow {
                Text("AR-DV1").foregroundStyle(.secondary)
                VStack(alignment: .leading) {
                    Text(signal.receiver)
                    if let hz = analysis["target_frequency_hz"].integer {
                        Text("Частота настройки \(NativeAnalyzerDraft.mhz(hz)) МГц")
                    }
                    if let reason = signal.receiverReason { Text(reason).foregroundStyle(.secondary) }
                    let verified = analysis["verification_evidence"]["receiver"]
                    if verified != .null {
                        Text("Связанное наблюдение \(verified["observation_id"].scalar) · \(verified["level"].scalar) \(verified["level_unit"].text) · шумодав открыт \(verified["squelch_open"].scalar)")
                    } else if let reading = analysis["receiver_status"].items.last {
                        Text("Последнее наблюдение \(reading["level"].scalar) \(reading["level_unit"].text) · шумодав открыт \(reading["squelch_open"].scalar)")
                    }
                }
            }
            GridRow {
                Text("Решение оператора").foregroundStyle(.secondary)
                Text(confirmation)
            }
        }
    }

    private var recognitionDetails: some View {
        VStack(alignment: .leading, spacing: 5) {
            let observed = recognition["observed_features"]
            if let width = observed["detected_width_hz"].integer { Text("Ширина срабатывания \(width) Гц") }
            if !observed["confirmed_receiver_mode"].text.isEmpty {
                Text("Режим по декодеру \(observed["confirmed_receiver_mode"].text)")
            }
            let bands = recognition["band_matches"].items
            if !bands.isEmpty {
                Text("Назначение полосы по частотному плану").font(.subheadline.weight(.semibold))
                ForEach(Array(bands.enumerated()), id: \.offset) { _, band in
                    NativeBandPlanBandRow(band: band)
                }
            }
            let matches = recognition["known_matches"].items
            if !matches.isEmpty {
                Text("Совпадения с каталогом").font(.subheadline.weight(.semibold))
                ForEach(Array(matches.enumerated()), id: \.offset) { _, match in
                    let kind = MonitoringKnownSignalKind(rawValue: match["kind"].text)?.title ?? "Не определён"
                    Text("\(match["name"].text) · \(kind) · \(match["reason"].text)")
                }
            }
            let hypotheses = recognition["hypotheses"].items
            if !hypotheses.isEmpty {
                Text("Гипотезы ядра").font(.subheadline.weight(.semibold))
                ForEach(Array(hypotheses.enumerated()), id: \.offset) { _, hypothesis in
                    Text("\(hypothesis["label"].text) · \(hypothesis["reason"].text)")
                }
            }
        }
    }

}

struct NativeGuardOriginalPane: View {
    let sweep: PlatformSweep
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Исходный свип tinySA").font(.headline)
                Spacer()
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction).accessibilityIdentifier("source-sheet.close")
            }
            Text(sweep.id).textSelection(.enabled)
                .accessibilityLabel("Идентификатор исходного свипа")
                .accessibilityValue(sweep.id)
                .accessibilityIdentifier("source-sheet.id")
            Text("Сеанс \(sweep.sessionId) · поток \(sweep.streamId)").textSelection(.enabled)
            Text("\(sweep.interval.startedAt) — \(sweep.interval.endedAt) · \(sweep.levels.count) точек")
                .font(.caption.monospacedDigit()).textSelection(.enabled)
            Text("Качество свипа · " + NativeBandCoverage.quality(of: sweep)).font(.caption)
            MonitoringCurveChart(curves: [.init(title: sweep.levelUnit, color: .blue,
                points: zip(sweep.frequenciesHz, sweep.levels).map { PlotPoint(x: Double($0), y: $1) })])
        }.padding().frame(minWidth: 640)
    }
}

struct NativeKnownSignalsPane: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel

    private struct Row: Identifiable {
        let value: MonitoringValue
        var id: PlatformExactID { value.exactID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Сигналы, подтверждённые оператором").font(.headline)
                Spacer()
                if workspace.signalPending.contains(.catalog) { ProgressView().controlSize(.small) }
                Button("Назад") {
                    let page = workspace.knownSignalsPage - 1
                    workspace.perform { await workspace.loadKnownSignals(page: page) }
                }.disabled(workspace.knownSignalsPage == 0 || workspace.signalPending.contains(.catalog))
                Text("Страница \(workspace.knownSignalsPage + 1)").monospacedDigit()
                Button("Далее") {
                    let page = workspace.knownSignalsPage + 1
                    workspace.perform { await workspace.loadKnownSignals(page: page) }
                }.disabled(!workspace.knownSignalsHasNext || workspace.signalPending.contains(.catalog))
                Button("Обновить") {
                    let page = workspace.knownSignalsPage
                    workspace.perform { await workspace.loadKnownSignals(page: page) }
                }.disabled(workspace.selected.isEmpty || workspace.signalPending.contains(.catalog))
            }
            ErrorLine(text: workspace.signalErrors[.catalog] ?? "")
            Table(workspace.knownSignals.map(Row.init)) {
                TableColumn("Название") { Text($0.value["name"].text) }
                TableColumn("Тип") { Text(MonitoringKnownSignalKind(rawValue: $0.value["kind"].text)?.title ?? "Не определён") }
                TableColumn("Частота, МГц") { Text(NativeGuardSignal.frequency($0.value["center_frequency_hz"])).monospacedDigit() }
                TableColumn("Первое наблюдение UTC") { Text($0.value["first_seen"].scalar).monospacedDigit() }
                TableColumn("Последнее наблюдение UTC") { Text($0.value["last_seen"].scalar).monospacedDigit() }
            }.overlay {
                if workspace.knownSignals.isEmpty && !workspace.signalPending.contains(.catalog) && workspace.signalErrors[.catalog] == nil {
                    Text(workspace.selected.isEmpty ? "Выберите проект охраны" : "В каталоге пока нет сигналов")
                        .foregroundStyle(.secondary).allowsHitTesting(false)
                }
            }
        }
        .padding(10)
        .task(id: workspace.signalContext) { await workspace.loadKnownSignals(page: workspace.knownSignalsPage) }
    }
}

private struct NativeKnownSignalEditor: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let eventID: PlatformExactID
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var kind = MonitoringKnownSignalKind.unknown
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Подтвердить известный сигнал").font(.headline)
            TextField("Название", text: $name).textFieldStyle(.roundedBorder).disabled(saving)
            Picker("Тип", selection: $kind) {
                ForEach(MonitoringKnownSignalKind.allCases, id: \.self) { Text($0.title).tag($0) }
            }.disabled(saving)
            ErrorLine(text: workspace.signalErrors[.confirm] ?? "")
            HStack {
                Spacer()
                if saving { ProgressView().controlSize(.small) }
                Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                Button("Сохранить") {
                    let enteredName = name, selectedKind = kind
                    saving = true
                    workspace.perform {
                        let saved = await workspace.saveKnownSignal(eventID: eventID, name: enteredName, kind: selectedKind)
                        saving = false
                        if saved { dismiss() }
                    }
                }.keyboardShortcut(.defaultAction)
                    .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding().frame(width: 420)
    }
}

private struct NativePairedRechecksPane: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let eventID: PlatformExactID
    @State private var importing = false
    @State private var importContext: Int?
    @State private var importError = ""

    var body: some View {
        GroupBox("Повторная проверка") {
            VStack(alignment: .leading, spacing: 6) {
                if !workspace.pairedRechecksAvailable {
                    Text("Ядро не сообщает о поддержке парных проверок.")
                } else {
                    if workspace.signalPending.contains(.rechecks) {
                        ProgressView("Проверяем исходники…").controlSize(.small)
                    } else if let result = workspace.pairedRechecks {
                        if result.state == "missing_evidence" {
                            Text("Парные наблюдения ещё не сохранены.")
                        } else {
                            Text((result.instrument ?? "") + " · " +
                                 NativeGuardSignal.frequency(result.frequencyHz.map(MonitoringValue.integer) ?? .null) + " МГц")
                            if result.simulated == true { Text("Учебные измерения").foregroundStyle(.secondary) }
                            Text("Повтор сигнала · \(result.signalRepeats ?? 0) из \(result.repeatTotal ?? 0)")
                                .accessibilityIdentifier("research.signal-repeats")
                            Text("Повтор расхождения · \(result.discrepancyRepeats ?? 0) из \(result.repeatTotal ?? 0)")
                                .accessibilityIdentifier("research.discrepancy-repeats")
                            if let background = result.localBackground {
                                Text("Местный фон tinySA · " + NativeGuardSignal.level(.number(background.tinyDbm), unit: "dBm"))
                                Text("Местный фон AR-DV1 · " + background.arSMeterCount.formatted() + " отсч. S-метра")
                            }
                            ForEach(Array(result.visits.enumerated()), id: \.offset) { offset, visit in
                                DisclosureGroup("Наблюдение \(offset + 1)") {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text("Сигнал повторился · " + verdict(visit.signalRepeated))
                                        Text("Расхождение повторилось · " + verdict(visit.discrepancyRepeated))
                                        if let delta = visit.tinyDeltaDb {
                                            Text("tinySA · изменение к фону " + NativeGuardSignal.level(.number(delta), unit: "dB"))
                                        }
                                        if let counts = visit.arDropCounts {
                                            Text("AR-DV1 · фон минус измерение \(counts.formatted()) отсч. S-метра")
                                        }
                                        Text("Свип tinySA · " + visit.sweepId)
                                        Text("Наблюдения AR-DV1 · " + visit.observationIds.joined(separator: ", "))
                                    }.textSelection(.enabled)
                                }
                            }
                        }
                    }
                    HStack {
                        Button("Обновить") { workspace.perform { await workspace.loadPairedRechecks(eventID: eventID) } }
                        Button("Исходники из JSON…") { importContext = workspace.signalContext; importing = true }
                    }.disabled(workspace.signalPending.contains(.rechecks))
                    ErrorLine(text: workspace.signalErrors[.rechecks] ?? "")
                    ErrorLine(text: importError)
                }
            }.frame(maxWidth: .infinity, alignment: .leading).font(.callout).monospacedDigit()
        }
        .accessibilityIdentifier("research.rechecks")
        .task(id: workspace.pairedRechecksAvailable) {
            if workspace.pairedRechecksAvailable { await workspace.loadPairedRechecks(eventID: eventID) }
        }
        .fileImporter(isPresented: $importing, allowedContentTypes: [.json]) { selection in
            do {
                guard let context = importContext, context == workspace.signalContext,
                      workspace.signalQueue.selectedID == eventID else { return }
                let url = try selection.get()
                let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                let file = try FileHandle(forReadingFrom: url)
                defer { try? file.close() }
                let data = try file.read(upToCount: 65_537) ?? Data()
                guard data.count <= 65_536 else { throw PlatformError.invalidCommand("Файл списка исходников превышает 64 КиБ") }
                let recipe = try JSONDecoder().decode(MonitoringValue.self, from: data)
                guard case .object(let fields) = recipe,
                      Set(fields.keys) == ["initial", "neighbors", "controls", "repeats"] else {
                    throw PlatformError.invalidCommand("Нужен список initial, neighbors, controls и repeats с идентификаторами исходников")
                }
                importError = ""
                workspace.perform {
                    guard context == workspace.signalContext else { return }
                    await workspace.loadPairedRechecks(eventID: eventID, recipe: recipe)
                }
            } catch CocoaError.userCancelled { }
              catch { importError = error.localizedDescription }
        }
    }

    private func verdict(_ value: Bool?) -> String {
        value.map { $0 ? "да" : "нет" } ?? "нет результата"
    }
}
