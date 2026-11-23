import Foundation

public struct PlatformRFExplorerPassSummary: Decodable, Equatable, Identifiable, Sendable {
    public var id: PlatformExactID { PlatformExactID(passId) }
    public let passId: String
    public let pass, tiles, receivedTiles, uniquePoints, expectedPoints: Int
    public let startHz, stopHz, stepHz: Int64
    public let startedAt, endedAt: String
    public let complete, simulated: Bool
    public let durationS, revisitS: Double?
    public let activeBoard, activeInput, detector: String
    public let rbwHz: Int
    public let offsetDb: Double

    fileprivate var valid: Bool {
        !passId.isEmpty && pass > 0 && tiles > 0 && (1...tiles).contains(receivedTiles)
            && startHz >= 0 && stopHz > startHz && stopHz <= 2_700_000_000 && stepHz > 0
            && (stopHz - startHz).isMultiple(of: stepHz)
            && expectedPoints == (stopHz - startHz) / stepHz + 1
            && (1...expectedPoints).contains(uniquePoints)
            && (!complete || (receivedTiles == tiles && uniquePoints == expectedPoints))
            && [durationS, revisitS].compactMap { $0 }.allSatisfy { $0.isFinite && $0 >= 0 }
            && validSurveyInterval(startedAt, endedAt)
    }
}

public struct PlatformRFExplorerPassPage: Decodable, Sendable {
    public let items: [PlatformRFExplorerPassSummary]
    public let nextBefore: String?
}

public struct PlatformRFExplorerPoint: Decodable, Equatable, Sendable {
    public let frequencyHz: Int64
    public let levelDbm: Double
    public let sweepId, startedAt, endedAt, lastMeasuredAt: String
    public let pointIndex, tile: Int

    /// A held peak can belong to an earlier pass. Use its original interval and exact index.
    public func validate(source: PlatformSweep) throws {
        guard PlatformExactID.same(sweepId, source.id), source.levelUnit == "dBm",
              source.frequenciesHz.indices.contains(pointIndex), source.frequenciesHz[pointIndex] == frequencyHz,
              source.levels[pointIndex] == levelDbm,
              try PlatformTimestamp(startedAt) == PlatformTimestamp(source.interval.startedAt),
              try PlatformTimestamp(endedAt) == PlatformTimestamp(source.interval.endedAt) else {
            throw PlatformError.invalidResponse("Архивный свип не содержит выбранный исходный отсчёт.")
        }
    }
}

public struct PlatformRFExplorerMarker: Decodable, Sendable {
    public let current, held: PlatformRFExplorerPoint?

    private enum CodingKeys: String, CodingKey { case current, held }
    public init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        guard fields.contains(.current), fields.contains(.held) else {
            throw PlatformError.invalidResponse("В ответе точного маркера отсутствует источник.")
        }
        current = try fields.decodeIfPresent(PlatformRFExplorerPoint.self, forKey: .current)
        held = try fields.decodeIfPresent(PlatformRFExplorerPoint.self, forKey: .held)
    }
}

public struct PlatformRFExplorerCoverage: Decodable, Equatable, Sendable {
    public let startHz, stopHz: Int64
}

public struct PlatformRFExplorerPanorama: Decodable, Sendable {
    public let summary: PlatformRFExplorerPassSummary
    public let columns: [PlatformRFExplorerPoint?]
    public let holdColumns: [PlatformRFExplorerPoint?]?
    public let coverage: [PlatformRFExplorerCoverage]
    public let holdCoverage: [PlatformRFExplorerCoverage]?

    /// Inclusive, unmeasured grid points. Narrow gaps can exist within a single display column.
    public var gaps: [ClosedRange<Int64>] {
        var missing: [ClosedRange<Int64>] = []
        var next = summary.startHz
        for segment in coverage {
            if segment.startHz > next { missing.append(next...(segment.startHz - summary.stepHz)) }
            next = segment.stopHz + summary.stepHz
        }
        if next <= summary.stopHz { missing.append(next...summary.stopHz) }
        return missing
    }

