import SwiftUI
import AppKit
import UniformTypeIdentifiers
import SpektrKomandy

struct NativeAnalyzerControls: View {
    @ObservedObject var model: RemoteDualModel
    @State private var draft = NativeAnalyzerDraft()
    @State private var lastEdited = NativeAnalyzerDraft.Field.start
    @State private var message = ""
    @State private var applying = false
    @State private var operation: Task<Void, Never>?
    @State private var loaded = false
    @AppStorage("native.receive.profile") private var savedProfile = Data()
    @State private var profileName = "Обзор"
    @State private var profilePanel = false
    @State private var receivePanel = false

    private let bandwidths = ["auto", "200", "1000", "3000", "10000", "30000", "100000", "300000", "600000", "850000"]

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ViewThatFits(in: .horizontal) {
                HStack(alignment: .bottom, spacing: 8) { frequencyFields; actionButtons }
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .bottom, spacing: 8) { frequencyFields }
                    HStack(spacing: 8) { actionButtons }
                }
            }.controlSize(.small).disabled(applying)
            Text(message.isEmpty ? "Поля: МГц · применение по кнопке · VBW и режимы: «Тракт / Наблюдение»" : message)
                .font(.caption2).foregroundStyle(.secondary).lineLimit(2).fixedSize(horizontal: false, vertical: true)
        }
        .onChange(of: model.spectrumPresentation.sweeps.last?.id, initial: true) { _, _ in
            if !loaded, let sweep = model.spectrumPresentation.sweeps.last {
                if let start = sweep.frequenciesHz.first, let stop = sweep.frequenciesHz.last {
                    draft.start = NativeAnalyzerDraft.mhz(start); draft.stop = NativeAnalyzerDraft.mhz(stop)
                    try? draft.synchronize(.start); loaded = true
                }
                draft.loadBandwidth(sweep.settings?["rbw_hz"])
            }
        }
        .onDisappear { operation?.cancel() }
        .onChange(of: model.worker(for: model.analyzer?.id)?.state) { old, new in
            if old != new && new == "running" && !applying { message = "" }
        }
        .sheet(isPresented: $receivePanel) { NativeReceiveWorkbench(model: model) }
    }
    private var frequencyFields: some View {
        Group {
                NativeFrequencyField(title: "Start · МГц", text: $draft.start) { changed(.start) }
                NativeFrequencyField(title: "Stop · МГц", text: $draft.stop) { changed(.stop) }
                NativeFrequencyField(title: "Center · МГц", text: $draft.center) { changed(.center) }
                NativeFrequencyField(title: "Span · МГц", text: $draft.span) { changed(.span) }
                VStack(alignment: .leading, spacing: 2) {
                    Text("RBW · Гц").font(.caption2)
                    Picker("RBW", selection: $draft.rbw) {
                        ForEach(bandwidths, id: \.self) { Text($0 == "auto" ? "AUTO" : $0).tag($0) }
                    }.labelsHidden().frame(width: 105)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text("VBW").font(.caption2)
                    Text("В тракте").font(.caption).foregroundStyle(.secondary).frame(height: 22)
                        .help("Эквивалент VBW/RBW через повторные измерения прошивки доступен в настройках тракта.")
                }
        }
    }
    private var actionButtons: some View {
        Group {
                Button(applying ? "Применение…" : "Применить") { apply() }
                    .disabled(applying || model.busy || model.worker(for: model.analyzer?.id)?.alive != true)
                Button("Тракт / Наблюдение") { receivePanel = true }
                Button("Считать") { load() }.disabled(applying || model.analyzer == nil)
                Button("Профиль") { profilePanel = true }
                    .popover(isPresented: $profilePanel) {
                        VStack(alignment: .leading, spacing: 10) {
                            TextField("Имя профиля", text: $profileName)
                            Button("Сохранить текущие поля") { saveProfile() }
                            Button("Загрузить сохранённый") { restoreProfile(savedProfile) }.disabled(savedProfile.isEmpty)
                            HStack {
                                Button("Экспорт JSON…") { exportProfile() }
                                Button("Импорт JSON…") { importProfile() }
                            }
                            Text("Загрузка заполняет поля. Прибор перестраивается только кнопкой «Применить».")
                                .font(.caption).foregroundStyle(.secondary)
                        }.padding().frame(width: 330)
                    }

        }
    }

    private func saveProfile() {
        do {
            let profile = try NativeMeasurementProfile(name: profileName, draft: draft, edited: lastEdited)
            savedProfile = try JSONEncoder().encode(profile)
            message = "Профиль сохранён: \(profile.name). Прибор не перестраивался."
        } catch { message = error.localizedDescription }
    }
    private func restoreProfile(_ data: Data) {
        do {
            let profile = try NativeMeasurementProfile.decode(data)
            draft = try profile.draft(); profileName = profile.name; lastEdited = .start; loaded = true
            message = "Профиль \(profile.name) загружен в поля. Для перестройки нажмите «Применить»."
        } catch { message = error.localizedDescription }
    }
    private func exportProfile() {
        do {
            let profile = try NativeMeasurementProfile(name: profileName, draft: draft, edited: lastEdited)
            let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "receive-profile.json"
            guard panel.runModal() == .OK, let url = panel.url else { return }
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(profile).write(to: url, options: .atomic)
            message = "Профиль экспортирован"
        } catch { message = error.localizedDescription }
    }
    private func importProfile() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]; panel.allowsMultipleSelection = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
            restoreProfile(try handle.read(upToCount: 16_385) ?? Data())
        } catch { message = error.localizedDescription }
    }

    private func changed(_ field: NativeAnalyzerDraft.Field) { lastEdited = field }
    private func load() {
        guard let client = model.client, let stream = model.analyzer else { return }
        let session = model.selectedSessionID
        operation?.cancel(); applying = true
        operation = Task { @MainActor in
            defer { applying = false }
            do {
                let status = try await client.tinySASettings(sessionID: session, streamID: stream.id)
                try Task.checkCancellation()
                if let low = status["settings"]["start_hz"].integer, let high = status["settings"]["stop_hz"].integer {
                    draft.start = NativeAnalyzerDraft.mhz(low); draft.stop = NativeAnalyzerDraft.mhz(high)
                    try draft.synchronize(.start); lastEdited = .start; loaded = true
                }
                let rbw = status["settings"]["rbw_hz"].scalar
                if bandwidths.contains(rbw) { draft.rbw = rbw }
                message = "Настройки ядра · источник: \(status["settings_source"].text). Подтверждение readback: \(status["settings_readback"].bool == true ? "да" : "нет")"
            } catch { message = error.localizedDescription }
        }
    }
    private func apply() {
        guard let client = model.client, let stream = model.analyzer, !applying else { return }
        do { try draft.synchronize(lastEdited) } catch { message = error.localizedDescription; return }
        let requested = draft, session = model.selectedSessionID
        applying = true; message = "Ожидание подтверждения ядра…"
        operation = Task { @MainActor in
            defer { applying = false }
            do {
                let status = try await client.tinySASettings(sessionID: session, streamID: stream.id)
                guard status["mode"].text == "monitoring", status["settings"]["stop_hz"].integer != nil else {
                    throw NativeAnalyzerDraft.DraftError.invalid("Нужен подключённый tinySA в режиме monitoring без лабораторной аренды.")
                }
                let start = try NativeAnalyzerDraft.hz(requested.start), stop = try NativeAnalyzerDraft.hz(requested.stop)
                let values: [String: MonitoringValue] = ["start_hz": .integer(start), "stop_hz": .integer(stop),
                    "rbw_hz": requested.rbw == "auto" ? .string("auto") : .integer(Int64(requested.rbw) ?? 10_000)]
                try Task.checkCancellation()
                guard client === model.client, session == model.selectedSessionID, stream.id == model.analyzer?.id else { return }
                _ = try await client.tinySAOperation(sessionID: session, streamID: stream.id, operation: "receive.configure", parameters: values, revision: status["revision"].integer)
                try Task.checkCancellation()
                message = "Диапазон и RBW подтверждены ядром. Проверьте новый свип; аппаратный readback не заявлен."
            } catch { message = "Применение не завершено: \(error.localizedDescription). Считайте состояние перед повтором." }
        }
    }
}

private struct NativeFrequencyField: View {
    let title: String
    @Binding var text: String
    let edited: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2)
            TextField(title, text: Binding(get: { text }, set: { text = $0; edited() }))
                .textFieldStyle(.roundedBorder).font(.system(.caption, design: .monospaced))
                .frame(minWidth: 75, maxWidth: 115)
        }
    }
}
