import SwiftUI
import SpektrKomandy

struct NativeRFExplorerRangeFields: View {
    @Binding var draft: NativeRFExplorerDraft
    @AppStorage("rf-explorer.survey-presets") private var savedPresets = Data()
    @State private var name = ""
    @State private var message = ""

    private var presets: [NativeRFExplorerPreset] {
        (try? JSONDecoder().decode([NativeRFExplorerPreset].self, from: savedPresets)) ?? []
    }

    var body: some View {
        if draft.mode == .survey {
            HStack {
                Button("80–120 МГц") { apply(80_000_000...120_000_000) }
                    .accessibilityIdentifier("rf-explorer.preset-80")
                Button("100–400 МГц") { apply(100_000_000...400_000_000) }
                if !presets.isEmpty {
                    Menu("Сохранённые диапазоны") {
                        ForEach(presets) { item in Button(item.name) { apply(item.bounds) } }
                    }
                }
            }.controlSize(.small)
        }
        if draft.usesBounds {
            Picker("Ввод диапазона", selection: Binding(get: { draft.style }, set: { style in
                do { try draft.switchStyle(to: style); message = "" }
                catch { message = error.localizedDescription }
            })) {
                Text("Start / Stop").tag(NativeRFExplorerDraft.Style.startStop)
                Text("Center / Span").tag(NativeRFExplorerDraft.Style.centerSpan)
            }.pickerStyle(.segmented)
            HStack {
                if draft.style == .startStop {
                    LabeledContent("Начало, МГц") {
                        TextField("100", text: $draft.startMHz).accessibilityIdentifier("rf-explorer.start-mhz")
                    }
                    LabeledContent("Конец, МГц") {
                        TextField(draft.mode == .survey ? "400" : "101,110", text: $draft.stopMHz)
                            .accessibilityIdentifier("rf-explorer.stop-mhz")
                    }
                } else {
                    LabeledContent("Центр, МГц") {
                        TextField("250", text: $draft.centerMHz).accessibilityIdentifier("rf-explorer.center-mhz")
                    }
                    LabeledContent("Ширина, МГц") {
                        TextField("300", text: $draft.spanMHz).accessibilityIdentifier("rf-explorer.span-mhz")
                    }
                }
            }.textFieldStyle(.roundedBorder)
            if draft.mode == .survey {
                HStack {
                    TextField("Имя своего диапазона", text: $name).textFieldStyle(.roundedBorder)
                    Button("Сохранить диапазон") {
                        do {
                            let item = try draft.savedRange(name: name)
                            savedPresets = try JSONEncoder().encode(presets.filter { $0.id != item.id } + [item])
                            name = ""; message = ""
                        } catch { message = error.localizedDescription }
                    }.disabled((try? draft.savedRange(name: name)) == nil)
                }.controlSize(.small)
            }
            if let tiles = draft.plannedSurveyTiles {
                Text("План · \(tiles) окон по 112 точек, шаг 10 кГц").font(.caption)
            }
        }
        if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.orange) }
    }

    private func apply(_ bounds: ClosedRange<Int64>) {
        draft.applySurveyRange(bounds)
        message = ""
    }
}
