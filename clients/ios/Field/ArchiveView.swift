import SpektrKomandy
import SwiftUI

extension FieldRecordKind {
    var title: String {
        switch self {
        case .sweep: "Спектр"
        case .observation: "Наблюдение"
        case .note: "Заметка"
        case .gps: "GPS"
        case .gap: "Разрыв"
        }
    }
}

struct ArchiveList: View {
    let document: PlatformFieldDocument
    let demo: Bool
    @State private var kind: FieldRecordKind?

    var body: some View {
        List {
            Section(demo ? "Учебный выезд · синтетические данные" : "Выезд") {
                Text(verbatim: document.sessionID)
                DisclosureGroup("Источники (\(document.sources.count))") {
                    ForEach(document.sources, id: \.fieldID) { source in
                        LabeledContent(source.device, value: source.profile)
                        Text(verbatim: source.id).font(.caption)
                    }
                }
            }
            Section("Записи") {
                Picker("Показать", selection: $kind) {
                    Text("Все").tag(nil as FieldRecordKind?)
                    ForEach([FieldRecordKind.sweep, .observation, .note, .gps, .gap], id: \.self) { item in
                        Text(item.title).tag(Optional(item))
                    }
                }
                ForEach(document.rows.filter { kind == nil || $0.id.kind == kind }) { row in
                    NavigationLink(value: row.id) {
                        VStack(alignment: .leading) {
                            Text(row.id.kind.title).font(.headline)
                            Text(verbatim: row.summary)
                            Text(verbatim: row.timeUTC).font(.caption).foregroundStyle(.secondary)
                        }
                    }.accessibilityIdentifier("row-" + row.id.kind.rawValue)
                }
            }
        }
    }
}

private extension FieldSourceDescriptor {
    var fieldID: PlatformExactID { .init(id) }
}

struct ArchiveRecord: View {
    let file: FieldFile
    let key: FieldRecordKey
    @State private var detail: FieldRecordDetail?
    @State private var failure: String?

    var body: some View {
        List {
            if let row = file.document.row(key) {
                Section {
                    LabeledContent("ID", value: row.id.identifier.rawValue)
                    LabeledContent("Поток", value: row.id.stream.rawValue)
                    LabeledContent("Время", value: row.timeUTC)
                }
                if let detail {
                    switch detail {
                    case .sweep(let sweep, let raw):
                        SweepSections(sweep: sweep)
                        rawRecord(raw)
                    case .observation(let observation, let raw):
                        ObservationSection(observation: observation)
                        rawRecord(raw)
                    case .event(let description, let raw):
                        Section(key.kind.title) { Text(verbatim: description) }
                        rawRecord(raw)
                    }
                } else if let failure { Text(failure).foregroundStyle(.red) }
                else { ProgressView() }
                if let link = row.link {
                    Section("Связанная запись") {
                        NavigationLink(link.kind.title + " · " + link.identifier.rawValue, value: link)
                            .accessibilityIdentifier("linked-" + link.kind.rawValue)
                    }
                }
                let linked = file.document.linkedRows(key)
                if !linked.isEmpty {
                    Section("Ссылки на эту запись") {
                        ForEach(linked) { item in
                            NavigationLink(item.id.kind.title + " · " + item.id.identifier.rawValue, value: item.id)
                        }
                    }
                }
            }
        }
        .navigationTitle(key.kind.title)
        .textSelection(.enabled)
        .task(id: key) {
            detail = nil
            failure = nil
            do {
                let result = try await Task.detached { try file.document.detail(for: key) }.value
                try Task.checkCancellation()
                detail = result
            } catch { if !Task.isCancelled { failure = error.localizedDescription } }
        }
    }

    private func rawRecord(_ raw: String) -> some View {
        DisclosureGroup("Исходная запись") { Text(verbatim: raw).font(.caption.monospaced()) }
    }
}

struct SweepSections: View {
    let sweep: PlatformSweep

    var body: some View {
        Section("Спектр · " + sweep.levelUnit) {
            Canvas { context, size in
                let frequencies = sweep.frequenciesHz
                let levels = sweep.levels
                guard let first = frequencies.first, let last = frequencies.last,
                      let low = levels.min(), let high = levels.max() else { return }
                let y = PlatformTraceVerticalScale(minimum: low, maximum: high)
                var path = Path()
                for index in frequencies.indices {
                    let x = first == last ? size.width / 2 : Double(frequencies[index] - first) / Double(last - first) * size.width
                    let point = CGPoint(x: x, y: (1 - y.fraction(levels[index])) * size.height)
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                    if frequencies.count == 1 {
                        context.fill(Path(ellipseIn: CGRect(x: point.x - 2, y: point.y - 2, width: 4, height: 4)), with: .color(.accentColor))
                    }
                }
                context.stroke(path, with: .color(.accentColor), lineWidth: 1.5)
            }.frame(height: 150).accessibilityLabel("Спектр, \(sweep.levels.count) точек в \(sweep.levelUnit)")
            LabeledContent("Диапазон, Гц", value: "\(sweep.frequenciesHz.first!) – \(sweep.frequenciesHz.last!)")
            LabeledContent("Начало", value: sweep.interval.startedAt)
            LabeledContent("Окончание", value: sweep.interval.endedAt)
            NavigationLink("Исходные значения (\(sweep.levels.count))") {
                List(sweep.levels.indices, id: \.self) { index in
                    LabeledContent(String(sweep.frequenciesHz[index]) + " Гц", value: String(sweep.levels[index]) + " " + sweep.levelUnit)
                }.navigationTitle("Исходные значения").textSelection(.enabled)
            }.accessibilityIdentifier("source-values")
        }
    }
}

struct ObservationSection: View {
    let observation: PlatformObservation
    var body: some View {
        Section("Наблюдение") {
            LabeledContent("Частота, Гц", value: String(observation.frequencyHz))
            LabeledContent("Режим", value: observation.mode)
            LabeledContent(observation.levelUnit, value: observation.level.map { String($0) } ?? "—")
            LabeledContent("Начало", value: observation.interval.startedAt)
            LabeledContent("Окончание", value: observation.interval.endedAt)
            if let open = observation.squelchOpen { LabeledContent("Шумоподавитель", value: open ? "Открыт" : "Закрыт") }
            if let mode = observation.decodedMode { LabeledContent("Определённый режим", value: mode) }
        }
    }
}
