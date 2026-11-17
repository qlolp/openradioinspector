import SwiftUI
import AppKit
import Charts
import UniformTypeIdentifiers
import SpektrKomandy

private struct HistoryFrequency: Identifiable {
    let id = UUID()
    var label: String
    var mhz: String
    init(label: String = "Частота", hz: Int64 = 100_000_000) { self.label = label; mhz = NativeHistoryFrequencyInput.mhz(hz) }
}

struct NativeFrequencyHistoryPane: View {
    @Bindable var model: RemoteDualModel
    @Environment(\.dismiss) private var dismiss
    @State private var browser = NativeFrequencyHistoryBrowser()
    @State private var frequencies = [HistoryFrequency()]
    @State private var start = Date().addingTimeInterval(-3600)
    @State private var end = Date()
    @State private var features: Set<String> = []
    @State private var sets: [PlatformFrequencyBookmarkSet] = []
    @State private var name = "Набор частот"
    @State private var selection: PlatformExactID?
    @State private var message = ""
    @State private var request: Task<Void, Never>?
    @State private var busy = false
    @State private var importing = false
    @State private var live = false
    @State private var loadedQuery = ""
    @State private var compare = false
    private var scope: [PlatformExactID] { [model.serverURL, model.selectedSessionID, model.analyzer?.id ?? "", String(model.connected)].map { PlatformExactID($0) } }
    private var queryKey: String { frequencies.map(\.mhz).joined(separator:"|") + NativeTimeWindow.iso(start) + NativeTimeWindow.iso(end) }
    private var supported: Bool { features.contains("frequency_history") }
    private var bookmarks: Bool { features.contains("frequency_bookmarks") }

