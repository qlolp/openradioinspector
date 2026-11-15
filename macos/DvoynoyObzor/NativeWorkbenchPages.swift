import SwiftUI
import SpektrKomandy

struct NativeSignalsPage: View {
    @ObservedObject var model: RemoteDualModel
    let openSpectrum: () -> Void
    @State private var message = ""
    @State private var sourceTask: Task<Void, Never>?
    @State private var sourceBusy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Обнаруженные сигналы").font(.headline)
                Spacer()
                Button("Добавить текущий пик") { model.createCandidateFromLatestPeak() }
                    .disabled(!model.connected || model.spectrumPresentation.sweeps.isEmpty || model.busy)
                Button("Следить за уровнем") {
                    if let candidate = model.selectedCandidate { model.followFrequency(candidate.centerFrequencyHz) }
                }.disabled(model.selectedCandidate == nil || model.selectedCandidate?.sourceStreamId != model.analyzer?.id)
                Button("Исходный свип") { openSource() }
                    .disabled(model.selectedCandidate == nil || sourceBusy)
                Button("В очередь AR-DV1") { model.enqueueSelectedCandidate() }
                    .disabled(model.selectedCandidate == nil || model.receiver == nil || model.busy)
            }
            Table(model.candidates, selection: Binding<String?>(get: { model.selectedCandidateID.isEmpty ? nil : model.selectedCandidateID },
                set: { model.selectedCandidateID = $0 ?? "" })) {
                TableColumn("Частота, МГц") { Text(NativeAnalyzerDraft.mhz($0.centerFrequencyHz)).monospacedDigit() }
                TableColumn("Уровень") { row in Text(row.peakLevel.map { String(format: "%.1f %@", $0, row.levelUnit) } ?? "—") }
                TableColumn("Начало UTC") { Text($0.interval.startedAt) }
                TableColumn("Окончание UTC") { Text($0.interval.endedAt) }
                TableColumn("Исходников") { Text(String($0.observationIds.count)) }
                TableColumn("Статус", value: \.state)
            }.frame(minHeight: 180)
            Text("Число исходников — ссылки ядра, не оценка количества передач. Уровни сохранены в исходных единицах.")
                .font(.caption).foregroundStyle(.secondary)
            if !message.isEmpty { Text(message).font(.caption).textSelection(.enabled) }
            DisclosureGroup("Очередь, автоматическое обнаружение и действия") {
                RemoteWorkflowPane(model: model).frame(height: 280)
            }
            DisclosureGroup("База частотных присвоений") {
                ScrollView { RemoteSignalListPane(model: model.signalList) }.frame(height: 240)
            }
            ErrorLine(text: model.errorText)
        }.padding(10)
        .onChange(of: model.selectedSessionID) { _, _ in cancelSource() }
        .onChange(of: model.serverURL) { _, _ in cancelSource() }
        .onDisappear { cancelSource() }
    }
    private func cancelSource() { sourceTask?.cancel(); sourceBusy = false; message = "" }
    private func openSource() {
        guard let candidate = model.selectedCandidate, let id = candidate.observationIds.first, let client = model.client else {
            message = "У сигнала нет ссылки на исходное измерение"; return
        }
        let session = model.selectedSessionID
        sourceBusy = true
        sourceTask = Task { @MainActor in
            defer { sourceBusy = false }
            do {
                let sweep = try await client.archiveOriginalSweep(sessionID: session, streamID: candidate.sourceStreamId, sweepID: id)
                try Task.checkCancellation()
                guard client === model.client, session == model.selectedSessionID,
                      candidate.sourceStreamId == model.analyzer?.id else { return }
                model.reviewedSweep = sweep; model.reviewedSweepRequest = UUID(); message = ""; openSpectrum()
            } catch is CancellationError { } catch { message = "Исходник недоступен: \(error.localizedDescription)" }
        }
    }
}

struct NativeArchivePage: View {
    private struct TableSource: Identifiable {
        let sweep: PlatformArchiveSweepRow
        var id: PlatformExactID { sweep.exactID }
    }

    @ObservedObject var model: RemoteDualModel
    let openSpectrum: () -> Void
    @State private var browser = NativeArchiveBrowser()
    @State private var start = Date().addingTimeInterval(-3600)
    @State private var end = Date()
    @State private var minimum = ""
    @State private var maximum = ""
    @State private var selection: PlatformExactID?
    @State private var message = ""
    @State private var request: Task<Void, Never>?
    @State private var opening = false

