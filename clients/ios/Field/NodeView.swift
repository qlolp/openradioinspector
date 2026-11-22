import SpektrKomandy
import SwiftUI
import UniformTypeIdentifiers

struct NodeView: View {
    let isVisible: Bool
    @Environment(\.scenePhase) private var scene
    @State private var model = NodeModel()
    @State private var address = ""
    @State private var code = ""
    @State private var certificate: ReaderCertificate?
    @State private var checked = false
    @State private var choosing = false
    @State private var confirming = false

    var body: some View {
        NavigationStack {
            Form {
                if let profile = model.profile {
                    Section("Сопряжённый узел") {
                        Text(verbatim: profile.origin)
                        if !model.status.isEmpty { Text(model.status) }
                        Button(model.running ? "Отключиться" : "Подключиться") {
                            model.running.toggle()
                            if !model.running { model.status = "Отключено" }
                        }.disabled(model.busy)
                    }
                    if model.running, let reader = model.reader {
                        Section("Сеансы") {
                            if model.sessions.isEmpty { Text("Архив узла пуст") }
                            ForEach(model.sessions, id: \.exactID) { session in
                                NavigationLink(session.name) {
                                    List {
                                        let streams = session.streams.filter { $0.isReceiver || $0.role == "spectrum_analyzer" }
                                        if streams.isEmpty { Text("В сеансе нет потоков измерений") }
                                        ForEach(streams, id: \.exactID) { stream in
                                            NavigationLink(stream.device + " · " + stream.id) {
                                                if stream.isReceiver {
                                                    ReceiverHistory(model: model, session: session.id, stream: stream.id)
                                                } else {
                                                    RemoteSpectrum(model: model, session: session.id, stream: stream.id)
                                                }
                                            }
                                        }
                                    }.navigationTitle(session.name)
                                }
                            }
                        }
                        Section("Последние события") {
                            if let journal = model.journal {
                                LabeledContent("Прочитано до", value: String(journal.cursor))
                                ForEach(journal.recent.reversed()) { item in
                                    VStack(alignment: .leading) {
                                        Text(verbatim: item.type)
                                        Text(verbatim: "#\(item.id) · \(item.occurredAt)").font(.caption)
                                        if let id = item.sweepID, let session = item.sessionID, let stream = item.streamID {
                                            NavigationLink("Исходный спектр") { OriginalSweep(reader: reader, session: session, stream: stream, id: id) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Section {
                        Button("Удалить сопряжение", role: .destructive) { confirming = true }.disabled(model.busy)
                    }
                } else {
                    Section("Локальный Mac или Raspberry Pi") {
                        TextField("https://имя.local:порт", text: $address)
                            .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                            .onChange(of: address) { checked = false }
                        Button("Выбрать сертификат CA") { choosing = true }
                        if let certificate {
                            Text(verbatim: "SHA-256\n" + certificate.fingerprint)
                                .font(.caption.monospaced()).textSelection(.enabled)
                            Toggle("Сверил отпечаток с узлом", isOn: $checked)
                        }
                        SecureField("Одноразовый код читателя", text: $code)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                        Button("Выполнить сопряжение") {
                            guard let certificate, checked else { return }
                            let submitted = code
                            code = ""
                            Task { await model.pair(address: address, certificate: certificate, code: submitted) }
                        }.disabled(model.busy || !checked || address.isEmpty || code.isEmpty)
                    } footer: {
                        Text("На узле создайте одноразовый код с правами reader. Выберите публичный сертификат его CA в формате PEM или DER.")
                    }
                }
                if model.busy { ProgressView() }
                if let failure = model.failure {
                    Section {
                        Text(failure).foregroundStyle(.red)
                    } footer: {
                        Text("Проверьте Wi-Fi, имя в сертификате и доступ к локальной сети в Настройках iPhone. Полевой архив открывается без сети.")
                    }
                }
            }
            .navigationTitle("Локальный узел")
        }
        .onAppear { model.load() }
        .task(id: model.running && isVisible && scene == .active) {
            if model.running && isVisible && scene == .active { await model.follow() }
        }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.data]) { result in
            do {
                certificate = try ReaderCertificate(url: result.get())
                checked = false
            } catch {
                if (error as NSError).code != NSUserCancelledError {
                    certificate = nil; checked = false; model.failure = error.localizedDescription
                }
            }
        }
        .confirmationDialog("Удаление сопряжения", isPresented: $confirming) {
            Button("Отозвать на узле и удалить", role: .destructive) { Task { await model.forget(revoke: true) } }
            Button("Удалить только с iPhone", role: .destructive) { Task { await model.forget(revoke: false) } }
        }
    }
}

private struct RemoteSpectrum: View {
    let model: NodeModel
    let session, stream: String
    @State private var newest: PlatformSweep?
    @State private var failure: String?

    var body: some View {
        List {
            if model.running, let reader = model.reader {
                NavigationLink("История спектра") { SweepHistory(reader: reader, session: session, stream: stream) }
                if let newest { SweepSections(sweep: newest) }
                else if failure == nil { Text("Ожидание исходного спектра") }
            } else { Text("Ожидание подключения к узлу") }
            if let failure { Text(failure).foregroundStyle(.red) }
        }
        .navigationTitle("Последний спектр")
        .task(id: model.revision) {
            guard let reader = model.reader else { return }
            do {
                let latest = try await reader.history(session: session, stream: stream).items.first
                if let latest {
                    if !PlatformExactID.same(latest.id, newest?.id) {
                        let value = try await reader.sweep(session: session, stream: stream, id: latest.id)
                        try Task.checkCancellation()
                        newest = value
                    }
                } else { newest = nil }
                failure = nil
            } catch { if !Task.isCancelled { failure = error.localizedDescription } }
        }
    }
}

private struct SweepHistory: View {
    let reader: PlatformFieldReader
    let session, stream: String
    @State private var rows: [PlatformArchiveSweepRow] = []
    @State private var next: String?
    @State private var busy = false
    @State private var failure: String?

    var body: some View {
        List {
            ForEach(rows, id: \.exactID) { row in
                NavigationLink {
                    OriginalSweep(reader: reader, session: session, stream: stream, id: row.id)
                } label: {
                    VStack(alignment: .leading) {
                        Text(verbatim: row.interval.startedAt)
                        Text(verbatim: "\(row.peakFrequencyHz) Гц · \(row.peakLevel) \(row.levelUnit)").font(.caption)
                    }
                }
            }
            if busy { ProgressView() }
            if next != nil { Button("Загрузить ещё") { Task { await page() } }.disabled(busy) }
            if let failure { Text(failure).foregroundStyle(.red); Button("Повторить") { Task { await page() } } }
            else if rows.isEmpty && !busy { Text("Свипов пока нет") }
        }
        .navigationTitle("История спектра")
        .task { if rows.isEmpty { await page() } }
    }

    private func page() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            let page = try await reader.history(session: session, stream: stream, after: next)
            try Task.checkCancellation()
            rows.append(contentsOf: page.items)
            next = page.nextCursor
            failure = nil
        } catch { if !Task.isCancelled { failure = error.localizedDescription } }
    }
}

private struct OriginalSweep: View {
    let reader: PlatformFieldReader
    let session, stream, id: String
    @State private var value: PlatformSweep?
    @State private var failure: String?

    var body: some View {
        List {
            Text(verbatim: id)
            if let value { SweepSections(sweep: value) }
            else if let failure { Text(failure).foregroundStyle(.red) }
            else { ProgressView() }
        }
        .navigationTitle("Исходный спектр")
        .task(id: id) {
            do {
                let source = try await reader.sweep(session: session, stream: stream, id: id)
                try Task.checkCancellation()
                value = source
            } catch { if !Task.isCancelled { failure = error.localizedDescription } }
        }
    }
}


private struct ReceiverHistory: View {
    let model: NodeModel
    let session, stream: String
    @State private var rows: [PlatformObservation] = []
    @State private var failure: String?

    var body: some View {
        List {
            if rows.isEmpty && failure == nil { Text("Наблюдений пока нет") }
            if let failure { Text(failure).foregroundStyle(.red) }
            ForEach(rows.reversed(), id: \.exactID) { observation in
                NavigationLink {
                    List {
                        LabeledContent("ID", value: observation.id)
                        ObservationSection(observation: observation)
                    }.navigationTitle("Наблюдение").textSelection(.enabled)
                } label: {
                    VStack(alignment: .leading) {
                        Text(verbatim: String(observation.frequencyHz) + " Гц · " + observation.mode)
                        Text(verbatim: (observation.level.map { String($0) } ?? "—") + " " + observation.levelUnit)
                        Text(verbatim: observation.interval.startedAt).font(.caption)
                    }
                }
            }
        }
        .navigationTitle("Наблюдения")
        .task(id: model.revision) {
            guard let reader = model.reader else { return }
            do {
                let observations = try await reader.observations(session: session, stream: stream)
                try Task.checkCancellation()
                rows = observations
                failure = nil
            } catch { if !Task.isCancelled { failure = error.localizedDescription } }
        }
    }
}
