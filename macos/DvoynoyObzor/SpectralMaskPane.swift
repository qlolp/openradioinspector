import Foundation
import SwiftUI
import AppKit
import UniformTypeIdentifiers
import SpektrKomandy

/// File boundaries are independent of AppKit panels, so cancellation and failure use temporary-file tests.
enum SpectralMaskFiles {
    static func readDraft(from url: URL) throws -> String {
        guard url.isFileURL, try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw PlatformError.invalidCommand("Выберите обычный локальный UTF-8 JSON файл")
        }
        let file = try FileHandle(forReadingFrom: url)
        defer { try? file.close() }
        let data = try file.read(upToCount: PlatformSpectralMask.maximumBytes + 1) ?? Data()
        guard data.count <= PlatformSpectralMask.maximumBytes else {
            throw PlatformError.invalidCommand("Файл маски превышает 65 536 байт UTF-8")
        }
        guard let text = String(data: data, encoding: .utf8) else {
            throw PlatformError.invalidCommand("Файл маски должен быть в UTF-8")
        }
        return text
    }

    @discardableResult static func save(_ mask: PlatformSpectralMask, to destination: URL?) throws -> Bool {
        guard let destination else { return false } // User canceled: no filesystem writes.
        guard destination.isFileURL else { throw PlatformError.invalidCommand("Нужен локальный файл назначения") }
        let data = try mask.canonicalData()
        try data.write(to: destination, options: .atomic)
        return true
    }
}