    /// Pick an actual display-column peak within the same measured segment. A hole has no sample.
    public func point(at frequency: Int64, held: Bool) -> PlatformRFExplorerPoint? {
        let spans = held ? holdCoverage ?? [] : coverage
        guard let span = spans.first(where: { $0.startHz <= frequency && frequency <= $0.stopHz }) else { return nil }
        return (held ? holdColumns ?? [] : columns).compactMap { $0 }
            .filter { span.startHz <= $0.frequencyHz && $0.frequencyHz <= span.stopHz }
            .min { abs($0.frequencyHz - frequency) < abs($1.frequencyHz - frequency) }
    }

    fileprivate func valid(columns requested: Int, hold: Bool) -> Bool {
        func onGrid(_ hz: Int64) -> Bool {
            hz >= summary.startHz && hz <= summary.stopHz && (hz - summary.startHz).isMultiple(of: summary.stepHz)
        }
        func validTrace(_ points: [PlatformRFExplorerPoint?], _ spans: [PlatformRFExplorerCoverage]) -> Bool {
            guard !points.isEmpty, points.count <= requested,
                  spans.allSatisfy({ onGrid($0.startHz) && onGrid($0.stopHz) && $0.startHz <= $0.stopHz }),
                  zip(spans, spans.dropFirst()).allSatisfy({ $0.stopHz < $1.startHz }) else { return false }
            var segment = 0
            for point in points.compactMap({ $0 }) {
                while segment < spans.count, spans[segment].stopHz < point.frequencyHz { segment += 1 }
                guard segment < spans.count, spans[segment].startHz <= point.frequencyHz,
                      onGrid(point.frequencyHz), point.levelDbm.isFinite, !point.sweepId.isEmpty,
                      point.pointIndex >= 0, (1...summary.tiles).contains(point.tile),
                      validSurveyInterval(point.startedAt, point.endedAt),
                      (try? PlatformTimestamp(point.lastMeasuredAt)) != nil else { return false }
            }
            return true
        }
        guard summary.valid, validTrace(columns, coverage) else { return false }
        switch (holdColumns, holdCoverage) {
        case let (points?, spans?): return hold && points.count == columns.count && validTrace(points, spans)
        case (nil, nil): return !hold
        default: return false
        }
    }
}

public struct PlatformRFExplorerWindow: Decodable, Equatable, Sendable {
    public let tile: Int
    public let sweepId, startedAt, endedAt: String

    public func seconds(until next: Self) throws -> Double {
        let elapsed = try PlatformTimestamp(next.startedAt).utcMicroseconds - PlatformTimestamp(startedAt).utcMicroseconds
        return max(0, Double(elapsed) / 1_000_000)
    }
}

public struct PlatformRFExplorerWindowPage: Decodable, Sendable {
    public let items: [PlatformRFExplorerWindow]
    public let nextAfterTile: Int?
}

private func validSurveyInterval(_ start: String, _ end: String) -> Bool {
    guard let first = try? PlatformTimestamp(start), let last = try? PlatformTimestamp(end) else { return false }
    return first <= last
}

extension PlatformRFExplorerConfiguration {
    public static let panoramaFeature = "rf_explorer_survey_panorama"

    /// Planning uses the 112-point survey profile. The committed pass supplies its actual tile count.
    public var plannedSurveyTiles: Int? {
        guard let startHz, let stopHz, surveyStepHz != nil else { return nil }
        return try? Self.plannedSurveyTiles(startHz: startHz, stopHz: stopHz)
    }

    public static func plannedSurveyTiles(startHz: Int, stopHz: Int) throws -> Int {
        try validateBounds(startHz: startHz, stopHz: stopHz, surveyStepHz: surveySpacingHz)
        let points = (stopHz - startHz) / surveySpacingHz + 1
        return (points + 111) / 112
    }
}

extension PlatformClient {
    private func surveyPath(_ session: String, _ stream: String) -> String {
        "/sessions/\(Self.segment(session))/streams/\(Self.segment(stream))/rf-explorer/passes"
    }

