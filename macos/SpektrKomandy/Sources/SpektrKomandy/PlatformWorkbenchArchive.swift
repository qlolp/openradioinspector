import Foundation

/// Metadata pages have a server-owned snapshot cursor; source arrays are fetched only on demand.
public struct PlatformArchiveSweepRow: Decodable, Identifiable, Sendable {
    public let id: String
    public let sessionId: String
    public let streamId: String
    public let interval: PlatformInterval
    public let levelUnit: String
    public let pointCount: Int
    public let minFrequencyHz: Int64
    public let maxFrequencyHz: Int64
    public let peakFrequencyHz: Int64
    public let peakLevel: Double
}

public struct PlatformArchiveSweepPage: Decodable, Sendable {
    public let items: [PlatformArchiveSweepRow]
    public let nextCursor: String?
}

extension PlatformSweep {
    /// The context endpoint returns the complete, validated original anchor, with no resampling.
    init(context: PlatformSweepContext) throws {
        guard let item = context.items.first(where: { PlatformExactID.same($0.id, context.anchorId) }) else {
            throw PlatformError.invalidResponse("В архивном ответе нет исходного свипа")
        }
        id = item.id; sessionId = context.sessionId; streamId = context.streamId
        interval = item.interval; frequenciesHz = context.frequenciesHz
        levels = item.levels; levelUnit = item.levelUnit; settings = item.settings
    }
}

extension PlatformClient {
    public func archiveSweepPage(sessionID: String, streamID: String, start: String? = nil,
                                 end: String? = nil, minimumHz: Int64? = nil, maximumHz: Int64? = nil,
                                 after: String? = nil) async throws -> PlatformArchiveSweepPage {
        var query = [URLQueryItem(name: "stream_id", value: streamID), URLQueryItem(name: "limit", value: "32")]
        for (name, value) in [("start", start), ("end", end), ("min_hz", minimumHz.map(String.init)),
                              ("max_hz", maximumHz.map(String.init)), ("after", after)] {
            if let value { query.append(URLQueryItem(name: name, value: value)) }
        }
        let page: PlatformArchiveSweepPage = try await get("/sessions/\(Self.segment(sessionID))/sweep-summaries", query: query)
        guard page.items.count <= 32, Set(page.items.map { PlatformExactID($0.id) }).count == page.items.count, page.items.allSatisfy({
            PlatformExactID.same($0.sessionId, sessionID) && PlatformExactID.same($0.streamId, streamID)
                && $0.pointCount > 0 && $0.peakLevel.isFinite && $0.minFrequencyHz >= 0
                && $0.minFrequencyHz <= $0.peakFrequencyHz && $0.peakFrequencyHz <= $0.maxFrequencyHz
        }) else { throw PlatformError.invalidResponse("Архив вернул данные другого потока или неверную страницу") }
        return page
    }

    public func archiveOriginalSweep(sessionID: String, streamID: String, sweepID: String) async throws -> PlatformSweep {
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !sweepID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              sweepID.unicodeScalars.count <= 128 else {
            throw PlatformError.invalidCommand("Нужны ID сеанса и архивного свипа до 128 символов")
        }
        let source: PlatformSweep
        do {
            source = try await get("/sessions/\(Self.segment(sessionID))/sweeps/\(Self.segment(sweepID))",
                                   validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        } catch PlatformError.http(status: 404, code: "not_found", message: _) {
            // Legacy servers expose original sweeps through the context route.
            let context = try await sweepContext(sessionID: sessionID, sweepID: sweepID, before: 0, after: 0)
            source = try PlatformSweep(context: context)
        }
        guard PlatformExactID.same(source.sessionId, sessionID), PlatformExactID.same(source.streamId, streamID),
              PlatformExactID.same(source.id, sweepID), source.levelUnit == "dBm",
              averageInterval(source.interval) != nil else {
            throw PlatformError.invalidResponse("Архивный исходник не соответствует выбранному потоку")
        }
        return source
    }
}
