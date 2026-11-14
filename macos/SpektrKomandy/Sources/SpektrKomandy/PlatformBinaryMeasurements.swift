import Foundation
import ORIContracts
import ORIMeasurementStream

public enum PlatformMeasurementTransport: Equatable, Sendable {
    case serverSentEvents, binaryV1
    public var label: String { self == .binaryV1 ? "Protobuf" : "SSE" }

    public static func negotiate(features: JSONObject) throws -> Self {
        guard let advertised = features["measurement_binary_stream"], advertised != .null, advertised != .bool(false) else { return .serverSentEvents }
        guard case let .object(value) = advertised else { throw PlatformError.invalidResponse("Некорректная возможность бинарного потока") }
        let required = ["version", "transport", "encoding", "path", "subprotocol", "max_frame_bytes"]
        guard required.allSatisfy({ value[$0] != nil }),
              case let .integer(version)? = value["version"],
              case let .integer(maximum)? = value["max_frame_bytes"],
              case let .string(transport)? = value["transport"],
              case let .string(encoding)? = value["encoding"],
              case let .string(path)? = value["path"],
              case let .string(subprotocolName)? = value["subprotocol"] else {
            throw PlatformError.invalidResponse("Неполное описание бинарного потока")
        }
        guard value.count == required.count, version == "1", maximum == String(MeasurementStream.maxFrameBytes),
              transport == "websocket", encoding == "protobuf", path == MeasurementStream.path,
              subprotocolName == MeasurementStream.subprotocolName else { return .serverSentEvents }
        return .binaryV1
    }
}

/// Valid original data can exceed this native display's exact representations.
/// This is a source state, not a decoding error or permission to round values.
public enum PlatformMeasurementDisplayIssue: Error, Equatable, Sendable {
    case unsupportedCursor, unsupportedFrequency, unsupportedLevel, unsupportedSettings, unsupportedMeasurement
    public var message: String {
        switch self {
        case .unsupportedCursor: return "Курсор исходника вне диапазона native-графика. Исходное событие доступно без преобразований."
        case .unsupportedFrequency: return "Частоты исходника нельзя представить на этом графике точно. Откройте исходное событие."
        case .unsupportedLevel: return "Уровни исходника нельзя представить на этом графике точно. Откройте исходное событие."
        case .unsupportedSettings: return "Настройки исходника требуют точного представления. Откройте исходное событие."
        case .unsupportedMeasurement: return "Исходное событие принято; его формат не поддерживается этим графиком."
        }
    }
}

public struct PlatformMeasurementSource: Sendable {
    public enum Encoding: Equatable, Sendable { case protobuf, json }
    public let sequence: UInt64
    public let encoding: Encoding
    /// Entire original binary message, or the original SSE JSON document.
    public let originalData: Data
    /// Original JSON metadata; typed arrays remain in the Protobuf source above.
    public let metadataJSON: Data
    public let metadata: JSONObject
    public var suggestedFilename: String { "measurement-\(sequence)." + (encoding == .protobuf ? "protobuf" : "json") }
}

public struct PlatformMeasurementEvent: Sendable {
    public let sequence: UInt64
    /// A metadata-only DTO advances the Int64 cache for a source-only sweep.
    /// Beyond Int64, only the independent UInt64 applied/source cursor advances.
    public let committedEvent: PlatformCommittedEvent?
    public let displayIssue: PlatformMeasurementDisplayIssue?
    public let source: PlatformMeasurementSource

