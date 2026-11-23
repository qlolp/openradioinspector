import SwiftUI
import SpektrKomandy

struct NativeRFExplorerWorkbench<Current: View>: View {
    let client: PlatformClient
    let session: String
    let stream: String
    let survey: Bool
    let currentWindow: PlatformSweep?
    let live: Bool
    let paused: Bool
    let setPaused: (Bool) -> Void
    let memory: NativeSpectrumViewMemory
    @ViewBuilder let current: (((PlatformSweep, Int?) -> Void)?, ((PlatformSweep) -> Void)?) -> Current

    @State private var wholePass = false
    @State private var pauseWithoutWindow = false
    @State private var features: Set<String>?
    @State private var operatorActions = false
    @State private var capabilityMessage = ""
    @State private var capabilityAttempt = 0
    @State private var browser = NativeRFExplorerPassBrowser()
    @State private var selectedPass: PlatformExactID?
    @State private var cursors: [String] = []
    @State private var showHold = false
    @State private var holdSince = "1970-01-01T00:00:00Z"
    @State private var frequency: Int64?
    @State private var namedSelection = false
    @State private var markerReading: MarkerReading?
    @State private var markers: [PlatformFrequencyBookmarkEntry] = []
    @State private var markerEditor = false
    @State private var cutoffEditor = false
    @State private var cutoffInput = ""
    @State private var raw: NativeRFExplorerRawRequest?
    @State private var analysisTarget: PlatformSweep?
    @State private var analysisReference: PlatformSweep?
    @State private var refreshNumber = 0