@MainActor
final class SpectralMaskModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String; let stream: String
        static func == (left: Self, right: Self) -> Bool { PlatformExactID.same(left.server, right.server) && PlatformExactID.same(left.session, right.session) && PlatformExactID.same(left.stream, right.stream) }
    }
    @Published var draft = "" { didSet { if oldValue != draft { cancel() } } }
    @Published private(set) var source: PlatformSweep?
    @Published private(set) var result: PlatformSweepMaskAnalysis?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let fetch: (PlatformClient, String, String, String) async throws -> PlatformSweepMaskAnalysis

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         fetch: @escaping (PlatformClient, String, String, String) async throws -> PlatformSweepMaskAnalysis = {
             try await $0.sweepMaskAnalysis(sessionID: $1, sweepID: $2, maskJSON: $3)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds; self.fetch = fetch
    }
    var canAnalyze: Bool { scope != nil && source != nil && client != nil && !draft.isEmpty }
    var canMakeTemplate: Bool {
        guard let source else { return false }
        return source.frequenciesHz.first! < source.frequenciesHz.last!
    }
    func configure(client: PlatformClient?, scope: Scope?, source: PlatformSweep?) {
        if self.scope != scope || self.client !== client { cancel() }
        self.client = client; self.scope = scope
        self.source = source.flatMap {
            guard PlatformExactID.same($0.sessionId, scope?.session), PlatformExactID.same($0.streamId, scope?.stream), $0.levelUnit == "dBm",
                  $0.settings?.filter({ $0.key != "sequence" }).isEmpty == false,
                  (1...16_384).contains($0.levels.count),
                  $0.frequenciesHz.allSatisfy({ $0 <= PlatformSpectralMask.maximumFrequencyHz }) else { return nil }
            return $0
        }
    }
    func reset() { configure(client: nil, scope: nil, source: nil) }
    func cancel() {
        revision += 1; request?.cancel(); timeout?.cancel()
        request = nil; timeout = nil; pending = false; result = nil; errorText = nil
    }
    func importDraft(from url: URL?) {
        guard let url else { return }
        do { draft = try SpectralMaskFiles.readDraft(from: url) }
        catch { errorText = describeSession(error) }
    }
    @discardableResult func exportValidated(to url: URL?, expected: PlatformSweepMaskAnalysis) -> Bool {
        guard let url else { return false }
        guard result == expected, !pending else {
            errorText = "Результат изменился. Сначала выполните новый анализ маски."
            return false
        }
        do { return try SpectralMaskFiles.save(expected.mask, to: url) }
        catch { errorText = describeSession(error); return false }
    }
    func makeTemplate() {
        guard canMakeTemplate, let source else { return }
        let artifact: [String: Any] = ["format": "openradioinspector-spectral-mask", "format_version": 1,
            "name": "NON-NORMATIVE example -80 dBm", "frequency_unit": "Hz", "level_unit": "dBm",
            "interpolation": "linear_dbm", "vertices": [
                ["frequency_hz": source.frequenciesHz.first!, "limit_dbm": -80],
                ["frequency_hz": source.frequenciesHz.last!, "limit_dbm": -80]]]
        do { draft = String(decoding: try JSONSerialization.data(withJSONObject: artifact, options: [.prettyPrinted, .sortedKeys]), as: UTF8.self) }
        catch { errorText = describeSession(error) }
    }
    func analyze() {
        guard !pending, let client, let scope, let source else { return }
        cancel()
        guard !draft.isEmpty, draft.utf8.count <= PlatformSpectralMask.maximumBytes else {
            errorText = "Введите маску JSON до 65 536 байт UTF-8. Проверку содержимого выполняет ядро."
            return
        }
        let currentRevision = revision, submitted = draft
        pending = true
        request = Task { [weak self, fetch] in
            do {
                let value = try await fetch(client, scope.session, source.id, submitted)
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                guard PlatformExactID.same(value.sessionId, scope.session), PlatformExactID.same(value.streamId, scope.stream), PlatformExactID.same(value.sweep.id, source.id),
                      value.sweep.interval == source.interval, value.sweep.levels == source.levels,
                      value.sweep.settings == source.settings,
                      value.frequenciesHz == source.frequenciesHz else {
                    throw PlatformError.invalidResponse("Сервер вернул другой источник анализа маски")
                }
                self.result = value; self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.matches(scope, client, currentRevision) else { return }
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.errorText = "Сеанс/свип не найден или сервер не поддерживает анализ маски (404)."
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.errorText = "Свип несовместим с маской (409): \(message)"
                case PlatformError.http(status: 413, code: _, message: _):
                    self.errorText = "Размер запроса превышает предел ядра (413)."
                default: self.errorText = describeSession(error)
                }
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.matches(scope, client, currentRevision), self.pending else { return }
            self.cancel(); self.errorText = "Время ожидания анализа маски истекло. Можно повторить запрос."
        }
    }
    private func matches(_ scope: Scope, _ client: PlatformClient, _ revision: Int) -> Bool {
        self.scope == scope && self.client === client && self.revision == revision
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteSpectralMaskPane: View {
    @ObservedObject var model: SpectralMaskModel
    var body: some View {
        GroupBox("Спектральная маска · переносимый JSON") {
            VStack(alignment: .leading, spacing: 8) {
                if let source = model.source {
                    Text("Источник следующего анализа: \(source.id) · \(source.streamId)")
                    Text("UTC \(source.interval.startedAt) — \(source.interval.endedAt)").font(.caption)
                } else { Text("Нет архивного dBm свипа выбранного потока").foregroundStyle(.secondary) }
                Text(model.result == nil ? "Черновик НЕ ПРОВЕРЕН ядром" : "Маска проверена для показанного результата")
                    .font(.caption.bold())
                TextEditor(text: $model.draft).font(.system(.caption, design: .monospaced)).frame(minHeight: 150, maxHeight: 220)
                    .border(Color.secondary.opacity(0.4))
                HStack {
                    Button("Импорт JSON…") { importFile() }
                    Button("Пример −80 dBm") { model.makeTemplate() }.disabled(!model.canMakeTemplate)
                    Text("\(model.draft.utf8.count) / 65 536 байт UTF-8").font(.caption)
                }
                Text("Пример NON-NORMATIVE: учебная горизонтальная линия, не норматив и не калибровка. При одинаковых частотах шаблон недоступен.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Анализировать маску") { model.analyze() }.disabled(!model.canAnalyze || model.pending)
                    Button(model.pending ? "Отмена" : "Очистить результат") { model.cancel() }
                    Button("Экспорт проверенной маски…") { exportFile() }.disabled(model.result == nil || model.pending)
                    if model.pending { ProgressView().controlSize(.small) }
                }
                Text("Импорт и редактирование локальны. Только кнопка анализа передаёт текст маски подключённому ядру. POST выполняет только чтение и расчёт: без записи архива, команд приборам и создания кандидатов. Правки очищают результат и отменяют запрос.")
                    .font(.caption).foregroundStyle(.secondary)
                if let result = model.result { resultView(result) }
                ErrorLine(text: model.errorText)
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.cancel() }
    }
    private func importFile() {
        let panel = NSOpenPanel(); panel.allowedContentTypes = [.json]; panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        if panel.runModal() == .OK { model.importDraft(from: panel.url) }
    }
    private func exportFile() {
        guard let expected = model.result else { return }
        let panel = NSSavePanel(); panel.allowedContentTypes = [.json]; panel.nameFieldStringValue = "spectral-mask.json"
        if panel.runModal() == .OK { model.exportValidated(to: panel.url, expected: expected) }
    }
    private func resultView(_ result: PlatformSweepMaskAnalysis) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Результат: \(result.sweep.id) · сеанс \(result.sessionId) · поток \(result.streamId)")
            Text("UTC \(result.sweep.interval.startedAt) — \(result.sweep.interval.endedAt)")
            Text("Зафиксированная маска: \(result.mask.name) · linear_dbm · строго >")
            DisclosureGroup("Записанные настройки источника") {
                Text(result.sweep.settings.keys.sorted().map { "\($0): \(String(describing: result.sweep.settings[$0]!))" }.joined(separator: "\n"))
            }
            SpectralMaskChart(result: result)
            Text("\(result.points) точек · \(result.exceedancePoints) превышений · \(result.regionCount) непрерывных областей")
            if result.regions.isEmpty { Text("Строгих превышений нет") }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 5) {
                    ForEach(result.regions) { region in
                        Text("Индексы \(region.startIndex)…\(region.endIndex) · \(region.startFrequencyHz)…\(region.endFrequencyHz) Гц · \(region.points) точек · пик \(region.peakFrequencyHz) Гц (индекс \(region.peakIndex)): \(String(format: "%+.2f", region.peakExcessDb)) dB")
                    }
                }
            }.frame(maxHeight: 180)
        }.font(.caption)
    }
}

