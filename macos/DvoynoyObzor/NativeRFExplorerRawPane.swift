import SwiftUI
import SpektrKomandy

struct NativeRFExplorerRawRequest: Identifiable {
    let id = UUID()
    let sweepID: String
    var point: PlatformRFExplorerPoint?
    var loaded: PlatformSweep?
    var sourceIndex: Int?
}

struct NativeRFExplorerRawPane: View {
    let client: PlatformClient
    let session: String
    let stream: String
    let request: NativeRFExplorerRawRequest
    let features: Set<String>
    @Binding var analysisReference: PlatformSweep?
    @State private var analysisTarget: PlatformSweep?
    @Environment(\.dismiss) private var dismiss
    @State private var raw: PlatformSweep?
    @State private var windows: [PlatformRFExplorerWindow] = []
    @State private var index: Int?
    @State private var loading = true
    @State private var playing = false
    @State private var message = ""
    @State private var operation: Task<Void, Never>?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Исходные окна RF Explorer").font(.title2)
                Spacer()
                if let raw, NativeRFExplorerSweepAnalysis.offered(features) {
                    Button("Анализ исходника") { stopPlayback(); analysisTarget = raw }
                }
                Button("Закрыть") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            if let raw {
                let sourceIndex = PlatformExactID.same(request.sweepID, raw.id) ? request.point?.pointIndex ?? request.sourceIndex : nil
                Text("Свип \(raw.id) · \(raw.interval.startedAt) — \(raw.interval.endedAt) UTC").textSelection(.enabled)
                if let sourceIndex, raw.levels.indices.contains(sourceIndex) {
                    Text("Индекс \(sourceIndex) · \(raw.frequenciesHz[sourceIndex]) Гц · \(raw.levels[sourceIndex].formatted()) дБм")
                        .textSelection(.enabled).accessibilityIdentifier("rf-explorer.raw-point")
                }
                NativeSpectrumInspector(sweeps: [raw], scale: nil, threshold: nil, sessionID: session, streamID: stream,
                    rfExplorer: true, liveReceiving: false, reviewedSweep: raw, reviewedPointIndex: sourceIndex,
                    reviewRequest: request.id)
                    .id(raw.exactID)
            } else if loading {
                ProgressView("Чтение исходного свипа…")
            }
            HStack {
                Button("Предыдущее окно") { navigate(by: -1) }.disabled(index.map { $0 <= 0 } ?? true)
                Button("Следующее окно") { navigate(by: 1) }.disabled(index.map { $0 + 1 >= windows.count } ?? true)
                Button(playing ? "Пауза воспроизведения" : "Воспроизвести по UTC") {
                    if playing { stopPlayback() } else { play() }
                }.disabled(!playing && (index.map { $0 + 1 >= windows.count } ?? true))
                if let index {
                    Text("Окно \(windows[index].tile) · \(index + 1) из \(windows.count) сохранённых").font(.caption)
                }
                if loading { ProgressView().controlSize(.small) }
            }.controlSize(.small)
            if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.orange) }
        }
        .padding(16).frame(minWidth: 760, minHeight: 510)
        .sheet(item: $analysisTarget) { source in
            NativeRFExplorerSweepAnalysis(client: client, target: source, features: features, reference: $analysisReference)
        }
        .task { await open() }
        .onDisappear { stopPlayback() }
    }

    private func open() async {
        defer { if !Task.isCancelled { loading = false } }
        do {
            let source: PlatformSweep
            if let loaded = request.loaded { source = loaded }
            else { source = try await client.archiveOriginalSweep(sessionID: session, streamID: stream, sweepID: request.sweepID) }
            try Task.checkCancellation()
            try request.point?.validate(source: source)
            raw = source
            guard let survey = NativeRFExplorerSurveyProgress(source.settings?["survey"]) else {
                throw PlatformError.invalidResponse("Исходный свип не содержит номера прохода.")
            }
            var items: [PlatformRFExplorerWindow] = [], after = 0
            repeat {
                let page = try await client.rfExplorerWindows(sessionID: session, streamID: stream, passID: survey.passID, afterTile: after)
                try Task.checkCancellation()
                guard page.items.last.map({ $0.tile <= survey.tiles }) ?? true else {
                    throw PlatformError.invalidResponse("Номер окна выходит за пределы прохода.")
                }
                items.append(contentsOf: page.items)
                guard let next = page.nextAfterTile else { break }
                after = next
            } while true
            guard let selected = items.firstIndex(where: { $0.tile == survey.tile && PlatformExactID.same($0.sweepId, source.id) }) else {
                throw PlatformError.invalidResponse("Архив не вернул выбранное окно в этом проходе.")
            }
            windows = items; index = selected
        } catch { if !Task.isCancelled { message = error.localizedDescription } }
    }

    private func read(_ at: Int) async throws -> PlatformSweep {
        let row = windows[at]
        let source = try await client.archiveOriginalSweep(sessionID: session, streamID: stream, sweepID: row.sweepId)
        try Task.checkCancellation()
        let original = NativeRFExplorerSurveyProgress(raw?.settings?["survey"])
        guard let survey = NativeRFExplorerSurveyProgress(source.settings?["survey"]), survey.tile == row.tile,
              PlatformExactID.same(survey.passID, original?.passID),
              try PlatformTimestamp(source.interval.startedAt) == PlatformTimestamp(row.startedAt),
              try PlatformTimestamp(source.interval.endedAt) == PlatformTimestamp(row.endedAt) else {
            throw PlatformError.invalidResponse("Исходный свип не соответствует выбранному окну прохода.")
        }
        return source
    }

    private func stopPlayback() {
        operation?.cancel(); operation = nil; playing = false; loading = false
    }

    private func navigate(by distance: Int) {
        guard let index, windows.indices.contains(index + distance) else { return }
        stopPlayback(); loading = true; message = ""
        let next = index + distance
        operation = Task { @MainActor in
            defer { if !Task.isCancelled { loading = false } }
            do { let source = try await read(next); try Task.checkCancellation(); raw = source; self.index = next }
            catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }

    private func play() {
        guard let index, index + 1 < windows.count else { return }
        stopPlayback(); playing = true; message = ""
        operation = Task { @MainActor in
            defer { if !Task.isCancelled { playing = false } }
            do {
                let clock = ContinuousClock(), origin = clock.now
                for next in (index + 1)..<windows.count {
                    let seconds = try windows[index].seconds(until: windows[next])
                    let source = try await read(next)
                    try await clock.sleep(until: origin.advanced(by: .seconds(seconds)))
                    try Task.checkCancellation()
                    raw = source; self.index = next
                }
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
}
