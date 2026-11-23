import SwiftUI
import SpektrKomandy

struct NativeRFExplorerMarkers: View {
    let client: PlatformClient
    let session: String
    let selectedHz: Int64?
    let shared: Bool
    let operatorActions: Bool
    @Binding var markers: [PlatformFrequencyBookmarkEntry]
    let jump: (Int64) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [HistoryFrequency] = []
    @State private var sets: [PlatformFrequencyBookmarkSet] = []
    @State private var name = "Маркеры RF Explorer"
    @State private var message = ""
    @State private var saving = false
    @State private var operation: Task<Void, Never>?

    private func document() throws -> PlatformFrequencyBookmarkDocument {
        let value = try PlatformFrequencyBookmarkDocument(name: name.trimmingCharacters(in: .whitespacesAndNewlines),
                                                          entries: rows.map { try $0.entry() })
        try value.validate()
        return value
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Частотные маркеры").font(.headline)
                Spacer()
                Button("Закрыть") { dismiss() }
            }
            if shared {
                HStack {
                    Menu("Наборы сеанса") {
                        ForEach(sets, id: \.exactID) { set in
                            Button("\(set.name) · v\(set.revision)") {
                                name = set.name; markers = set.entries
                                rows = set.entries.map { HistoryFrequency(label: $0.label, hz: $0.frequencyHz) }
                            }
                        }
                    }.disabled(sets.isEmpty)
                    TextField("Имя набора", text: $name)
                }
            }
            ForEach($rows) { $row in
                HStack {
                    TextField("Подпись", text: $row.label)
                    TextField("Частота, МГц", text: $row.mhz).monospacedDigit()
                    Text("МГц").font(.caption)
                    Button("Показать") {
                        guard let entry = try? row.entry(), let value = try? document() else { return }
                        markers = value.entries; jump(entry.frequencyHz); dismiss()
                    }.disabled((try? document()) == nil)
                    Button("Убрать") { rows.removeAll { $0.id == row.id } }
                }
            }
            HStack {
                Button("Добавить частоту") {
                    let hz = selectedHz ?? 100_000_000
                    rows.append(HistoryFrequency(label: "M\(rows.count + 1)", hz: hz))
                }.disabled(rows.count >= 4)
                Spacer()
                Button("На график") {
                    markers = (try? document().entries) ?? []
                    dismiss()
                }.disabled(!rows.isEmpty && (try? document()) == nil)
                if shared, operatorActions {
                    Button("Сохранить в сеанс") { save() }.disabled(saving || (try? document()) == nil)
                }
            }
            if !message.isEmpty { Text(message).font(.caption).foregroundStyle(.secondary) }
        }
        .padding(14).frame(width: 540).textFieldStyle(.roundedBorder)
        .onAppear { rows = markers.map { HistoryFrequency(label: $0.label, hz: $0.frequencyHz) } }
        .task {
            guard shared else { return }
            do { let result = try await client.frequencyBookmarks(sessionID: session); try Task.checkCancellation(); sets = result.items }
            catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
        .onDisappear { operation?.cancel() }
    }

    private func save() {
        guard let value = try? document(), !saving else { return }
        saving = true
        operation = Task { @MainActor in
            defer { if !Task.isCancelled { saving = false } }
            do {
                let set = try await client.saveFrequencyBookmarks(sessionID: session, document: value)
                try Task.checkCancellation()
                sets.removeAll { $0.exactID == set.exactID }; sets.insert(set, at: 0)
                markers = set.entries; message = "Сохранена версия \(set.revision)"
            } catch { if !Task.isCancelled { message = error.localizedDescription } }
        }
    }
}