    private var scope: [PlatformExactID] { [client.baseURL.root.absoluteString, client.baseURL.runtimeInstanceID ?? "", session, stream].map { PlatformExactID($0) } }
    private var supported: Bool { features?.contains(PlatformRFExplorerConfiguration.panoramaFeature) == true }
    private var query: NativeRFExplorerPassQuery {
        .init(before: cursors.last, pass: selectedPass, holdSince: showHold ? holdSince : nil)
    }
    private struct Refresh: Hashable {
        let query: NativeRFExplorerPassQuery
        let visible, live, paused, reviewing: Bool
        let number: Int
    }
    private var displayPaused: Bool { paused || pauseWithoutWindow }
    private var refresh: Refresh {
        Refresh(query: query, visible: wholePass && supported, live: live, paused: displayPaused,
                reviewing: raw != nil || markerEditor, number: refreshNumber)
    }
    private var value: PlatformRFExplorerPanorama? { browser.query == query ? browser.panorama : nil }
    private struct MarkerQuery: Equatable {
        let pass: PlatformExactID
        let hz: Int64
        let holdSince: String?
        let endedAt: String
        let receivedTiles: Int
    }
    private struct MarkerReading {
        let key: MarkerQuery
        let result: PlatformRFExplorerMarker?
        let message: String?
    }
    private var markerQuery: MarkerQuery? {
        guard wholePass, namedSelection, let frequency, let summary = value?.summary,
              (summary.startHz...summary.stopHz).contains(frequency), (frequency - summary.startHz).isMultiple(of: summary.stepHz) else { return nil }
        return .init(pass: summary.id, hz: frequency, holdSince: query.holdSince,
                     endedAt: summary.endedAt, receivedTiles: summary.receivedTiles)
    }
    private func selectFrequency(_ hz: Int64?, named: Bool = false) {
        frequency = hz
        namedSelection = named
    }
    private var missingMarkerText: String {
        guard let key = markerQuery else { return "Частота маркера вне измеряемой сетки этого прохода." }
        guard let reading = markerReading, reading.key == key else { return "Чтение точной частоты маркера…" }
        return reading.message ?? "На точной частоте маркера нет сохранённого отсчёта для выбранной трассы."
    }
    private func peak(in pass: PlatformRFExplorerPanorama) -> PlatformRFExplorerPoint? {
        if namedSelection {
            guard let key = markerQuery, let reading = markerReading, reading.key == key else { return nil }
            return showHold ? reading.result?.held : reading.result?.current
        }
        if let frequency { return pass.point(at: frequency, held: showHold) }
        return (showHold ? pass.holdColumns ?? [] : pass.columns).compactMap { $0 }.max { $0.levelDbm < $1.levelDbm }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                if survey {
                    Picker("Вид спектра", selection: $wholePass) {
                        Text("Текущее окно").tag(false)
                        Text("Весь проход").tag(true)
                    }.pickerStyle(.segmented).frame(maxWidth: 320).disabled(!supported)
                        .accessibilityIdentifier("rf-explorer.spectrum-view")
                }
                if survey, !supported, let features, !features.contains(PlatformRFExplorerConfiguration.panoramaFeature) {
                    Text("Это ядро поддерживает только показ исходных окон.").font(.caption).foregroundStyle(.secondary)
                }
            }
            if features != nil, !operatorActions {
                Text("Доступ для чтения · расчёты и сохранение наборов доступны оператору.").font(.caption).foregroundStyle(.secondary)
            }
            if !capabilityMessage.isEmpty {
                HStack { Text(capabilityMessage); Button("Повторить") { capabilityAttempt += 1 } }.font(.caption)
            }
            if wholePass, supported {
                passView
            } else {
                current(survey && supported ? { source, index in
                    raw = .init(sweepID: source.id, loaded: source, sourceIndex: index)
                } : nil, operatorActions && NativeRFExplorerSweepAnalysis.offered(features ?? []) ? { source in analysisTarget = source } : nil)
            }
        }
        .onAppear {
            if let reset = memory.surveyHoldSince[scope] { holdSince = reset }
        }
        .task(id: capabilityAttempt) {
            do {
                let access = try await client.rfExplorerWorkbenchAccess()
                try Task.checkCancellation()
                features = access.features; operatorActions = access.operatorActions; capabilityMessage = ""
            } catch { if !Task.isCancelled { capabilityMessage = error.localizedDescription } }
        }
        .task(id: markerQuery) {
            guard let key = markerQuery else { return }
            do {
                let point = try await client.rfExplorerMarker(sessionID: session, streamID: stream, passID: key.pass.rawValue,
                    frequencyHz: key.hz, holdSince: key.holdSince)
                try Task.checkCancellation()
                markerReading = .init(key: key, result: point, message: nil)
            } catch {
                if !Task.isCancelled { markerReading = .init(key: key, result: nil, message: error.localizedDescription) }
            }
        }
        .task(id: refresh) {
            let request = refresh
            guard request.visible, !request.reviewing else { return }
            if request.paused, browser.panorama != nil, browser.query == request.query { return }
            repeat {
                await browser.load(request.query, list: { before in
                    try await client.rfExplorerPasses(sessionID: session, streamID: stream, before: before)
                }, detail: { id, columns, since in
                    try await client.rfExplorerPass(sessionID: session, streamID: stream, passID: id, columns: columns, holdSince: since)
                })
                guard !Task.isCancelled, request.live, !request.paused, request.query.followsLatest else { break }
                do { try await Task.sleep(for: .seconds(browser.message.isEmpty ? 3 : 10)) } catch { break }
            } while !Task.isCancelled
        }
        .sheet(item: $raw) { request in
            NativeRFExplorerRawPane(client: client, session: session, stream: stream, request: request,
                features: operatorActions ? features ?? [] : [], analysisReference: $analysisReference)
        }
        .sheet(item: $analysisTarget) { source in
            NativeRFExplorerSweepAnalysis(client: client, target: source, features: features ?? [], reference: $analysisReference)
        }
    }

    private var passView: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Menu {
                    Button("Следить за последним проходом") { cursors = []; selectedPass = nil; selectFrequency(nil) }
                    if let page = browser.page {
                        ForEach(page.items) { item in
                            Button("Проход \(item.pass) · \(item.startedAt)") { selectedPass = PlatformExactID(item.passId); selectFrequency(nil) }
                        }
                        Divider()
                        Button("Раньше") {
                            if let cursor = page.nextBefore { cursors.append(cursor); selectedPass = nil; selectFrequency(nil) }
                        }.disabled(page.nextBefore == nil)
                        Button("Позже") {
                            if !cursors.isEmpty { cursors.removeLast(); selectedPass = nil; selectFrequency(nil) }
                        }.disabled(cursors.isEmpty)
                    }
                } label: { Text(query.followsLatest ? "Последний проход" : "История проходов") }
                Toggle("Программный Max Hold", isOn: $showHold)
                Menu("Сбросить Hold") {
                    Button("С текущего момента") { useCutoff(Self.cutoff(Date())) }
                    Button("От начала выбранного прохода") {
                        if let summary = browser.selectedSummary { useCutoff(summary.startedAt) }
                    }.disabled(browser.selectedSummary == nil)
                    Button("Указать время UTC…") {
                        cutoffInput = browser.selectedSummary?.startedAt ?? holdSince
                        cutoffEditor = true
                    }
                }.disabled(!showHold)
                    .popover(isPresented: $cutoffEditor) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Начало программного Max Hold, UTC").font(.headline)
                            TextField("2026-11-22T00:00:00Z", text: $cutoffInput).textFieldStyle(.roundedBorder)
                            Button("Применить") {
                                if let instant = try? PlatformTimestamp(cutoffInput) { useCutoff(instant.canonicalUTC); cutoffEditor = false }
                            }.disabled((try? PlatformTimestamp(cutoffInput)) == nil)
                        }.padding(12).frame(width: 320)
                    }
                Spacer()
                Button(displayPaused ? "Продолжить показ" : "Пауза показа") {
                    let next = !displayPaused
                    pauseWithoutWindow = next && currentWindow == nil
                    setPaused(next)
                }
                Button("Обновить") { refreshNumber += 1 }.disabled(browser.busy || displayPaused)
            }.controlSize(.small).font(.caption)
            if let value {
                let summary = value.summary
                let selected = peak(in: value)
                let currentPass = NativeRFExplorerSurveyProgress(currentWindow?.settings?["survey"])
                let readback = PlatformExactID.same(currentPass?.passID, summary.passId) ? NativeRFExplorerReadback(currentWindow?.settings) : nil
                NativeRFExplorerPassStatus(summary: summary, gaps: value.gaps, readback: readback)
                NativeRFExplorerPanoramaPlot(panorama: value, showHold: showHold, selected: selected, markers: markers,
                    select: { selectFrequency($0) })
                    .frame(minHeight: 90)
                HStack {
                    Text("Проход").foregroundStyle(.cyan)
                    if showHold { Text("Max Hold клиента").foregroundStyle(.orange) }
                    Text("· серым показаны пропуски").foregroundStyle(.secondary)
                    Spacer()
                    if let frequency { Text("Маркер \(frequency) Гц") }
                    Button("Сильнейший пик") { selectFrequency(nil) }
                    Button("Маркеры (\(markers.count))") { markerEditor = true }
                        .popover(isPresented: $markerEditor) {
                            NativeRFExplorerMarkers(client: client, session: session, selectedHz: frequency ?? selected?.frequencyHz,
                                shared: features?.contains("frequency_bookmarks") == true, operatorActions: operatorActions,
                                markers: $markers, jump: { selectFrequency($0, named: true) })
                        }
                }.controlSize(.small).font(.caption)
                if let selected {
                    let sourceLabel = namedSelection ? (showHold ? "Max Hold маркера" : "Отсчёт маркера") : (showHold ? "Пик Max Hold" : "Пик прохода")
                    HStack {
                        Text("\(sourceLabel) · \(selected.frequencyHz) Гц · \(selected.levelDbm.formatted()) дБм")
                            .fontWeight(.semibold)
                        Spacer()
                        Button("Открыть исходный свип") { raw = .init(sweepID: selected.sweepId, point: selected) }
                            .accessibilityIdentifier("rf-explorer.open-raw")
                    }
                    Text("Свип \(selected.sweepId) · индекс \(selected.pointIndex) · \(selected.startedAt) — \(selected.endedAt) UTC")
                        .textSelection(.enabled)
                    NativeRFExplorerAge(utc: selected.lastMeasuredAt, title: "Последнее измерение частоты")
                } else {
                    Text(namedSelection ? missingMarkerText : (showHold ? "В выбранном участке нет отображённого пика Max Hold." : "В выбранном участке нет отображённого пика."))
                        .foregroundStyle(.secondary)
                }
                if showHold {
                    Text("Удержание совместимых проходов с \(holdSince) UTC").foregroundStyle(.secondary)
                }
            } else if browser.busy {
                ProgressView("Чтение сохранённого прохода…")
            }
            if !browser.message.isEmpty { Text(browser.message).foregroundStyle(.orange) }
        }
        .font(.caption).controlSize(.small)
        .padding(10)
        .background(Color(red: 0.055, green: 0.075, blue: 0.10), in: RoundedRectangle(cornerRadius: 10))
        .environment(\.colorScheme, .dark)
    }

    private func useCutoff(_ value: String) {
        holdSince = value
        memory.surveyHoldSince[scope] = value
        if !namedSelection { selectFrequency(nil) }
    }

    private static func cutoff(_ date: Date) -> String {
        let timestamp = date.timeIntervalSince1970, seconds = floor(timestamp)
        let utc = Date(timeIntervalSince1970: seconds).ISO8601Format()
        return utc.dropLast() + String(format: ".%06dZ", Int((timestamp - seconds) * 1_000_000))
    }
}

