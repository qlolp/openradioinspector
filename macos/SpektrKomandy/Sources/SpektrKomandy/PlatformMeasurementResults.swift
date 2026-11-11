import Foundation

private struct ResultKey: CodingKey, Sendable {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func resultExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: ResultKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected measurement result fields"))
    }
}

/// The methodology catalog: ids, titles, original tables and exact value fields.
public struct PlatformMethodologyEntry: Decodable, Equatable, Sendable {
    public let methodology: String
    public let title: String
    public let originalTable: String
    public let unit: String
    public let fields: [String]
    public let resultCodes: [String]
}

/// One archived measurement result: the methodology is explicit, the verdict
/// code is stored apart from the numeric values.
public struct PlatformMeasurementResult: Decodable, Equatable, Sendable {
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public static let resultCodes = ["norm", "not_norm", "not_measured"]
    public let id: String
    public let sessionId: String
    public let streamId: String
    public let methodology: String
    public let resultCode: String
    public let frequencyHz: Int64
    public let interval: PlatformInterval
    public let values: [String: Double]
    public let createdAt: String
    private enum CodingKeys: String, CodingKey, Sendable {
        case id, sessionId, streamId, methodology, resultCode, frequencyHz
        case interval, values, createdAt
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        id = try v.decode(String.self, forKey: .id)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        methodology = try v.decode(String.self, forKey: .methodology)
        resultCode = try v.decode(String.self, forKey: .resultCode)
        frequencyHz = try v.decode(Int64.self, forKey: .frequencyHz)
        interval = try v.decode(PlatformInterval.self, forKey: .interval)
        values = try v.decode([String: Double].self, forKey: .values)
        createdAt = try v.decode(String.self, forKey: .createdAt)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .values, in: v, debugDescription: "Некорректный результат измерения")
        }
        guard !id.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              Self.resultCodes.contains(resultCode),
              (0...Self.maximumFrequencyHz).contains(frequencyHz),
              values.values.allSatisfy(\.isFinite),
              resultCode == "not_measured" ? values.isEmpty : !values.isEmpty else { throw invalid() }
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return a == b }
        return Data(a.utf8) == Data(b.utf8)
    }
}

/// Client-side request body; the core owns the final validation.
public struct PlatformMeasurementResultDraft: Encodable, Sendable {
    public let methodology: String
    public let resultCode: String
    public let frequencyHz: Int64
    public let interval: PlatformInterval
    public let values: [String: Double]
    public let streamId: String
    public init(methodology: String, resultCode: String, frequencyHz: Int64,
                interval: PlatformInterval, values: [String: Double], streamId: String) {
        self.methodology = methodology; self.resultCode = resultCode
        self.frequencyHz = frequencyHz; self.interval = interval
        self.values = values; self.streamId = streamId
    }
}

extension PlatformClient {
    public func measurementCatalog() async throws -> [PlatformMethodologyEntry] {
        try await get("/measurement-catalog", expectedStatuses: [200])
    }
    public func listMeasurementResults(sessionID: String, methodology: String? = nil) async throws -> [PlatformMeasurementResult] {
        var query: [URLQueryItem] = []
        if let methodology { query.append(.init(name: "methodology", value: methodology)) }
        return try await get("/sessions/\(Self.segment(sessionID))/measurement-results",
                             query: query, expectedStatuses: [200])
    }
    /// POST creates one derived record; the archived source measurements stay unchanged.
    public func saveMeasurementResult(sessionID: String, _ draft: PlatformMeasurementResultDraft) async throws -> PlatformMeasurementResult {
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !draft.streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              PlatformMeasurementResult.resultCodes.contains(draft.resultCode),
              (0...PlatformMeasurementResult.maximumFrequencyHz).contains(draft.frequencyHz),
              draft.values.values.allSatisfy(\.isFinite),
              draft.resultCode == "not_measured" ? draft.values.isEmpty : !draft.values.isEmpty else {
            throw PlatformError.invalidCommand("Некорректный черновик результата измерения")
        }
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(draft).count <= 65_536 else {
            throw PlatformError.invalidCommand("Запрос превышает 65 536 байт")
        }
        try Task.checkCancellation()
        let result: PlatformMeasurementResult = try await post(
            "/sessions/\(Self.segment(sessionID))/measurement-results", body: draft,
            expectedStatuses: [201], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformMeasurementResult.sameID(result.sessionId, sessionID),
              PlatformMeasurementResult.sameID(result.streamId, draft.streamId),
              result.methodology == draft.methodology,
              result.resultCode == draft.resultCode,
              result.frequencyHz == draft.frequencyHz else {
            throw PlatformError.invalidResponse("Сервер вернул другой результат измерения")
        }
        return result
    }
}
