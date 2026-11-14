import Foundation

public struct PlatformFieldStrengthRequest: Encodable, Sendable {
    public let sweepId: String
    public let frequencyHz: Int64
    public let calibration: MonitoringValue
    public init(sweepId: String, frequencyHz: Int64, calibration: MonitoringValue) {
        self.sweepId = sweepId; self.frequencyHz = frequencyHz; self.calibration = calibration
    }
    public static func calibration(from text: String) throws -> MonitoringValue {
        let data = Data(text.utf8)
        guard data.count <= 65536 else { throw PlatformError.invalidCommand("Калибровка больше 64 КиБ") }
        var scanner = MaskJSONScanner(bytes: Array(data)); try scanner.check()
        return try JSONDecoder().decode(MonitoringValue.self, from: data)
    }
}

public struct PlatformFieldStrengthResult: Decodable, Sendable {
    public struct Source: Decodable, Sendable {
        public let id: String
        public let sessionId: String
        public let streamId: String
        public let frequencyHz: Int64
    }
    public struct Calibration: Decodable, Sendable {
        public let profile: MonitoringValue
        public let sha256: String
    }
    public let value: Double
    public let valueDbuvPerM: Double
    public let uncertaintyDb: Double?
    public let source: Source
    public let calibration: Calibration
    private enum CodingKeys: String, CodingKey {
        case value, valueDbuvPerM, uncertaintyDb, source, calibration, methodology, unit
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        value = try v.decode(Double.self, forKey: .value)
        valueDbuvPerM = try v.decode(Double.self, forKey: .valueDbuvPerM)
        uncertaintyDb = try v.decodeIfPresent(Double.self, forKey: .uncertaintyDb)
        source = try v.decode(Source.self, forKey: .source)
        calibration = try v.decode(Calibration.self, forKey: .calibration)
        guard try v.decode(String.self, forKey: .methodology) == "field_strength_rms_v2",
              try v.decode(String.self, forKey: .unit) == "V/m",
              value.isFinite, value > 0, valueDbuvPerM.isFinite,
              v.contains(.uncertaintyDb),
              uncertaintyDb.map({ $0.isFinite && $0 >= 0 }) ?? true,
              calibration.sha256.utf8.count == 64,
              calibration.sha256.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else {
            throw PlatformError.invalidResponse("Некорректная RMS-оценка поля")
        }
    }
    public func validate(sessionID: String, streamID: String, request: PlatformFieldStrengthRequest) throws {
        func same(_ a: String, _ b: String) -> Bool { a.utf8.elementsEqual(b.utf8) }
        guard same(source.sessionId, sessionID), same(source.streamId, streamID),
              same(source.id, request.sweepId), source.frequencyHz == request.frequencyHz,
              calibration.profile == request.calibration else {
            throw PlatformError.invalidResponse("Ответ другого источника или калибровочного профиля")
        }
    }
    public var summary: String {
        "\(value) V/m (\(valueDbuvPerM) dBuV/m) · "
            + (uncertaintyDb.map { "погрешность \($0) dB" } ?? "погрешность неизвестна")
    }
}

extension PlatformClient {
    public func fieldStrengthRMSV2(sessionID: String, streamID: String,
                                  request: PlatformFieldStrengthRequest) async throws -> PlatformFieldStrengthResult {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard !sessionID.isEmpty, !request.sweepId.isEmpty, request.sweepId.utf8.count <= 2048,
              (0...9_007_199_254_740_991).contains(request.frequencyHz),
              try encoder.encode(request).count <= 65536 else {
            throw PlatformError.invalidCommand("Неверный запрос или калибровка больше 64 КиБ")
        }
        let result: PlatformFieldStrengthResult = try await post(
            "/sessions/\(Self.segment(sessionID))/field-strength-rms-v2", body: request,
            expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        try result.validate(sessionID: sessionID, streamID: streamID, request: request)
        return result
    }
}