    private var scope: [PlatformExactID] { [model.serverURL, model.selectedSessionID, model.analyzer?.id ?? ""].map { PlatformExactID($0) } }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                DatePicker("С", selection: $start).environment(\.timeZone, TimeZone(secondsFromGMT: 0) ?? .gmt)
                DatePicker("По", selection: $end).environment(\.timeZone, TimeZone(secondsFromGMT: 0) ?? .gmt)
                Text("UTC").font(.caption)
                TextField("От, МГц", text: $minimum).frame(width: 90)
                TextField("До, МГц", text: $maximum).frame(width: 90)
                Button("Найти") { search() }.disabled(browser.busy || !model.connected || opening)
            }
            HStack {
                Text(browser.message).font(.caption)
                Spacer()
                Button("Следующая страница") { search(next: true) }
                    .disabled(browser.busy || browser.nextCursor == nil || opening)
                Button("Открыть спектр") { openSource() }.disabled(selection == nil || opening || browser.busy)
            }
            Table(browser.rows.map(TableSource.init(sweep:)), selection: $selection) {
                TableColumn("Начало UTC") { Text($0.sweep.interval.startedAt) }
                TableColumn("Диапазон, МГц") {
                    Text("\(NativeAnalyzerDraft.mhz($0.sweep.minFrequencyHz))–\(NativeAnalyzerDraft.mhz($0.sweep.maxFrequencyHz))")
                }
                TableColumn("Точек") { Text(String($0.sweep.pointCount)) }
                TableColumn("Пик") { Text(String(format: "%.1f %@", $0.sweep.peakLevel, $0.sweep.levelUnit)) }
            }.frame(minHeight: 180)
            if !message.isEmpty { Text(message).foregroundStyle(.orange).font(.caption) }
            DisclosureGroup("Хранение и экспорт") {
                ScrollView {
                    RemoteArchiveCapacityPane(model: model)
                    RemoteAnalyticsPane(model: model)
                }.frame(height: 220)
            }
            Text("Открытие архива не меняет приём. Исходные данные остаются в ядре; просмотр доступен и при отключённых приборах.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(10)
        .onChange(of: scope) { _, _ in reset() }
        .onChange(of: model.client.map(ObjectIdentifier.init)) { _, _ in reset() }
        .onChange(of: start) { _, _ in reset() }
        .onChange(of: end) { _, _ in reset() }
        .onChange(of: minimum) { _, _ in reset() }
        .onChange(of: maximum) { _, _ in reset() }
        .onDisappear { request?.cancel() }
    }
    private func reset() { request?.cancel(); browser.reset(); selection = nil; opening = false; message = "" }
    private func search(next: Bool = false) {
        guard let client = model.client, let stream = model.analyzer else { return }
        do {
            guard start < end else { throw NativeAnalyzerDraft.DraftError.invalid("Начало должно быть раньше окончания") }
            let low = minimum.isEmpty ? nil : try NativeAnalyzerDraft.hz(minimum)
            let high = maximum.isEmpty ? nil : try NativeAnalyzerDraft.hz(maximum)
            if let low, let high, low > high { throw NativeAnalyzerDraft.DraftError.invalid("Неверный диапазон поиска") }
            let session = model.selectedSessionID
            selection = nil; message = ""
            request = Task {
                await browser.search(client: client, session: session, stream: stream.id,
                    start: NativeTimeWindow.iso(start), end: NativeTimeWindow.iso(end), minimum: low, maximum: high, next: next)
            }
        } catch { message = error.localizedDescription }
    }
    private func openSource() {
        guard let client = model.client, let selection, let row = browser.row(id: selection) else { return }
        let savedScope = scope
        opening = true
        request = Task { @MainActor in
            defer { opening = false }
            do {
                let sweep = try await client.archiveOriginalSweep(sessionID: row.sessionId, streamID: row.streamId, sweepID: row.id)
                try Task.checkCancellation()
                guard savedScope == scope, client === model.client else { return }
                model.reviewedSweep = sweep; model.reviewedSweepRequest = UUID(); openSpectrum()
            } catch is CancellationError { } catch { message = "Исходник недоступен: \(error.localizedDescription)" }
        }
    }
}
