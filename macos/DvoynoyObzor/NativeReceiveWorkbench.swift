import SwiftUI
import SpektrKomandy

/// All acquisition, band scheduling and detection execute in the core's device actor.
struct NativeReceiveWorkbench: View {
    @Bindable var model: RemoteDualModel
    @Environment(\.dismiss) private var dismiss
    @State private var tab = 0
    @State private var draft = NativeReceiveDraft()
    @State private var status: MonitoringValue = .null
    @State private var bands = [NativeWatchBand()]
    @State private var margin = "6"
    @State private var impulse = "12"
    @State private var knownRanges = ""
    @State private var message = ""
    @State private var busy = false
    @State private var task: Task<Void, Never>?
    @State private var zeroFrequency = "100"
    @State private var zeroData: MonitoringValue = .null
    @State private var artifacts: [MonitoringValue] = []
    @State private var artifactOffset = 0
    @State private var selectedArtifact: MonitoringValue = .null
    @State private var loaded = false
    @State private var recoverySupported = false
    @State private var operations: MonitoringValue = .null
    private var running: Bool { model.worker(for: model.analyzer?.id)?.alive == true }
    private var watching: Bool { status["watch"] != .null }
    private func supports(_ name: String) -> Bool {
        operations[name] != .null && operations[name]["available"].bool != false
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Приём и длительное наблюдение").font(.title2)
                Spacer()
                Button("Закрыть") { dismiss() }
            }
            HStack {
                Label(running ? "Исполнитель работает" : "Приём остановлен", systemImage: running ? "antenna.radiowaves.left.and.right" : "pause.circle")
                    .foregroundStyle(running ? .green : .orange)
                Text("\(status["profile"].text) · ревизия \(status["revision"].scalar)").font(.caption)
            }
            if let error = model.worker(for: model.analyzer?.id)?.lastError { Text(error).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            DisclosureGroup("Диагностика обмена и восстановление") {
                VStack(alignment: .leading, spacing: 6) {
                    if recoverySupported {
                        Button("Восстановить остановленный поток") { recover() }
                            .disabled(running || busy || model.worker(for: model.analyzer?.id)?.stopping == true)
                    }
                    Text("Новая попытка соединения сохраняет диагностику. Наблюдение и ручное управление нужно запускать заново; приём подтверждается новым измерением.")
                        .font(.caption).foregroundStyle(.secondary)
                    ScrollView {
                        VStack(alignment: .leading) {
                            diagnostics(status["transactions"].items, title: "Текущее соединение")
                            diagnostics(status["previous_transactions"].items, title: "Предыдущее соединение")
                        }
                    }.frame(maxHeight: 120)
                }
            }
            Picker("Раздел", selection: $tab) {
                Text("Тракт и развёртка").tag(0)
                if supports("watch.start") || watching { Text("Наблюдение и события").tag(1) }
                Text("Zero Span").tag(2)
                Text("Журнал архива").tag(3)
            }.pickerStyle(.segmented)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    switch tab {
                    case 0: receiveControls
                    case 1: watchControls
                    case 2: zeroControls
                    default: archiveControls
                    }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(4)
            }
            Divider()
            if busy { ProgressView("Ожидание ядра; повторная команда не отправляется…").controlSize(.small) }
            Text(message).font(.caption).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
            Text("При потере USB наблюдение останавливается. После нового соединения фон собирается заново. Генератор в этом сценарии не включается.")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(18).frame(minWidth: 860, idealWidth: 940, minHeight: 620)
            .task(id: [model.serverURL, model.selectedSessionID, model.analyzer?.id ?? ""].map { PlatformExactID($0) }) {
                task?.cancel(); busy = false; loaded = false; status = .null; recoverySupported = false
                operations = .null
                if tab == 1 { tab = 0 }
                if let client = model.client {
                    let session = model.selectedSessionID, stream = model.analyzer?.id
                    let features = try? await client.workbenchFeatures()
                    let available = if let stream {
                        try? await client.tinySAControlOperations(sessionID: session, streamID: stream)
                    } else { nil as MonitoringValue? }
                    if !Task.isCancelled, model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream {
                        recoverySupported = features?.contains("explicit_stream_recovery") == true
                        operations = available ?? .null
                    }
                }
                await refresh(initialize: true)
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .seconds(3)) } catch { return }
                    await refresh()
                }
            }
            .onDisappear { task?.cancel() }
    }

    private func diagnostics(_ rows: [MonitoringValue], title: String) -> some View {
        VStack(alignment: .leading) {
            Text("\(title): \(rows.count) обменов").font(.caption.bold())
            ForEach(Array(rows.suffix(64).reversed().enumerated()), id: \.offset) { _, row in
                Text("\(row["started_at"].text) · \(row["command"].text) · \(row["stage"].text) / \(row["outcome"].text) · \(row["received_bytes"].scalar) байт, \(row["complete_lines"].scalar) строк · \(row["elapsed_seconds"].scalar) с / \(row["deadline_seconds"].scalar) с · prompt: \(row["terminal_received"].scalar)")
                    .font(.caption2.monospaced()).textSelection(.enabled)
            }
        }
    }

    private func recover() {
        guard recoverySupported, !running, !busy, let client = model.client, let stream = model.analyzer?.id else { return }
        let session = model.selectedSessionID; busy = true; message = "Пересоздание соединения…"
        task = Task { @MainActor in
            defer { if model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream { busy = false } }
            do {
                _ = try await client.recoverStream(sessionID: session, streamID: stream)
                try Task.checkCancellation()
                guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream else { return }
                message = "Новая попытка запущена. Дождитесь нового измерения; наблюдение не возобновляется автоматически."
                await refresh()
            } catch {
                if !Task.isCancelled, model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream { message = error.localizedDescription }
            }
        }
    }

    private var receiveControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                field("Аттенюатор, дБ / auto", text: $draft.attenuation)
                Toggle("LNA", isOn: $draft.lna)
                Toggle("Запрос Ultra", isOn: $draft.ultra)
                Picker("Spur", selection: $draft.spur) { ForEach(["auto","off","on"], id: \.self) { Text($0) } }.frame(width: 160)
            }
            if let ultra = status["settings"]["ultra"].bool {
                Text("Настройка ядра Ultra · \(ultra ? "включена" : "выключена")")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if draft.ultra || status["settings"]["ultra"].bool == true {
                Text("Подтвердите Ultra по флажку ENABLE ULTRA на экране прибора.")
                    .font(.caption)
            }
            if let advice = draft.lnaAdvice(stopHz: status["settings"]["stop_hz"].integer) {
                Label(advice, systemImage: "exclamationmark.triangle").font(.caption).foregroundStyle(.orange)
            }
            ultraPreparation
            HStack {
                field("Точек 2–450", text: $draft.points)
                field("Повторы 1–1000", text: $draft.repeats).disabled(draft.useVBW)
                field("Мин. свип, с; 0 = Auto", text: $draft.sweepSeconds)
                Picker("Развёртка", selection: $draft.accuracy) {
                    Text("Обычная").tag("normal"); Text("Точная").tag("precise"); Text("Быстрая").tag("fast")
                }.frame(width: 200)
            }
            HStack {
                Toggle("VBW через повторы прошивки", isOn: $draft.useVBW)
                Picker("VBW/RBW", selection: $draft.vbw) {
                    ForEach(["auto","1","1/3","1/10","1/30","1/100"], id: \.self) { Text($0) }
                }.frame(width: 180).disabled(!draft.useVBW)
            }
            Text("VBW — эквивалент через repeat для прошивки ea71dfd. Малые отношения увеличивают время измерения. С LNA внутренний аттенюатор выключен.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Picker("Триггер", selection: $draft.trigger) {
                    Text("Auto").tag("auto"); Text("Normal").tag("normal"); Text("Single").tag("single")
                }.frame(width: 220)
                field("Уровень триггера, дБм", text: $draft.triggerLevel)
            }
            Text("Normal/Single могут ожидать сигнал. Таймаут или прерывание не повторяет команду автоматически.").font(.caption)
            HStack {
                Button("Применить тракт и развёртку") {
                    do { run("receive.configure", parameters: try draft.parameters()) }
                    catch { message = error.localizedDescription }
                }.disabled(busy || !running || watching || !supports("receive.configure"))
                Button("Считать настройки ядра") { task = Task { await refresh(initialize: true) } }.disabled(busy)
            }
            Text("Подтверждение: \(status["settings_source"].text) · readback: \(status["settings_readback"].scalar)").font(.caption)
        }
    }

    private var ultraPreparation: some View {
        DisclosureGroup("Подготовка Ultra на ZS405") {
            VStack(alignment: .leading, spacing: 6) {
                Text("На экране прибора откройте CONFIG → MORE → ENABLE ULTRA. Если флажок снят, выберите пункт и введите 4321. Проверьте отметку в этом меню.")
                Text("Перед измерением уровней на гигагерцовых частотах остановите приём и соедините CAL и RF коротким кабелем SMA. Выполните CONFIG → LEVEL CAL → CALIBRATE 100kHz to 5.34GHz, затем выключите прибор, снимите кабель и включите прибор. Проверьте белые цифры уровней.")
                Text("Паспортный диапазон LNA заканчивается на 4 ГГц. Между 3,5 и 4 ГГц сравните показания с усилителем и без него. Для диапазона 5 ГГц выключите LNA.")
                Text("Диапазон для ручных измерений Ultra задаётся в панели «Экран и управление tinySA». Проверяйте возможные зеркальные и ложные отклики.")
            }
            .font(.caption)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var watchControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Новые сигналы относительно фона").font(.headline)
            Text("Каждая полоса сначала собирает не менее 20 свипов за 60 секунд. Событие подтверждается в 3 из 5 свипов; сильный импульс регистрируется сразу. Уже присутствовавший при обучении сигнал входит в фон.").font(.caption)
            ForEach($bands) { $band in
                HStack {
                    field("Имя", text: $band.name)
                    field("Начало, МГц", text: $band.start)
                    field("Конец, МГц", text: $band.stop)
                    field("RBW, Гц", text: $band.rbw)
                    field("Точек", text: $band.points)
                    Button("−") { bands.removeAll { $0.id == band.id } }.disabled(bands.count == 1)
                }
            }.disabled(watching || busy)
            HStack {
                Button("Добавить полосу") { bands.append(NativeWatchBand()) }.disabled(bands.count >= 8 || watching)
                field("Превышение фона, дБ", text: $margin)
                field("Импульс, дБ", text: $impulse)
            }.disabled(busy || watching)
            field("Известные полосы, МГц (например 99.5-100.5; 102-103)", text: $knownRanges).disabled(watching || busy)
            HStack {
                Button("Собрать фон и начать наблюдение") { startWatch() }.disabled(busy || !running || watching)
                Button("Остановить наблюдение") { run("watch.stop", parameters: [:]) }.disabled(busy || !running || !watching)
            }
            if watching {
                Text("Свипов: \(status["watch"]["completed_sweeps"].scalar) · новых событий: \(status["watch"]["events_opened"].scalar)").font(.headline)
                ForEach(Array(status["watch"]["bands"].items.enumerated()), id: \.offset) { _, band in
                    Text("\(band["name"].text): \(band["state"].text == "learning" ? "сбор фона" : "наблюдение") · \(band["sweeps"].scalar) свипов · активных событий \(band["active_events"].scalar)")
                }
                eventRows(status["watch"]["recent_events"].items.reversed())
            }
        }
    }

    private var zeroControls: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                field("Частота, МГц", text: $zeroFrequency)
                Button("Один Zero Span") {
                    do { run("acquire.zero", parameters: ["start_hz": .integer(try NativeAnalyzerDraft.hz(zeroFrequency)), "points": .integer(450)]) }
                    catch { message = error.localizedDescription }
                }.disabled(busy || !running || watching)
                if supports("receive.configure") && operations["receive.configure"]["continuous_zero_span"].bool != false {
                    Button("Непрерывный Zero Span") {
                        do {
                            let hz = try NativeAnalyzerDraft.hz(zeroFrequency)
                            run("receive.configure", parameters: ["start_hz": .integer(hz), "stop_hz": .integer(hz)])
                        } catch { message = error.localizedDescription }
                    }.disabled(busy || !running || watching)
                    Button("Вернуть диапазон полосы 1") {
                        do {
                            guard let band = bands.first, case .object(let values) = try band.value() else { return }
                            run("receive.configure", parameters: values.filter { $0.key != "name" })
                        } catch { message = error.localizedDescription }
                    }.disabled(busy || !running || watching)
                }
            }
            Text("График уровня последовательных отсчётов. Точное время каждого отсчёта прошивка не передаёт; показывается общий UTC-интервал, без выдуманной временной сетки.").font(.caption)
            NativeZeroSpanPlot(data: zeroData).frame(height: 260)
            Text("\(zeroData["started_at"].text) — \(zeroData["ended_at"].text) UTC").font(.caption.monospaced())
        }
    }

    private var archiveControls: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Button("Новее") { artifactOffset = max(0, artifactOffset - 50); task = Task { await refresh() } }.disabled(artifactOffset == 0 || busy)
                Button("Старее") { artifactOffset += 50; task = Task { await refresh() } }.disabled(artifacts.count < 50 || busy)
                Text("Страница \(artifactOffset / 50 + 1) · события и исходные Zero Span сохраняются ядром")
            }
            ForEach(Array(artifacts.enumerated()), id: \.offset) { _, artifact in
                HStack {
                    Text(artifact["created_at"].text).font(.caption.monospaced())
                    Text(artifact["filename"].text)
                    Spacer()
                    Button("Открыть") { loadArtifact(artifact) }.disabled(artifact["mime_type"].text != "application/json" || busy)
                }
            }
            if selectedArtifact != .null {
                eventRows(selectedArtifact["events"].items)
                Text(String(selectedArtifact.pretty.prefix(8192))).font(.caption.monospaced()).textSelection(.enabled)
            }
        }
    }

    private func eventRows<S: Sequence>(_ events: S) -> some View where S.Element == MonitoringValue {
        ForEach(Array(Array(events).enumerated()), id: \.offset) { _, event in
            HStack {
                Text("\(event["occurred_at"].text) · \(event["transition"].text) · \(event["band_name"].text)")
                Text("\(event["center_frequency_hz"].scalar) Гц · +\(event["peak_excess_db"].scalar) дБ")
                if !event["source"]["sweep_id"].text.isEmpty {
                    Button("Исходный свип") { openSource(event["source"]["sweep_id"].text) }.disabled(busy)
                }
            }.font(.caption).textSelection(.enabled)
        }
    }

    private func field(_ title: String, text: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(title).font(.caption)
            TextField(title, text: text).textFieldStyle(.roundedBorder)
        }
    }

    private func startWatch() {
        do {
            guard let threshold = Double(margin), let strong = Double(impulse), threshold.isFinite, strong.isFinite else { throw PlatformError.invalidCommand("Проверьте пороги") }
            let allowed = try knownRanges.split(separator: ";").map { part -> MonitoringValue in
                let edges = part.split(separator: "-")
                guard edges.count == 2 else { throw PlatformError.invalidCommand("Известные полосы: начало-конец; начало-конец") }
                return .object(["start_hz": .integer(try NativeAnalyzerDraft.hz(String(edges[0]).trimmingCharacters(in: .whitespaces))),
                                "end_hz": .integer(try NativeAnalyzerDraft.hz(String(edges[1]).trimmingCharacters(in: .whitespaces)))])
            }
            let plan = MonitoringValue.object(["bands": .array(try bands.map { try $0.value() }), "margin_db": .number(threshold), "impulse_db": .number(strong), "allowed": .array(allowed)])
            run("watch.start", parameters: ["document": .string(plan.pretty)])
        } catch { message = error.localizedDescription }
    }

    private func run(_ operation: String, parameters: [String: MonitoringValue]) {
        guard let client = model.client, let stream = model.analyzer, !busy else { return }
        let session = model.selectedSessionID
        busy = true; message = ""
        task = Task { @MainActor in
            defer { busy = false }
            do {
                let current = try await client.tinySASettings(sessionID: session, streamID: stream.id)
                let result = try await client.tinySAOperation(sessionID: session, streamID: stream.id, operation: operation, parameters: parameters, revision: current["revision"].integer)
                try Task.checkCancellation()
                guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                if result["revision"].integer != nil { status = result }
                if result["artifact"]["kind"].text == "zero_span" {
                    let data = try await client.tinySAArtifactJSON(sessionID: session, streamID: stream.id, id: result["artifact"]["id"].text)
                    try Task.checkCancellation()
                    guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                    zeroData = data
                }
                message = "Операция подтверждена ядром. Фактические измерения проверяйте по новым данным."
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }

    private func refresh(initialize: Bool = false) async {
        guard let client = model.client, let stream = model.analyzer else { return }
        let session = model.selectedSessionID
        do {
            let value = try await client.tinySACachedStatus(sessionID: session, streamID: stream.id)
            try Task.checkCancellation()
            guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
            if status["connection_id"] != .null && status["connection_id"] != value["connection_id"] { message = "Соединение изменилось. Проверьте настройки; наблюдение нужно запустить заново." }
            status = value
            if initialize || !loaded {
                draft.load(value["settings"]); loaded = true
                if let sweep = model.spectrumPresentation.sweeps.last, let low = sweep.frequenciesHz.first, let high = sweep.frequenciesHz.last {
                    bands[0].start = NativeAnalyzerDraft.mhz(low); bands[0].stop = NativeAnalyzerDraft.mhz(high)
                }
            }
            if tab == 2 || tab == 3 {
                let page = try await client.tinySAArtifacts(sessionID: session, streamID: stream.id, offset: tab == 2 ? 0 : artifactOffset)
                try Task.checkCancellation()
                guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                artifacts = page["items"].items
                if tab == 2, let newest = artifacts.first(where: { $0["kind"].text == "zero_span" }) {
                    let data = try await client.tinySAArtifactJSON(sessionID: session, streamID: stream.id, id: newest["id"].text)
                    try Task.checkCancellation()
                    guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                    zeroData = data
                }
            }
        } catch { if !Task.isCancelled { message = error.localizedDescription } }
    }

    private func loadArtifact(_ artifact: MonitoringValue) {
        guard let client = model.client, let stream = model.analyzer, !busy else { return }
        let session = model.selectedSessionID
        task?.cancel()
        task = Task { @MainActor in
            do {
                let result = try await client.tinySAArtifactJSON(sessionID: session, streamID: stream.id, id: artifact["id"].text)
                try Task.checkCancellation()
                guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                selectedArtifact = result
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
    private func openSource(_ id: String) {
        guard let client = model.client, let stream = model.analyzer, !busy else { return }
        let session = model.selectedSessionID
        task?.cancel()
        task = Task { @MainActor in
            do {
                let result = try await client.archiveOriginalSweep(sessionID: session, streamID: stream.id, sweepID: id)
                try Task.checkCancellation()
                guard model.client === client, model.selectedSessionID == session, model.analyzer?.id == stream.id else { return }
                model.reviewedSweep = result; model.reviewedSweepRequest = UUID(); dismiss()
            } catch { message = error.localizedDescription }
        }
    }
}

private struct NativeZeroSpanPlot: View {
    let data: MonitoringValue
    var body: some View {
        let levels = data["levels"].items.compactMap(\.double)
        VStack(alignment: .leading) {
            if levels.isEmpty { ContentUnavailableView("Нет Zero Span", systemImage: "waveform.path", description: Text("Запустите измерение или выберите запись архива")) }
            else {
                let low = (levels.min() ?? -100) - 5, high = (levels.max() ?? 0) + 5
                Text(String(format: "%.1f…%.1f дБм · %d отсчётов", low, high, levels.count)).font(.caption)
                Canvas { context, size in
                    var path = Path()
                    for (i, level) in levels.enumerated() {
                        let point = CGPoint(x: Double(i) / Double(max(1, levels.count - 1)) * size.width,
                                            y: (high - level) / (high - low) * size.height)
                        if i == 0 { path.move(to: point) } else { path.addLine(to: point) }
                    }
                    context.stroke(path, with: .color(.cyan), lineWidth: 1.5)
                }.background(.black.opacity(0.8))
                Text("Отсчёт 0 → \(levels.count - 1) · время отдельных отсчётов неизвестно").font(.caption)
            }
        }
    }
}