/// Full raw and threshold arrays, including equal-frequency bins, without decimation.
struct SpectralMaskChart: View {
    let result: PlatformSweepMaskAnalysis
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Canvas { context, size in
                let levels = result.sweep.levels + result.thresholdLevels
                let scale = TraceChartVerticalScale(minimum: levels.min()!, maximum: levels.max()!)
                let first = result.frequenciesHz.first!, last = result.frequenciesHz.last!
                let span = max(Double(last - first), 1)
                func point(_ index: Int, _ level: Double) -> CGPoint {
                    CGPoint(x: 8 + Double(result.frequenciesHz[index] - first) / span * max(size.width - 16, 1),
                            y: 8 + (1 - scale.fraction(level)) * max(size.height - 16, 1))
                }
                func draw(_ values: [Double], color: Color, style: StrokeStyle) {
                    var path = Path(); path.move(to: point(0, values[0]))
                    for index in values.indices.dropFirst() { path.addLine(to: point(index, values[index])) }
                    context.stroke(path, with: .color(color), style: style)
                    if values.count == 1 {
                        let p = point(0, values[0]); context.fill(Path(ellipseIn: CGRect(x: p.x - 2, y: p.y - 2, width: 4, height: 4)), with: .color(color))
                    }
                }
                draw(result.sweep.levels, color: .primary, style: StrokeStyle(lineWidth: 1.6))
                draw(result.thresholdLevels, color: .orange, style: StrokeStyle(lineWidth: 1.5, dash: [5, 4]))
            }.frame(height: 220).background(Color.primary.opacity(0.04)).clipShape(RoundedRectangle(cornerRadius: 8))
            Text("Сплошная: исходные dBm · оранжевый пунктир: маска dBm · превышение отдельно в dB")
            Text("Частота, Гц: \(result.frequenciesHz.first!)…\(result.frequenciesHz.last!) · показаны все \(result.points) точек")
        }.font(.caption)
    }
}