    public static func decodeBinary(_ bytes: Data) throws -> Self {
        let frame = try MeasurementStream.decode(bytes)
        let metadata = try frame.envelope.requireObject()
        let data = try required(metadata, "data").requireObject()
        let kind = try required(data, "event_type").requireString()
        let payload = try required(data, "payload").requireObject()
        let source = PlatformMeasurementSource(sequence: frame.sequence, encoding: .protobuf,
            originalData: bytes, metadataJSON: frame.envelopeJSON, metadata: metadata)
        guard let sequence = Int64(exactly: frame.sequence) else {
            return Self(sequence: frame.sequence, committedEvent: nil, displayIssue: .unsupportedCursor, source: source)
        }
        var sweep: PlatformSweep?, observation: PlatformObservation?, issue: PlatformMeasurementDisplayIssue?
        do {
            if kind == "sweep.received" {
                let raw = try required(payload, "sweep").requireObject()
                let frequencies: [Int64], levels: [Double]
                if let arrays = frame.spectrum {
                    frequencies = try arrays.frequenciesHz.map { value in
                        guard let exact = Int64(exactly: value) else { throw PlatformMeasurementDisplayIssue.unsupportedFrequency }; return exact
                    }
                    levels = arrays.levels // Owned typed f64 buffer; no JSON reconstruction.
                } else {
                    frequencies = try required(raw, "frequencies_hz").requireArray().map {
                        guard case let .integer(text) = $0, let exact = Int64(text), exact >= 0 else { throw PlatformMeasurementDisplayIssue.unsupportedFrequency }; return exact
                    }
                    levels = try required(raw, "levels").requireArray().map { try exactLevel($0) }
                }
                // The existing renderer's exact-frequency range is narrower
                // than Int64. Keep wider valid sources opaque instead of
                // rejecting them later in the consumer and withholding ACK.
                guard frequencies.allSatisfy({ $0 <= 9_007_199_254_740_991 }) else {
                    throw PlatformMeasurementDisplayIssue.unsupportedFrequency
                }
                let settings = try raw["settings"].flatMap { value -> [String: PlatformComparisonSetting]? in
                    if value == .null { return nil }; return try legacySettings(value.requireObject())
                }
                sweep = try PlatformSweep(measurementMetadata: raw, frequencies: frequencies, levels: levels, settings: settings)
            } else if kind == "observation.received" {
                observation = try PlatformObservation(measurementMetadata: required(payload, "observation").requireObject())
            }
        } catch let unsupported as PlatformMeasurementDisplayIssue { issue = unsupported }
        catch { issue = .unsupportedMeasurement }
        let session: String?
        if let value = data["session_id"], value != .null { session = try value.requireString() } else { session = nil }
        let event = PlatformCommittedEvent(sequence: sequence, eventType: kind,
            occurredAt: try required(data, "occurred_at").requireString(), sessionId: session,
            sweep: sweep, observation: observation)
        return Self(sequence: frame.sequence, committedEvent: event, displayIssue: issue, source: source)
    }

    static func serverSent(_ event: PlatformCommittedEvent) throws -> Self {
        guard event.sequence > 0, let original = event.sourceJSON else { throw PlatformEventError.malformedEvent }
        let metadata = try StrictJSON.parse(original).requireObject()
        let sequence = UInt64(event.sequence)
        return Self(sequence: sequence, committedEvent: event, displayIssue: nil,
            source: PlatformMeasurementSource(sequence: sequence, encoding: .json, originalData: original,
                metadataJSON: original, metadata: metadata))
    }
}

private func required(_ object: JSONObject, _ key: String) throws -> JSONValue {
    guard let value = object[key] else { throw PlatformMeasurementDisplayIssue.unsupportedMeasurement }; return value
}
private func exactLevel(_ value: JSONValue) throws -> Double {
    switch value {
    case let .number(number) where number.isFinite: return number
    case let .integer(text):
        if let value = Int64(text), let number = Double(exactly: value) { return number }
        if let value = UInt64(text), let number = Double(exactly: value) { return number }
        throw PlatformMeasurementDisplayIssue.unsupportedLevel
    default: throw PlatformMeasurementDisplayIssue.unsupportedLevel
    }
}
private func legacySettings(_ object: JSONObject) throws -> [String: PlatformComparisonSetting] {
    var result: [String: PlatformComparisonSetting] = [:]
    for (key, value) in object {
        // The display's legacy map uses Swift String identity. The raw source
        // remains JSONObject; never collapse distinct NFC/NFD setting names.
        guard result[key] == nil else { throw PlatformMeasurementDisplayIssue.unsupportedSettings }
        result[key] = try legacySetting(value)
    }
    return result
}
private func legacySetting(_ value: JSONValue) throws -> PlatformComparisonSetting {
    switch value {
    case .null: return .null
    case let .bool(value): return .boolean(value)
    case let .string(value): return .string(value)
    case let .number(value): return .number(value)
    case .integer:
        do { return .number(try exactLevel(value)) } catch { throw PlatformMeasurementDisplayIssue.unsupportedSettings }
    case let .array(values): return .array(try values.map(legacySetting))
    case let .object(values): return .object(try legacySettings(values))
    }
}

func measurementString(_ object: JSONObject, _ key: String) throws -> String { try required(object, key).requireString() }
func measurementInterval(_ metadata: JSONObject) throws -> PlatformInterval {
    let value = try required(metadata, "interval").requireObject()
    let startText = try measurementString(value, "started_at"), endText = try measurementString(value, "ended_at")
    let start = try UtcTimestamp(startText), end = try UtcTimestamp(endText)
    guard end >= start else { throw PlatformMeasurementDisplayIssue.unsupportedMeasurement }
    // Preserve already-UTC source spelling: scientific response validation
    // compares immutable interval fields, including omitted zero fractions.
    // Offset display adaptation is exact; its original remains in source bytes.
    func displayed(_ text: String, _ timestamp: UtcTimestamp) -> String {
        text.hasSuffix("Z") || text.hasSuffix("+00:00") ? text : timestamp.rawValue
    }
    return PlatformInterval(startedAt: displayed(startText, start), endedAt: displayed(endText, end))
}
func measurementLevel(_ value: JSONValue) throws -> Double { try exactLevel(value) }
