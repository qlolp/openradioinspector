import Foundation
import SwiftUI
import SpektrKomandy

@MainActor
final class MeasurementMapModel: ObservableObject {
    struct Scope: Equatable {
        let server: String; let session: String
        static func == (a: Self, b: Self) -> Bool {
            PlatformPhonogram.sameID(a.server, b.server)
                && PlatformPhonogram.sameID(a.session, b.session)
        }
    }
    @Published var draft = "" { didSet { if oldValue != draft { clear() } } }
    @Published private(set) var result: PlatformMeasurementMap?
    @Published private(set) var pending = false
    @Published private(set) var errorText: String?
    @Published private(set) var infoText: String?
    private var scope: Scope?
    private var client: PlatformClient?
    private var request: Task<Void, Never>?
    private var timeout: Task<Void, Never>?
    private var revision = 0
    private let timeoutNanoseconds: UInt64
    private let measure: (PlatformClient, String, String) async throws -> PlatformMeasurementMap

    init(timeoutNanoseconds: UInt64 = 6_000_000_000,
         measure: @escaping (PlatformClient, String, String) async throws -> PlatformMeasurementMap = {
             try await $0.measurementMap(sessionID: $1, mapJSON: $2)
         }) {
        self.timeoutNanoseconds = timeoutNanoseconds
        self.measure = measure
    }
    var canBuild: Bool { scope != nil && client != nil && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
    func configure(client: PlatformClient?, scope: Scope?) {
        if self.scope != scope || self.client !== client { clear() }
        self.client = client; self.scope = scope
    }
    var geoJSONText: String {
        guard let result else { return "" }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        guard let data = try? encoder.encode(result.geojson) else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    func reset() { configure(client: nil, scope: nil) }
    func clear() {
        revision += 1; request?.cancel(); timeout?.cancel(); request = nil; timeout = nil
        pending = false; result = nil; errorText = nil; infoText = nil
    }
    func analyze() {
        guard !pending, let client, let scope else { return }
        clear()
        let raw = draft
        let current = revision
        pending = true
        request = Task { [weak self, measure] in
            do {
                let value = try await measure(client, scope.session, raw)
                guard let self, !Task.isCancelled, self.revision == current else { return }
                guard PlatformPhonogram.sameID(value.sessionId, scope.session) else {
                    throw PlatformError.invalidResponse("Сервер вернул другую карту")
                }
                self.result = value
                self.infoText = "Карта построена: точек \(value.summary.pointCount), секторов \(value.summary.sectorCount)."
                self.finish()
            } catch {
                guard let self, !Task.isCancelled, self.revision == current else { return }
                self.errorText = describeSession(error)
                self.finish()
            }
        }
        timeout = Task { [weak self, timeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: timeoutNanoseconds) } catch { return }
            guard let self, self.revision == current, self.pending else { return }
            self.clear(); self.errorText = "Время ожидания карты истекло."
        }
    }
    private func finish() { timeout?.cancel(); timeout = nil; request = nil; pending = false }
}

struct RemoteMeasurementMapPane: View {
    @ObservedObject var model: MeasurementMapModel
    var body: some View {
        GroupBox("Карта точек измерений и секторов") {
            VStack(alignment: .leading, spacing: 8) {
                Text("GeoJSON FeatureCollection: точки измерений и антенные секторы (азимут, ширина луча, радиус). Секторы строятся ядром по дуге большого круга. Координаты вводятся явно; это производный артефакт, не измерение и не привязка GPS.")
                    .font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $model.draft)
                    .font(.system(.caption, design: .monospaced))
                    .frame(minHeight: 110, maxHeight: 160)
                    .border(Color.secondary.opacity(0.4))
                HStack {
                    Button("Построить карту") { model.analyze() }
                        .disabled(!model.canBuild || model.pending)
                    Button("Очистить результат") { model.clear() }
                    if model.pending { ProgressView().controlSize(.small) }
                }
                if let info = model.infoText { Text(info).font(.caption) }
                if let error = model.errorText { ErrorLine(text: error) }
                if let result = model.result {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Точек: \(result.summary.pointCount), секторов: \(result.summary.sectorCount)")
                            .fontWeight(.semibold)
                        let bbox = result.summary.bbox
                        Text("Границы: широта \(String(describing: bbox["min_latitude_deg"] ?? 0))…\(String(describing: bbox["max_latitude_deg"] ?? 0)), долгота \(String(describing: bbox["min_longitude_deg"] ?? 0))…\(String(describing: bbox["max_longitude_deg"] ?? 0))")
                        Text("GeoJSON результата — скопируйте в файл для картографа.")
                            .foregroundStyle(.secondary)
                        ScrollView {
                            Text(model.geoJSONText).font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                        }.frame(maxHeight: 160)
                    }.font(.caption)
                } else {
                    Text("Карта строится только по кнопке; архив не изменяется.").foregroundStyle(.secondary)
                }
            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
        }.onDisappear { model.clear() }
    }
}
