import SwiftUI
import SpektrKomandy

struct NativeBandPlanButton: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let range: ClosedRange<Double>?
    @State private var showing = false
    @State private var showAll = false

    private var allBands: [MonitoringValue] { workspace.bandPlan["items"].items }
    private var visibleBands: [MonitoringValue] { NativeBandPlanBandRow.overlapping(allBands, range: range) }
    private var title: String {
        guard range != nil, let first = visibleBands.first else { return "Полосы частот…" }
        return first["label"].text + (visibleBands.count > 1 ? " +\(visibleBands.count - 1)" : "")
    }

    var body: some View {
        if workspace.signalCatalogAvailable {
            Button {
                showAll = false
                showing.toggle()
            } label: {
                ViewThatFits(in: .horizontal) {
                    Label(title, systemImage: "list.bullet.rectangle")
                    Image(systemName: "list.bullet.rectangle").accessibilityLabel(title)
                }
            }
            .controlSize(.small)
            .frame(maxWidth: 240)
            .help("Справочные назначения частотных полос")
            .popover(isPresented: $showing) { referencePane }
            .onChange(of: workspace.bandPlanContext) { _, _ in showing = false }
        }
    }

    private var referencePane: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Частотный план").font(.headline)
            if let range {
                Text("\(NativeSpectrumInspection.frequency(range.lowerBound)) — \(NativeSpectrumInspection.frequency(range.upperBound))")
                    .monospacedDigit().foregroundStyle(.secondary)
                Toggle("Весь справочник", isOn: $showAll)
            }
            if workspace.bandPlanLoading { ProgressView("Загрузка справочника…").controlSize(.small) }
            if !workspace.bandPlanError.isEmpty { ErrorLine(text: workspace.bandPlanError) }
            if workspace.bandPlan == .null && !workspace.bandPlanLoading {
                Button("Загрузить справочник") { Task { await workspace.loadBandPlan() } }
            }
            if workspace.bandPlan != .null {
                let bands = showAll ? allBands : visibleBands
                if bands.isEmpty {
                    Text("В справочнике нет полос для этого интервала.").foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 9) {
                            ForEach(Array(bands.enumerated()), id: \.offset) { _, band in NativeBandPlanBandRow(band: band) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(maxHeight: 360)
                }
            }
        }.font(.callout).padding().frame(width: 470, alignment: .leading)
    }
}

struct NativeBandPlanBandRow: View {
    let band: MonitoringValue

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text("\(band["label"].text) · \(NativeGuardSignal.frequency(band["start_hz"]))–\(NativeGuardSignal.frequency(band["end_hz"])) МГц")
                if let url = sourceURL { Link("Источник", destination: url).help(band["source_title"].text) }
            }
            let services = band["services"].items.map(\.text).filter { !$0.isEmpty }
            if !services.isEmpty { Text(services.joined(separator: " · ")).foregroundStyle(.secondary) }
            if !band["applicability"].text.isEmpty { Text(band["applicability"].text).foregroundStyle(.secondary) }
        }
    }

    private var sourceURL: URL? {
        guard let parts = URLComponents(string: band["source_url"].text),
              ["http", "https"].contains(parts.scheme?.lowercased() ?? ""),
              parts.host?.isEmpty == false, parts.user == nil, parts.password == nil else { return nil }
        return parts.url
    }

    static func overlapping(_ bands: [MonitoringValue], range: ClosedRange<Double>?) -> [MonitoringValue] {
        guard let range else { return bands }
        return bands.filter { band in
            guard let start = band["start_hz"].integer, let end = band["end_hz"].integer else { return false }
            return Double(start) <= range.upperBound && range.lowerBound <= Double(end)
        }
    }
}