private struct NativeRFExplorerPassStatus: View {
    let summary: PlatformRFExplorerPassSummary
    let gaps: [ClosedRange<Int64>]
    let readback: NativeRFExplorerReadback?

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("Проход \(summary.pass)").font(.headline)
                Text(summary.complete ? "Завершён" : "Частичное покрытие").foregroundStyle(summary.complete ? Color.green : .orange)
                Text("\(summary.receivedTiles) из \(summary.tiles) окон · \(summary.uniquePoints) из \(summary.expectedPoints) частот")
                if summary.simulated { Text("Симуляция").foregroundStyle(.yellow) }
            }
            ProgressView(value: Double(summary.uniquePoints), total: Double(summary.expectedPoints))
                .accessibilityLabel("Доля измеренных частот")
            HStack {
                Text("\(summary.startHz)–\(summary.stopHz) Гц · шаг \(summary.stepHz) Гц")
                Text("· \(summary.activeBoard) / SMA \(summary.activeInput) · RBW \(summary.rbwHz) Гц · детектор прибора \(summary.detector)")
            }
            if let readback { Text(readback.label) }
            else { Text("Пределы модуля · см. данные чтения в исходном свипе.").foregroundStyle(.secondary) }
            Text("Длительность прохода \(time(summary.durationS)) · измеренный интервал повторного посещения \(time(summary.revisitS))")
            NativeRFExplorerAge(utc: summary.endedAt, title: "Последнее окно")
            if !gaps.isEmpty {
                DisclosureGroup("Пропуски (\(gaps.count))") {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(gaps, id: \.lowerBound) { gap in Text("\(gap.lowerBound)–\(gap.upperBound) Гц") }
                        }
                    }.frame(maxHeight: 65)
                }
            }
        }.font(.caption)
    }
    private func time(_ seconds: Double?) -> String {
        seconds.map { $0.formatted(.number.precision(.fractionLength(0...3))) + " с" } ?? "нет данных"
    }
}

private struct NativeRFExplorerAge: View {
    let utc: String
    let title: String
    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            let date = NativeSpectrumTimeline.date(utc)
            let elapsed = date.map { context.date.timeIntervalSince($0) }
            let age = elapsed.map {
                $0 < 0 ? "метка опережает часы клиента" : "\($0.formatted(.number.precision(.fractionLength(0)))) с назад"
            } ?? "возраст неизвестен"
            Text("\(title) · \(utc) UTC · \(age)").font(.caption2).foregroundStyle(.secondary)
        }
    }
}
