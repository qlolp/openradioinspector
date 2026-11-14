import Foundation
import SwiftUI
import SpektrKomandy

private struct ScanBandDraft: Identifiable {
    var id = UUID().uuidString
    var name = "Поддиапазон"
    var start = "88000000", stop = "108000000", points = "201", rbw = "10000", revisit = "15"
    init() {}
    init(_ value: MonitoringValue) {
        id = value["id"].text; name = value["name"].text
        start = value["start_hz"].scalar; stop = value["stop_hz"].scalar
        points = value["points"].scalar; rbw = value["rbw_hz"].scalar; revisit = value["max_revisit_seconds"].scalar
    }
    var payload: MonitoringValue? {
        guard let start = Int64(start), let stop = Int64(stop), let points = Int64(points),
              let rbw = Int64(rbw), let revisit = Double(revisit), revisit.isFinite else { return nil }
        return .object(["id": .string(id), "name": .string(name), "start_hz": .integer(start),
            "stop_hz": .integer(stop), "points": .integer(points), "rbw_hz": .integer(rbw),
            "max_revisit_seconds": .number(revisit)])
    }
}

struct MonitoringScanPlanPane: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @State private var name = "Обзор объекта"
    @State private var bands = [ScanBandDraft()]
    @State private var confirmed: Set<String> = []
    @State private var expanded = false
    private var editable: Bool { workspace.canConfigure && workspace.project["state"].text != "collecting" }
    private var payload: [String: MonitoringValue]? {
        let values = bands.compactMap(\.payload)
        guard values.count == bands.count else { return nil }
        return ["scan_plan": .object(["name": .string(name), "bands": .array(values)])]
    }
    var body: some View {
        DisclosureGroup("План последовательного обзора", isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 8) {
                Text("Сохранение новой версии требует новых эталонов каждого поддиапазона. Приборы этой кнопкой не запускаются.").font(.caption)
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Название плана", text: $name)
                    ForEach($bands) { $band in
                        GroupBox(band.name) {
                            VStack {
                                HStack {
                                    TextField("Название поддиапазона", text: $band.name)
                                    Button("Убрать") { bands.removeAll { $0.id == band.id } }.disabled(bands.count <= 1)
                                }
                                HStack {
                                    TextField("Начало, Гц", text: $band.start)
                                    TextField("Конец, Гц", text: $band.stop)
                                    TextField("Точек", text: $band.points).frame(width: 70)
                                }
                                HStack {
                                    Picker("RBW, Гц", selection: $band.rbw) { Text("10000").tag("10000"); Text("30000").tag("30000") }
                                    TextField("Возврат, с", text: $band.revisit)
                                }
                            }
                        }
                    }
                    HStack {
                        Button("Добавить поддиапазон") { var band = ScanBandDraft(); band.name = "Поддиапазон \(bands.count + 1)"; bands.append(band) }.disabled(bands.count >= 16)
                        Button("Сохранить новую версию") {
                            guard let payload else { return }
                            workspace.perform { await workspace.command("configure", body: payload, success: "Версия плана сохранена. Подтвердите эталоны поддиапазонов."); await workspace.checkPreflight() }
                        }.disabled(payload == nil)
                    }
                    if payload == nil { Text("Нужны целые частоты в Гц, число точек и конечный срок возврата.").font(.caption) }
                }.disabled(!editable)
                Text("Приёмный профиль: 0,1–800 МГц, 2–290 точек, RBW 10/30 кГц. Для 2,4/5 ГГц нужен отдельно проверенный профиль.").font(.caption)
                ForEach(Array(workspace.preflight["baseline_collection"]["bands"].items.enumerated()), id: \.offset) { _, band in
                    baseline(band)
                }
                ForEach(Array(coverage.enumerated()), id: \.offset) { _, band in
                    Text("\(band["name"].text): \(coverageLabel(band["state"].text)) · возраст \(band["age_seconds"].scalar) с · возврат \(band["last_revisit_seconds"].scalar) с").font(.caption)
                    if band["deadline_missed"].bool == true { Text("Превышен заданный срок возврата.").font(.caption).foregroundStyle(.orange) }
                }
            }
        }
        .onAppear(perform: load)
        .onChange(of: PlatformExactID(workspace.selected)) { _, _ in load() }
        .onChange(of: PlatformExactID(workspace.project["scan_plan"]["id"].text)) { _, _ in load() }
    }
    private var coverage: [MonitoringValue] {
        workspace.status["workers"].items.flatMap { $0["current"]["scan_coverage"]["bands"].items }
    }
    private func coverageLabel(_ state: String) -> String {
        switch state {
        case "fresh": "Обзор актуален"
        case "stale": "Измерение устарело"
        case "paused": "Обзор приостановлен"
        default: "Измерений ещё нет"
        }
    }
    @ViewBuilder private func baseline(_ band: MonitoringValue) -> some View {
        let id = band["band_id"].text
        VStack(alignment: .leading, spacing: 4) {
            Text("\(band["name"].text): \(band["sweeps"].scalar) / 20 свипов · \(band["elapsed_seconds"].scalar) / 60 с").font(.caption)
            Toggle("Подтверждаю обычную обстановку этого поддиапазона", isOn: Binding(get: { confirmed.contains(id) }, set: { if $0 { confirmed.insert(id) } else { confirmed.remove(id) } }))
            Button(band["baseline_id"] == .null ? "Подтвердить эталон поддиапазона" : "Обновить подтверждённый эталон") {
                let body: [String: MonitoringValue] = ["name": .string(String("\(name) · \(band["name"].text)".prefix(200))),
                    "location": workspace.project["site"], "confirm": .boolean(true), "band_id": .string(id)]
                workspace.perform { await workspace.command("baselines", body: body, success: "Эталон поддиапазона сохранён.") }
            }.disabled(workspace.pending || workspace.isArmed || band["ready"].bool != true || !confirmed.contains(id) || (!workspace.preparationSimulation && (!workspace.savedPreparationReady || workspace.preparationDirty)))
        }
    }
    private func load() {
        confirmed = []
        let saved = workspace.project["scan_plan"]["config"]
        if !saved["bands"].items.isEmpty { name = saved["name"].text; bands = saved["bands"].items.map(ScanBandDraft.init) }
        else {
            name = "Обзор объекта"
            var band = ScanBandDraft()
            let settings = workspace.project["profile"]["analyzer"]["settings"]
            if settings["start_hz"] != .null { band.start = settings["start_hz"].scalar }
            if settings["stop_hz"] != .null { band.stop = settings["stop_hz"].scalar }
            if settings["points"] != .null { band.points = settings["points"].scalar }
            if settings["rbw_hz"] != .null { band.rbw = settings["rbw_hz"].scalar }
            bands = [band]
        }
    }
}