    public func rfExplorerPasses(sessionID: String, streamID: String, before: String? = nil) async throws -> PlatformRFExplorerPassPage {
        var query = [URLQueryItem(name: "limit", value: "12")]
        if let before { query.append(.init(name: "before", value: before)) }
        let page: PlatformRFExplorerPassPage = try await get(surveyPath(sessionID, streamID), query: query)
        guard page.items.count <= 12, page.items.allSatisfy(\.valid),
              Set(page.items.map { PlatformExactID($0.passId) }).count == page.items.count else {
            throw PlatformError.invalidResponse("Некорректная страница проходов RF Explorer.")
        }
        return page
    }

    public func rfExplorerPass(sessionID: String, streamID: String, passID: String, columns: Int,
                               holdSince: String? = nil) async throws -> PlatformRFExplorerPanorama {
        guard (1...2048).contains(columns) else { throw PlatformError.invalidCommand("Нужно 1…2048 колонок панорамы.") }
        var query = [URLQueryItem(name: "columns", value: String(columns))]
        if let holdSince {
            _ = try PlatformTimestamp(holdSince)
            query.append(.init(name: "hold_since", value: holdSince))
        }
        let path = surveyPath(sessionID, streamID) + "/" + Self.segment(passID)
        let result: PlatformRFExplorerPanorama = try await get(path, query: query)
        guard PlatformExactID.same(passID, result.summary.passId), result.valid(columns: columns, hold: holdSince != nil) else {
            throw PlatformError.invalidResponse("Панорама не соответствует выбранному проходу или частотной сетке.")
        }
        return result
    }

    public func rfExplorerMarker(sessionID: String, streamID: String, passID: String, frequencyHz: Int64,
                                 holdSince: String? = nil) async throws -> PlatformRFExplorerMarker {
        guard (0...9_007_199_254_740_991).contains(frequencyHz) else {
            throw PlatformError.invalidCommand("Для маркера нужна допустимая частота в целых Гц.")
        }
        var query = [URLQueryItem(name: "frequency_hz", value: String(frequencyHz))]
        if let holdSince {
            _ = try PlatformTimestamp(holdSince)
            query.append(.init(name: "hold_since", value: holdSince))
        }
        let path = surveyPath(sessionID, streamID) + "/" + Self.segment(passID) + "/point"
        let result: PlatformRFExplorerMarker = try await get(path, query: query)
        guard holdSince != nil || result.held == nil,
              [result.current, result.held].compactMap({ $0 }).allSatisfy({
                  $0.frequencyHz == frequencyHz && !$0.sweepId.isEmpty && $0.pointIndex >= 0 && $0.tile > 0
                      && $0.levelDbm.isFinite && validSurveyInterval($0.startedAt, $0.endedAt)
                      && (try? PlatformTimestamp($0.lastMeasuredAt)) != nil
              }) else {
            throw PlatformError.invalidResponse("Исходник не соответствует точной частоте маркера.")
        }
        return result
    }

    public func rfExplorerWorkbenchAccess() async throws -> (features: Set<String>, operatorActions: Bool) {
        let features = try await workbenchFeatures()
        guard hasBearerCredential else { return (features, true) }
        let status = try await securityStatus()
        return (features, !status.pairingRequired && (status.accessScope == nil || status.accessScope == "operator"))
    }

    public func rfExplorerWindows(sessionID: String, streamID: String, passID: String,
                                  afterTile: Int) async throws -> PlatformRFExplorerWindowPage {
        guard afterTile >= 0 else { throw PlatformError.invalidCommand("Недопустимый номер окна.") }
        let page: PlatformRFExplorerWindowPage = try await get(surveyPath(sessionID, streamID) + "/" + Self.segment(passID) + "/windows",
            query: [.init(name: "after_tile", value: String(afterTile)), .init(name: "limit", value: "256")])
        guard page.items.count <= 256,
              page.items.allSatisfy({ $0.tile > afterTile && !$0.sweepId.isEmpty && validSurveyInterval($0.startedAt, $0.endedAt) }),
              zip(page.items, page.items.dropFirst()).allSatisfy({ $0.tile < $1.tile }),
              page.nextAfterTile == nil || (page.nextAfterTile == page.items.last?.tile && !page.items.isEmpty) else {
            throw PlatformError.invalidResponse("Некорректная страница исходных окон RF Explorer.")
        }
        return page
    }
}
