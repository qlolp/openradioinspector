import Foundation

private struct CounterKey: CodingKey, Sendable {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func counterExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: CounterKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected frequency counter fields"))
    }
}

/// One built-in frequency counter measurement on an archived sweep.
///
/// The estimate is parabolic only on a uniform raster with an interior peak;
/// otherwise the peak bin itself is the measurement. ``uncertaintyHz`` is the
/// honest bound ``max(resolution, raster) / 2``: the estimate never claims
/// better than both the requested resolution and the recorded raster.
public struct PlatformFrequencyCounterResult: Decodable, Equatable, Sendable {
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public let sessionId: String
    public let streamId: String
    public let sweepId: String
    public let windowStartHz: Int64
    public let windowStopHz: Int64
    public let resolutionHz: Int64
    public let rasterHz: Int64
    public let method: String
    public let peakIndex: Int
    public let peakFrequencyHz: Int64
    public let peakLevelDbm: Double
    public let measuredFrequencyHz: Double
    public let uncertaintyHz: Double
    public let values: [String: Double]
    private enum CodingKeys: String, CodingKey, Sendable {
        case sessionId, streamId, sweep, window, resolutionHz, rasterHz, method
        case peakIndex, peakFrequencyHz, peakLevelDbm, measuredFrequencyHz, uncertaintyHz
        case measurementMethodology, values
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        let sweepObject = try v.decode(SweepIdentity.self, forKey: .sweep)
        sweepId = sweepObject.id
        let window = try v.decode(WindowBounds.self, forKey: .window)
        windowStartHz = window.startHz
        windowStopHz = window.stopHz
        resolutionHz = try v.decode(Int64.self, forKey: .resolutionHz)
        rasterHz = try v.decode(Int64.self, forKey: .rasterHz)
        method = try v.decode(String.self, forKey: .method)
        peakIndex = try v.decode(Int.self, forKey: .peakIndex)
        peakFrequencyHz = try v.decode(Int64.self, forKey: .peakFrequencyHz)
        peakLevelDbm = try v.decode(Double.self, forKey: .peakLevelDbm)
        measuredFrequencyHz = try v.decode(Double.self, forKey: .measuredFrequencyHz)
        uncertaintyHz = try v.decode(Double.self, forKey: .uncertaintyHz)
        let methodology = try v.decode(String.self, forKey: .measurementMethodology)
        values = try v.decode([String: Double].self, forKey: .values)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .values, in: v, debugDescription: "Некорректный результат частотомера")
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !sweepId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              (0...Self.maximumFrequencyHz).contains(windowStartHz),
              (0...Self.maximumFrequencyHz).contains(windowStopHz),
              windowStartHz < windowStopHz,
              (1...Self.maximumFrequencyHz).contains(resolutionHz),
              rasterHz >= 0,
              method == "parabolic" || method == "peak_bin",
              peakIndex >= 0,
              (0...Self.maximumFrequencyHz).contains(peakFrequencyHz),
              peakLevelDbm.isFinite, measuredFrequencyHz.isFinite, uncertaintyHz.isFinite,
              Double(windowStartHz) <= measuredFrequencyHz, measuredFrequencyHz <= Double(windowStopHz),
              uncertaintyHz >= 0,
              methodology == "frequency_counter",
              Set(values.keys) == ["measured_frequency_hz", "resolution_hz", "uncertainty_hz"],
              values.values.allSatisfy(\.isFinite) else { throw invalid() }
    }
    private struct SweepIdentity: Decodable, Sendable {
        let id: String
    }
    private struct WindowBounds: Decodable, Sendable {
        let startHz: Int64
        let stopHz: Int64
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return a == b }
        return Data(a.utf8) == Data(b.utf8)
    }
}

/// Client-side request body; the core owns the final validation.
public struct PlatformFrequencyCounterRequest: Encodable, Sendable {
    public let sweepId: String
    public let resolutionHz: Int64
    public let startHz: Int64
    public let stopHz: Int64
    public init(sweepId: String, resolutionHz: Int64, startHz: Int64, stopHz: Int64) {
        self.sweepId = sweepId; self.resolutionHz = resolutionHz
        self.startHz = startHz; self.stopHz = stopHz
    }
}

extension PlatformClient {
    /// POST is explicitly read-only: the archived bins are never rewritten.
    public func frequencyCounter(sessionID: String, _ request: PlatformFrequencyCounterRequest) async throws -> PlatformFrequencyCounterResult {
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !request.sweepId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              request.sweepId.unicodeScalars.count <= 128,
              (1...PlatformFrequencyCounterResult.maximumFrequencyHz).contains(request.resolutionHz),
              (0...PlatformFrequencyCounterResult.maximumFrequencyHz).contains(request.startHz),
              (0...PlatformFrequencyCounterResult.maximumFrequencyHz).contains(request.stopHz),
              request.startHz < request.stopHz else {
            throw PlatformError.invalidCommand("Некорректный запрос частотомера")
        }
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(request).count <= 8192 else {
            throw PlatformError.invalidCommand("Запрос превышает 8192 байт")
        }
        try Task.checkCancellation()
        let result: PlatformFrequencyCounterResult = try await post(
            "/sessions/\(Self.segment(sessionID))/frequency-counter", body: request,
            expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
            })
        try Task.checkCancellation()
        guard PlatformFrequencyCounterResult.sameID(result.sessionId, sessionID),
              PlatformFrequencyCounterResult.sameID(result.sweepId, request.sweepId),
              result.resolutionHz == request.resolutionHz,
              result.windowStartHz == request.startHz, result.windowStopHz == request.stopHz else {
            throw PlatformError.invalidResponse("Сервер вернул другое измерение частотомера")
        }
        return result
    }
}
