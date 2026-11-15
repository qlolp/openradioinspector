import Foundation
import Observation
import SpektrKomandy

@MainActor @Observable
final class NativeArchiveBrowser {
    private(set) var rows: [PlatformArchiveSweepRow] = []
    private(set) var sources: [PlatformSweep] = []
    private(set) var nextCursor: String?
    private(set) var busy = false
    private(set) var didLoad = false
    private(set) var message = ""
    private var revision = UUID()

    func reset() {
        revision = UUID(); rows = []; sources = []; nextCursor = nil; busy = false; didLoad = false; message = ""
    }

    func row(id: PlatformExactID) -> PlatformArchiveSweepRow? {
        rows.first { $0.exactID == id }
    }

    /// Each search replaces the previous result. Callers cancel their task when scope changes.
    func search(client: PlatformClient, session: String, stream: String, start: String? = nil,
                end: String? = nil, minimum: Int64? = nil, maximum: Int64? = nil,
                next: Bool = false, loadSources: Bool = false) async {
        guard !Task.isCancelled, !busy else { return }
        let token = UUID(); revision = token
        let after = next ? nextCursor : nil
        busy = true; didLoad = false; message = "Загрузка архива…"
        if !next { rows = []; sources = []; nextCursor = nil }
        defer { if revision == token { busy = false } }
        do {
            let page = try await client.archiveSweepPage(sessionID: session, streamID: stream, start: start,
                end: end, minimumHz: minimum, maximumHz: maximum, after: after)
            try Task.checkCancellation()
            guard revision == token else { return }
            var originals: [PlatformSweep] = []
            if loadSources {
                // Bounded page and sequential requests avoid monopolizing the measurement service.
                for row in page.items {
                    try Task.checkCancellation()
                    guard revision == token else { return }
                    originals.append(try await client.archiveOriginalSweep(sessionID: session, streamID: stream, sweepID: row.id))
                }
            }
            try Task.checkCancellation()
            guard revision == token else { return }
            didLoad = true
            rows = page.items
            sources = originals.sorted { $0.interval.startedAt < $1.interval.startedAt }
            nextCursor = page.nextCursor
            message = "\(rows.count) свипов в странице" + (nextCursor == nil ? " · конец выборки" : " · доступна следующая страница")
        } catch is CancellationError {
            if revision == token { message = "Загрузка отменена" }
        } catch {
            guard revision == token else { return }
            message = "Архив недоступен: \(error.localizedDescription). Текущий приём продолжается."
        }
    }
}

enum NativeTimeWindow {
    static func range(end: Date, seconds: Double) -> ClosedRange<Date> {
        let duration = seconds.isFinite ? min(3600, max(1, seconds)) : 120
        return end.addingTimeInterval(-duration)...end
    }
    static func rows(_ sweeps: [PlatformSweep], within window: ClosedRange<Date>) -> [PlatformSweep] {
        sweeps.filter {
            guard let start = NativeSpectrumTimeline.date($0.interval.startedAt),
                  let end = NativeSpectrumTimeline.date($0.interval.endedAt) else { return false }
            return start <= window.upperBound && end >= window.lowerBound
        }
    }
    static func iso(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
