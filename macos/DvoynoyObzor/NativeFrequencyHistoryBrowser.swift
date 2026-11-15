import Foundation
import Observation
import SpektrKomandy

@MainActor @Observable final class NativeFrequencyHistoryBrowser {
    private(set) var rows: [PlatformFrequencyHistoryRow] = []
    private(set) var nextCursor: String?
    private(set) var busy = false
    private(set) var message = ""
    private var revision = UUID()
    private var cursors: Set<String> = []
    let maximumRows: Int
    init(maximumRows: Int = 1000) { self.maximumRows = min(1000,max(1,maximumRows)) }
    func reset() { revision = UUID(); rows = []; nextCursor = nil; cursors = []; busy = false; message = "" }
    func load(next: Bool = false, fetch: (String?) async throws -> PlatformFrequencyHistoryPage) async {
        guard !busy, !Task.isCancelled, !next || nextCursor != nil else { return }
        if !next { reset() }
        let token = revision, cursor = next ? nextCursor : nil
        busy = true; message = "Загрузка истории…"
        defer { if token == revision { busy = false } }
        do {
            let page = try await fetch(cursor)
            try Task.checkCancellation(); guard revision == token else { return }
            let oldIDs = Set(rows.map(\.id))
            guard page.items.allSatisfy({ !oldIDs.contains($0.id) }),
                  page.nextCursor == nil || !cursors.contains(page.nextCursor!) else {
                throw PlatformError.invalidResponse("Повтор исходника или курсора; обновите выборку")
            }
            if let previous = rows.last, let first = page.items.first,
               let earlier = NativeSpectrumTimeline.date(first.interval.startedAt), let later = NativeSpectrumTimeline.date(previous.interval.startedAt),
               earlier > later || (earlier == later && !first.sweepId.utf8.lexicographicallyPrecedes(previous.sweepId.utf8)) {
                throw PlatformError.invalidResponse("Нарушен порядок страниц истории")
            }
            if let cursor { cursors.insert(cursor) }
            let remaining = maximumRows - rows.count
            rows.append(contentsOf:page.items.prefix(remaining))
            let full = rows.count == maximumRows
            nextCursor = full ? nil : page.nextCursor
            message = full ? "Достигнут предел \(maximumRows) свипов. Сузьте интервал для подробного просмотра." :
                "\(rows.count) свипов" + (nextCursor == nil ? " · конец выборки" : " · доступны более ранние")
        } catch { if revision == token && !Task.isCancelled { message = error.localizedDescription } }
    }
}
