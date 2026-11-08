import Foundation

private struct DvbKey: CodingKey {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func dvbExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: DvbKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected DVB-T fields"))
    }
}

/// One DVB-T parameter evaluation: verdicts on entered numbers, never a measurement.
public struct PlatformDvbEvaluation: Decodable, Equatable {
    public static let guardIntervals = ["1/32", "1/16", "1/8", "1/4"]
    public static let codeRates = ["1/2", "2/3", "3/4", "5/6", "7/8"]
    public static let parameterFields = ["mer_db", "ber_after_viterbi", "ber_after_rs"]
    public static let thresholdFields = ["min_mer_db", "max_ber_after_viterbi", "max_ber_after_rs"]
    public let sessionId: String
    public let methodology: String
    public let resultCode: String
    public let guardInterval: String
    public let codeRate: String
    public let parameters: [String: Double]
    public let thresholds: [String: Double]
    public let verdicts: [String: Bool]
    public let values: [String: Double]
    private enum CodingKeys: String, CodingKey {
        case sessionId, methodology, resultCode, guardInterval, codeRate
        case parameters, thresholds, verdicts, values
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        methodology = try v.decode(String.self, forKey: .methodology)
        resultCode = try v.decode(String.self, forKey: .resultCode)
        guardInterval = try v.decode(String.self, forKey: .guardInterval)
        codeRate = try v.decode(String.self, forKey: .codeRate)
        parameters = try v.decode([String: Double].self, forKey: .parameters)
        thresholds = try v.decode([String: Double].self, forKey: .thresholds)
        verdicts = try v.decode([String: Bool].self, forKey: .verdicts)
        values = try v.decode([String: Double].self, forKey: .values)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .values, in: v, debugDescription: "Некорректный ответ DVB-T")
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              methodology == "dvb_t_parameters",
              resultCode == "norm" || resultCode == "not_norm",
              Self.guardIntervals.contains(guardInterval),
              Self.codeRates.contains(codeRate),
              Set(parameters.keys) == Set(Self.parameterFields),
              Set(thresholds.keys) == Set(Self.thresholdFields),
              parameters.values.allSatisfy(\.isFinite),
              thresholds.values.allSatisfy(\.isFinite),
              Set(verdicts.keys) == Set(Self.parameterFields),
              verdicts.values.allSatisfy({ $0 == true || $0 == false }),
              Set(values.keys) == Set(Self.parameterFields).union(["guard_interval_ratio", "code_rate_ratio"]),
              values.values.allSatisfy(\.isFinite) else { throw invalid() }
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return a == b }
        return Data(a.utf8) == Data(b.utf8)
    }
}

/// Client-side request body; the core owns the final validation.
public struct PlatformDvbEvaluationRequest: Encodable {
    public let parameters: [String: Double]
    public let thresholds: [String: Double]
    public let guardInterval: String
    public let codeRate: String
    public init(parameters: [String: Double], thresholds: [String: Double],
                guardInterval: String, codeRate: String) {
        self.parameters = parameters; self.thresholds = thresholds
        self.guardInterval = guardInterval; self.codeRate = codeRate
    }
}

extension PlatformClient {
    /// POST is explicitly read-only: no measurement, no demodulation, no writes.
    public func dvbEvaluation(sessionID: String, _ request: PlatformDvbEvaluationRequest) async throws -> PlatformDvbEvaluation {
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              Set(request.parameters.keys) == Set(PlatformDvbEvaluation.parameterFields),
              Set(request.thresholds.keys) == Set(PlatformDvbEvaluation.thresholdFields),
              request.parameters.values.allSatisfy(\.isFinite),
              request.thresholds.values.allSatisfy(\.isFinite),
              PlatformDvbEvaluation.guardIntervals.contains(request.guardInterval),
              PlatformDvbEvaluation.codeRates.contains(request.codeRate) else {
            throw PlatformError.invalidCommand("Некорректный запрос DVB-T")
        }
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(request).count <= 8192 else {
            throw PlatformError.invalidCommand("Запрос превышает 8192 байт")
        }
        try Task.checkCancellation()
        let result: PlatformDvbEvaluation = try await post(
            "/sessions/\(Self.segment(sessionID))/dvb-evaluation", body: request,
            expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformDvbEvaluation.sameID(result.sessionId, sessionID),
              result.guardInterval == request.guardInterval,
              result.codeRate == request.codeRate,
              result.parameters == request.parameters,
              result.thresholds == request.thresholds else {
            throw PlatformError.invalidResponse("Сервер вернул другую оценку DVB-T")
        }
        return result
    }
}
