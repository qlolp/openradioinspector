import Foundation
import SwiftUI
import SpektrKomandy

struct ScanBandDraft: Identifiable {
    var id = UUID().uuidString
    var name = "Поддиапазон"
    var start = "88000000", stop = "108000000", points = "201", rbw = "10000", revisit = "15"
    var usesGrid = false
    init() {}
    init(_ value: MonitoringValue) {
        id = value["id"].text; name = value["name"].text
        start = value["start_hz"].scalar; stop = value["stop_hz"].scalar
        usesGrid = value["step_hz"].integer == 10_000
        points = value["points"].scalar; rbw = value["rbw_hz"].scalar; revisit = value["max_revisit_seconds"].scalar
    }
    var gridPoints: Int64? {
        guard let lo = Int64(start), let hi = Int64(stop), lo >= 100_000, hi <= 1_300_000_000,
              lo < hi, (hi - lo).isMultiple(of: 10_000) else { return nil }
        return (hi - lo) / 10_000 + 1
    }
    var payload: MonitoringValue? {
        guard let start = Int64(start), let stop = Int64(stop), start >= 100_000, stop > start,
              let points = usesGrid ? gridPoints : Int64(points), usesGrid || (2...290).contains(points),
              let rbw = Int64(rbw), [10_000, 30_000].contains(rbw),
              let revisit = Double(revisit), (1...3600).contains(revisit) else { return nil }
        var fields: [String: MonitoringValue] = ["id": .string(id), "name": .string(name),
            "start_hz": .integer(start), "stop_hz": .integer(stop), "points": .integer(points),
            "rbw_hz": .integer(rbw), "max_revisit_seconds": .number(revisit)]
        if usesGrid { fields["step_hz"] = .integer(10_000) }
        return .object(fields)
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
                                    if band.usesGrid {
                                        Text(band.gridPoints.map { "\($0) точек" } ?? "Неверная сетка").font(.caption)
                                    } else {
                                        TextField("Точек", text: $band.points).frame(width: 70)
                                    }
                                }
                                HStack {
                                    Toggle("Сетка 10 кГц", isOn: $band.usesGrid)
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
                    if payload == nil { Text("Проверьте границы в Гц, шаг сетки или 2–290 точек и срок возврата 1–3600 с.").font(.caption) }
                }.disabled(!editable)
                Text("Приём tinySA с шагом 10 кГц до 1,3 ГГц требует совместимого профиля ядра. Выше 800 МГц требуется опорное измерение. RBW 10 или 30 кГц.").font(.caption)
                ForEach(Array(workspace.preflight["baseline_collection"]["bands"].items.enumerated()), id: \.offset) { _, band in
                    baseline(band)
                }
                NativeBandCoveragePane(status: workspace.status, sessionID: workspace.project["active_session_id"].text,
                                       verified: workspace.projectStatusIsCurrent())
            }
        }
        .onAppear(perform: load)
        .onChange(of: PlatformExactID(workspace.selected)) { _, _ in load() }
        .onChange(of: PlatformExactID(workspace.project["scan_plan"]["id"].text)) { _, _ in load() }
    }
    @ViewBuilder private func baseline(_ band: MonitoringValue) -> some View {
        let id = band["band_id"].text
        VStack(alignment: .leading, spacing: 4) {
            Text("\(band["name"].text): \(band["sweeps"].scalar) / 20 свипов · \(band["elapsed_seconds"].scalar) / 60 с").font(.caption)
            if let windows = band["window_count"].integer {
                let eligible = band["eligible_windows"].integer ?? windows
                Text("Окон с достаточным фоном \(band["ready_windows"].scalar) из \(eligible) · эталоны подтверждены \(band["confirmed_windows"].scalar)").font(.caption)
                if eligible < windows {
                    Text("Окон вне автоматической охраны · \(windows - eligible)").font(.caption).foregroundStyle(.orange)
                }
            }
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

struct NativeBandCoveragePane: View {
    let status: MonitoringValue
    let sessionID: String
    let verified: Bool

    var body: some View {
        let rows = NativeBandCoverage.rows(status: status, sessionID: sessionID)
        VStack(alignment: .leading, spacing: 8) {
            Text("Покрытие и качество по поддиапазонам").font(.headline)
            if !verified { Label("Нет актуального отчёта ядра", systemImage: "clock.badge.exclamationmark").foregroundStyle(.orange) }
            if rows.isEmpty { Text("Для этого сеанса нет сведений о поддиапазонах.").foregroundStyle(.secondary) }
            ForEach(rows) { row in
                let band = row.band, report = row.report, window = report["last_window"]
                Divider()
                Text(band["name"].text + " · " + NativeGuardSignal.frequency(band["start_hz"]) + "–" +
                     NativeGuardSignal.frequency(band["stop_hz"]) + " МГц").font(.subheadline.bold())
                Text((verified ? row.state : "Последний отчёт · " + row.state) +
                     " · окон " + report["observed_windows"].scalar + " / " + report["window_count"].scalar)
                Text("Возраст старейшего измеренного окна в отчёте · " + NativeBandCoverage.duration(report["age_seconds"]))
                Text("Возврат · " + NativeBandCoverage.duration(report["last_revisit_seconds"]) +
                     " · срок " + NativeBandCoverage.duration(band["max_revisit_seconds"]))
                Text("Качество полосы · " + NativeBandCoverage.quality(report["measurement_qualification"]))
                    .accessibilityIdentifier("coverage.quality." + row.id.rawValue)
                if window != .null {
                    Text("Последнее окно · " + NativeGuardSignal.frequency(window["start_hz"]) + "–" +
                         NativeGuardSignal.frequency(window["stop_hz"]) + " МГц · " + NativeBandCoverage.quality(window["measurement_qualification"]))
                }
                if report["deadline_missed"].bool == true {
                    Label("Срок возврата превышен", systemImage: "clock.badge.exclamationmark").foregroundStyle(.orange)
                }
            }
        }.font(.callout).monospacedDigit().textSelection(.enabled)
    }
}