    var body: some View {
        VStack(alignment:.leading,spacing:10) {
            HStack {
                Text("История уровней").font(.title2)
                Spacer()
                Button("Закрыть") { dismiss() }
            }
            if !supported { Text("Этот узел не объявил поддержку истории уровней.").foregroundStyle(.secondary) }
            HStack {
                DatePicker("С",selection:$start).environment(\.timeZone,.gmt)
                DatePicker("По",selection:$end).environment(\.timeZone,.gmt)
                Text("UTC")
            }.disabled(busy || browser.busy || live)
            HStack(alignment:.top) {
                ForEach($frequencies) { $frequency in
                    VStack {
                        TextField("Подпись",text:$frequency.label)
                        TextField("МГц",text:$frequency.mhz).monospacedDigit()
                        if frequencies.count > 1 { Button("Убрать") { frequencies.removeAll { $0.id == frequency.id } } }
                    }
                }
                Button("＋") { frequencies.append(HistoryFrequency(hz:100_000_000 + Int64(frequencies.count) * 1_000_000)) }
                    .disabled(frequencies.count == 4)
            }.textFieldStyle(.roundedBorder).disabled(busy || browser.busy)
            HStack {
                Button("Показать") { search() }.disabled(!supported || busy || browser.busy)
                Button("Ещё 100 свипов") { search(next:true) }
                    .disabled(!supported || busy || browser.busy || browser.nextCursor == nil || queryKey != loadedQuery || live)
                Toggle("Обновлять каждые 5 с",isOn:$live).disabled(!supported)
                Spacer()
                Button("Открыть исходник") { openSource() }.disabled(selection == nil || busy || browser.busy)
                if features.contains("sweep_comparison") { Button("Эталоны и сравнение…") { compare = true } }
            }.controlSize(.small)
            Text("Каждая полоска — интервал исходного свипа, не непрерывная запись. Частота берётся из ближайшего реального бина; вне диапазона — пропуск.")
                .font(.caption).foregroundStyle(.secondary)
            historyChart.frame(height:170)
            Table(browser.rows, selection:$selection) {
                TableColumn("Начало UTC") { Text($0.interval.startedAt) }
                TableColumn("Окончание UTC") { Text($0.interval.endedAt) }
                TableColumn("Фактические бины и уровни") { row in
                    Text(row.samples.map { sample in
                        guard let hz = sample.frequencyHz, let level = sample.level else { return "—" }
                        return "\(NativeHistoryFrequencyInput.mhz(hz)) МГц: \(String(format:"%.2f",level)) \(row.levelUnit)"
                    }.joined(separator:" · ")).font(.caption)
                }
            }.frame(minHeight:100)
            DisclosureGroup("Сохраняемые наборы частот") {
                HStack {
                    TextField("Имя набора",text:$name).textFieldStyle(.roundedBorder)
                    Button("Сохранить версию") { save() }.disabled(!bookmarks || busy)
                    Menu("Загрузить") {
                        ForEach(sets) { item in
                            Button("\(item.name) · версия \(item.revision)") { apply(item.name, entries:item.entries) }
                        }
                    }.disabled(!bookmarks || sets.isEmpty || busy)
                    Button("Импорт JSON…") { importing = true }.disabled(busy)
                    Button("Экспорт JSON…") { export() }.disabled(busy)
                }
                Text("Набор содержит только подписи и частоты; при загрузке прибор не перестраивается.").font(.caption)
            }
            if busy || browser.busy { ProgressView().controlSize(.small) }
            Text(message.isEmpty ? browser.message : message).font(.caption).textSelection(.enabled)
        }.padding(16).frame(minWidth:860,idealWidth:1040,minHeight:680)
        .task(id:scope) { await prepare() }
        .task(id:live) {
            while live && !Task.isCancelled {
                do { try await Task.sleep(for:.seconds(5)) } catch { return }
                if supported && !busy && !browser.busy { search(refresh:true) }
            }
        }
        .onDisappear { request?.cancel(); browser.reset() }
        .fileImporter(isPresented:$importing,allowedContentTypes:[.json]) { result in
            do {
                let url = try result.get(); let access = url.startAccessingSecurityScopedResource()
                defer { if access { url.stopAccessingSecurityScopedResource() } }
                guard (try url.resourceValues(forKeys:[.fileSizeKey]).fileSize ?? Int.max) <= 8192 else { throw PlatformError.responseTooLarge }
                let document = try PlatformFrequencyBookmarkDocument.importData(Data(contentsOf:url))
                apply(document.name,entries:document.entries)
            } catch { message = error.localizedDescription }
        }
        .sheet(isPresented:$compare) { ScrollView { RemoteSweepComparisonPane(model:model).padding(16) }.frame(width:820,height:680) }
    }

