import AppKit
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

/// Launch Services and File > Open share one offline window per document URL.
@MainActor
public final class NativeFieldOpenDelegate: NSObject, NSApplicationDelegate {
    private static var windows: [URL: FieldWindowController] = [:]
    public override init() { super.init() }
    public func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls { Self.open(url) }
    }

    static func chooseDocument() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [UTType(exportedAs: "org.openradioinspector.orifield", conformingTo: .zip)]
        panel.allowsMultipleSelection = true
        if panel.runModal() == .OK { for url in panel.urls { open(url) } }
    }

    static func open(_ url: URL) {
        guard url.isFileURL, url.pathExtension.lowercased() == "orifield" else { return }
        let url = url.standardizedFileURL
        if let window = windows[url]?.window {
            window.makeKeyAndOrderFront(nil)
            return
        }
        let controller = NSHostingController(rootView: NativeFieldDocumentView(url: url))
        let window = NSWindow(contentViewController: controller)
        window.isReleasedWhenClosed = false
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.title = url.lastPathComponent + " — только чтение"
        window.representedURL = url
        window.setContentSize(NSSize(width: 1000, height: 680))
        let owner = FieldWindowController(window: window)
        owner.didClose = { windows[url] = nil }
        window.delegate = owner
        windows[url] = owner
        window.center()
        window.makeKeyAndOrderFront(nil)
    }
}

@MainActor
private final class FieldWindowController: NSWindowController, NSWindowDelegate {
    var didClose: (() -> Void)?
    func windowWillClose(_ notification: Notification) { didClose?() }
}

private extension FieldRecordKind {
    var title: String {
        switch self {
        case .sweep: "Свип"
        case .observation: "Наблюдение"
        case .gps: "GPS"
        case .note: "Заметка"
        case .gap: "Разрыв"
        }
    }
}

private struct NativeFieldDocumentView: View {
    let url: URL
    @State private var document: PlatformFieldDocument?
    @State private var failure: String?

    var body: some View {
        Group {
            if let document {
                NativeFieldBrowser(document: document)
            } else if let failure {
                ContentUnavailableView("Не удалось открыть выезд", systemImage: "exclamationmark.triangle",
                                       description: Text(failure))
            } else { ProgressView("Проверка контрольных сумм и ссылок…") }
        }
        .frame(minWidth: 760, minHeight: 460)
        .task(id: url) {
            let work = Task.detached(priority: .userInitiated) { try PlatformFieldDocument(opening: url) }
            do {
                let archive = try await withTaskCancellationHandler(operation: { try await work.value }, onCancel: { work.cancel() })
                try Task.checkCancellation()
                document = archive
            } catch is CancellationError {} catch { failure = error.localizedDescription }
        }
    }
}

private extension FieldSourceDescriptor {
    var exactIDForField: PlatformExactID { PlatformExactID(id) }
}

private struct NativeFieldBrowser: View {
    enum Section: String, CaseIterable {
        case measurements = "Измерения", gps = "Трек GPS", notes = "Заметки", gaps = "Разрывы"
        init(_ kind: FieldRecordKind) {
            switch kind {
            case .sweep, .observation: self = .measurements
            case .gps: self = .gps
            case .note: self = .notes
            case .gap: self = .gaps
            }
        }
    }
    let document: PlatformFieldDocument
    @State private var section: Section = .measurements
    @State private var selected: FieldRecordKey?
    @State private var detail: FieldRecordDetail?
    @State private var detailError: String?

    private func select(_ key: FieldRecordKey) {
        section = Section(key.kind)
        selected = key
    }