    private var historyChart: some View {
        Chart {
            ForEach(browser.rows) { row in
                if let begin = NativeSpectrumTimeline.date(row.interval.startedAt), let finish = NativeSpectrumTimeline.date(row.interval.endedAt) {
                    ForEach(Array(row.samples.enumerated()),id:\.offset) { index, sample in
                        if let level = sample.level {
                            RuleMark(xStart:.value("Начало UTC",begin),xEnd:.value("Окончание UTC",finish),y:.value(row.levelUnit,level))
                                .foregroundStyle(by:.value("Частота", "\(sample.requestedHz) Гц · \(row.levelUnit)"))
                                .accessibilityLabel("\(sample.requestedHz) Гц · \(row.interval.startedAt) — \(row.interval.endedAt) UTC")
                                .accessibilityValue("\(level) \(row.levelUnit)")
                        }
                    }
                }
            }
        }.chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 5)) { tick in
                AxisGridLine(); AxisTick()
                AxisValueLabel {
                    if let date = tick.as(Date.self) { Text(String(NativeTimeWindow.iso(date).dropFirst(11).prefix(8))) }
                }
            }
        }.chartXAxisLabel("UTC · интервалы свипов").environment(\.timeZone,.gmt)
    }
    private func entries() throws -> [PlatformFrequencyBookmarkEntry] {
        try frequencies.map { .init(label:$0.label.trimmingCharacters(in:.whitespacesAndNewlines),frequencyHz:try NativeHistoryFrequencyInput.hz($0.mhz)) }
    }
    private func apply(_ title: String, entries: [PlatformFrequencyBookmarkEntry]) {
        request?.cancel(); browser.reset(); busy = false; live = false; loadedQuery = ""; selection = nil
        name = title; frequencies = entries.map { HistoryFrequency(label:$0.label,hz:$0.frequencyHz) }; message = "Набор загружен. Нажмите «Показать»."
    }
    private func current(_ client: PlatformClient, _ session: String, _ stream: String) -> Bool {
        !Task.isCancelled && model.client === client && PlatformExactID.same(model.selectedSessionID,session)
            && PlatformExactID.same(model.analyzer?.id,stream) && model.connected
    }
    private func prepare() async {
        request?.cancel(); browser.reset(); features = []; sets = []; busy = false; selection = nil; loadedQuery = ""; message = ""; live = false
        guard model.connected, let client = model.client, let stream = model.analyzer?.id else { return }
        let session = model.selectedSessionID
        if let hz = model.historyFrequency { frequencies = [HistoryFrequency(hz:hz)] }
        do {
            let capabilities = try await client.workbenchFeatures()
            guard current(client,session,stream) else { return }
            features = capabilities
            if bookmarks {
                let page = try await client.frequencyBookmarks(sessionID:session)
                guard current(client,session,stream) else { return }; sets = page.items
            }
        } catch { if current(client,session,stream) { message = error.localizedDescription } }
    }
    private func search(next: Bool = false, refresh: Bool = false) {
        guard supported, let client = model.client, let stream = model.analyzer?.id, !busy, !browser.busy else { return }
        do {
            let values = try entries().map(\.frequencyHz)
            if refresh { let duration = max(1,end.timeIntervalSince(start)); end = Date(); start = end.addingTimeInterval(-duration) }
            let from = NativeTimeWindow.iso(start), to = NativeTimeWindow.iso(end), session = model.selectedSessionID, key = queryKey
            if next && key != loadedQuery { return }
            request?.cancel(); message = ""; selection = nil
            request = Task { @MainActor in
                await browser.load(next:next) { cursor in
                    try await client.frequencyHistory(sessionID:session,streamID:stream,frequencies:values,start:from,end:to,after:cursor)
                }
                if current(client,session,stream) { loadedQuery = key }
            }
        } catch { message = error.localizedDescription }
    }
    private func save() {
        guard bookmarks, let client = model.client, let stream = model.analyzer?.id else { return }
        do {
            let document = PlatformFrequencyBookmarkDocument(name:name.trimmingCharacters(in:.whitespacesAndNewlines),entries:try entries()); try document.validate()
            let session = model.selectedSessionID; busy = true
            request = Task { @MainActor in
                defer { if current(client,session,stream) { busy = false } }
                do {
                    let item = try await client.saveFrequencyBookmarks(sessionID:session,document:document)
                    guard current(client,session,stream) else { return }
                    sets.removeAll { PlatformExactID.same($0.id,item.id) }; sets.insert(item,at:0)
                    sets = Array(sets.prefix(256)); message = "Сохранена версия \(item.revision)"
                } catch { if current(client,session,stream) { message = error.localizedDescription } }
            }
        } catch { message = error.localizedDescription }
    }
    private func export() {
        do {
            let bytes = try PlatformFrequencyBookmarkDocument(name:name,entries:try entries()).exportData()
            let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "frequencies.json"
            if panel.runModal() == .OK, let url = panel.url { try bytes.write(to:url,options:.atomic) }
        } catch { message = error.localizedDescription }
    }
    private func openSource() {
        guard let id = selection, let client = model.client, let stream = model.analyzer?.id else { return }
        let session = model.selectedSessionID; busy = true
        request = Task { @MainActor in
            defer { if current(client,session,stream) { busy = false } }
            do {
                let sweep = try await client.archiveOriginalSweep(sessionID:session,streamID:stream,sweepID:id.rawValue)
                guard current(client,session,stream) else { return }
                model.reviewedSweep = sweep; model.reviewedSweepRequest = UUID(); dismiss()
            } catch { if current(client,session,stream) { message = error.localizedDescription } }
        }
    }
}