    var body: some View {
        VStack(alignment: .leading) {
            Text("Выезд " + document.sessionID).font(.title2).textSelection(.enabled)
            ForEach(document.sources, id: \.exactIDForField) { source in
                Text(source.device + " · " + source.profile + " · " + source.id).font(.caption)
            }
            Text("Свипы: \(document.sweepCount) · Наблюдения: \(document.observationCount) · Только чтение")
                .foregroundStyle(.secondary)
            Picker("Раздел", selection: Binding(get: { section }, set: {
                guard section != $0 else { return }
                section = $0
                selected = nil
            })) {
                ForEach(Section.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
            let rows = document.rows.filter { Section($0.id.kind) == section }
            if section == .gps, !rows.isEmpty { NativeFieldTrack(rows: rows, selection: selected).frame(height: 130) }
            HSplitView {
                List(rows, selection: $selected) { row in
                    VStack(alignment: .leading) {
                        Text(row.id.kind.title + " · " + row.timeUTC)
                        Text(row.summary).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }.tag(row.id)
                }.frame(minWidth: 270)
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        if let selected, let row = document.row(selected) {
                            Text(row.id.kind.title + " " + row.id.identifier.rawValue).font(.headline)
                            Text("Поток " + row.id.stream.rawValue + " · " + row.timeUTC)
                            if let link = row.link {
                                Button("Открыть " + link.kind.title + " " + link.identifier.rawValue) { select(link) }
                                    .accessibilityIdentifier("field.open-linked-source")
                            }
                            ForEach(document.linkedRows(selected)) { source in
                                Button(source.id.kind.title + " " + source.id.identifier.rawValue) { select(source.id) }
                            }
                            if let detailError { Text(detailError).foregroundStyle(.red) }
                            if let detail { NativeFieldDetailView(detail: detail) }
                        } else { Text("Выберите запись").foregroundStyle(.secondary) }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding().textSelection(.enabled)
                }.frame(minWidth: 330)
            }
        }.padding()
        .task(id: selected) {
            detail = nil
            detailError = nil
            guard let selected else { return }
            let work = Task.detached(priority: .userInitiated) { try document.detail(for: selected) }
            do {
                let value = try await work.value
                try Task.checkCancellation()
                detail = value
            } catch is CancellationError {} catch {
                if !Task.isCancelled { detailError = error.localizedDescription }
            }
        }
    }
}

private struct NativeFieldDetailView: View {
    let detail: FieldRecordDetail
    var body: some View {
        switch detail {
        case .sweep(let sweep, let raw):
            Text("\(sweep.interval.startedAt) — \(sweep.interval.endedAt) · \(sweep.levelUnit)")
            MonitoringCurveChart(curves: [.init(title: sweep.levelUnit, color: .blue,
                points: zip(sweep.frequenciesHz, sweep.levels).map { PlotPoint(x: Double($0), y: $1) })])
                .frame(height: 190)
            DisclosureGroup("Исходная запись") { Text(verbatim: raw).font(.caption.monospaced()) }
        case .observation(let observation, let raw):
            Text("\(observation.frequencyHz) Гц · \(observation.mode) · \(observation.level.map { String($0) } ?? "—") \(observation.levelUnit)")
            DisclosureGroup("Исходная запись") { Text(verbatim: raw).font(.caption.monospaced()) }
        case .event(let raw):
            Text(verbatim: raw).font(.caption.monospaced())
        }
    }
}

/// Offline longitude/latitude plot. Each point represents a recorded fix.
private struct NativeFieldTrack: View {
    let rows: [FieldRecordRow]
    let selection: FieldRecordKey?
    var body: some View {
        Canvas { context, size in
            let latitudes = rows.compactMap(\.latitude), longitudes = rows.compactMap(\.longitude)
            guard let west = longitudes.min(), let east = longitudes.max(),
                  let south = latitudes.min(), let north = latitudes.max() else { return }
            for row in rows {
                guard let lon = row.longitude, let lat = row.latitude else { continue }
                let x = east == west ? 0.5 : (lon - west) / (east - west)
                let y = south == north ? 0.5 : (north - lat) / (north - south)
                let radius: CGFloat = row.id == selection ? 4 : 2
                let center = CGPoint(x: 5 + x * (size.width - 10), y: 5 + y * (size.height - 10))
                context.fill(Path(ellipseIn: CGRect(x: center.x - radius, y: center.y - radius,
                    width: radius * 2, height: radius * 2)), with: .color(row.id == selection ? .orange : .blue))
            }
        }.background(.quaternary).accessibilityLabel("Трек GPS, \(rows.count) точек")
    }
}
