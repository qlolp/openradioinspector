import Foundation
import CoreFoundation

private struct MaskKey: CodingKey, Sendable {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func maskExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: MaskKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected spectral mask fields"))
    }
}

/// Portable artifact only; no source measurement, equipment identity, or calibration assertion.
public struct PlatformSpectralMask: Codable, Equatable, Sendable {
    public static let maximumBytes = 65_536
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public struct Vertex: Codable, Equatable, Sendable {
        public let frequencyHz: Int64
        public let limitDbm: Double
        private enum CodingKeys: String, CodingKey, Sendable { case frequencyHz, limitDbm }
        public init(from decoder: Decoder) throws {
            try maskExactKeys(decoder, ["frequencyHz", "limitDbm"])
            let values = try decoder.container(keyedBy: CodingKeys.self)
            frequencyHz = try values.decode(Int64.self, forKey: .frequencyHz)
            limitDbm = try values.decode(Double.self, forKey: .limitDbm)
            guard (0...PlatformSpectralMask.maximumFrequencyHz).contains(frequencyHz), limitDbm.isFinite else {
                throw DecodingError.dataCorruptedError(forKey: .frequencyHz, in: values, debugDescription: "Invalid mask vertex")
            }
        }
    }
    public let format: String
    public let formatVersion: Int
    public let name: String
    public let frequencyUnit: String
    public let levelUnit: String
    public let interpolation: String
    public let vertices: [Vertex]
    private enum CodingKeys: String, CodingKey, Sendable {
        case format, formatVersion, name, frequencyUnit, levelUnit, interpolation, vertices
    }
    public init(from decoder: Decoder) throws {
        try maskExactKeys(decoder, ["format", "formatVersion", "name", "frequencyUnit", "levelUnit", "interpolation", "vertices"])
        let values = try decoder.container(keyedBy: CodingKeys.self)
        format = try values.decode(String.self, forKey: .format)
        formatVersion = try values.decode(Int.self, forKey: .formatVersion)
        name = try values.decode(String.self, forKey: .name)
        frequencyUnit = try values.decode(String.self, forKey: .frequencyUnit)
        levelUnit = try values.decode(String.self, forKey: .levelUnit)
        interpolation = try values.decode(String.self, forKey: .interpolation)
        vertices = try values.decode([Vertex].self, forKey: .vertices)
        guard format == "openradioinspector-spectral-mask", formatVersion == 1,
              frequencyUnit == "Hz", levelUnit == "dBm", interpolation == "linear_dbm",
              (try? normalizedSweepReferenceName(name))?.unicodeScalars.elementsEqual(name.unicodeScalars) == true,
              (2...128).contains(vertices.count),
              zip(vertices, vertices.dropFirst()).allSatisfy({ $0.frequencyHz < $1.frequencyHz }) else {
            throw DecodingError.dataCorruptedError(forKey: .vertices, in: values, debugDescription: "Invalid canonical spectral mask")
        }
    }

    public func canonicalData() throws -> Data {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(self)
        guard data.count <= Self.maximumBytes else { throw PlatformError.invalidResponse("Маска превышает 65 536 байт") }
        return data
    }

    /// Compare only after the core has processed the original raw text. Never used to rewrite a request.
    static func submitted(_ text: String) throws -> Self {
        let bytes = Data(text.utf8)
        var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        guard bytes.count <= maximumBytes,
              var object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              let name = object["name"] as? String else { throw PlatformError.invalidResponse("Некорректная исходная маска") }
        try validateMaskShape(object)
        object["name"] = try normalizedSweepReferenceName(name)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Self.self, from: JSONSerialization.data(withJSONObject: object))
    }
}

/// A frozen, read-only core calculation. All plotted arrays come from the core.
public struct PlatformSweepMaskAnalysis: Decodable, Equatable, Sendable {
    public let sessionId: String
    public let streamId: String
    public let sweep: PlatformComparisonSweep
    public let mask: PlatformSpectralMask
    public let frequenciesHz: [Int64]
    public let thresholdLevels: [Double]
    public let levelUnit: String
    public let excessLevels: [Double]
    public let deltaUnit: String
    public let comparison: String
    public let regions: [PlatformSweepThresholdRegion]
    public let points: Int
    public let exceedancePoints: Int
    public let regionCount: Int
    private enum CodingKeys: String, CodingKey, Sendable {
        case sessionId, streamId, sweep, mask, frequenciesHz, thresholdLevels, levelUnit
        case excessLevels, deltaUnit, comparison, regions, points, exceedancePoints, regionCount
    }
    public init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try values.decode(String.self, forKey: .sessionId)
        streamId = try values.decode(String.self, forKey: .streamId)
        sweep = try values.decode(PlatformComparisonSweep.self, forKey: .sweep)
        mask = try values.decode(PlatformSpectralMask.self, forKey: .mask)
        frequenciesHz = try values.decode([Int64].self, forKey: .frequenciesHz)
        thresholdLevels = try values.decode([Double].self, forKey: .thresholdLevels)
        levelUnit = try values.decode(String.self, forKey: .levelUnit)
        excessLevels = try values.decode([Double].self, forKey: .excessLevels)
        deltaUnit = try values.decode(String.self, forKey: .deltaUnit)
        comparison = try values.decode(String.self, forKey: .comparison)
        regions = try values.decode([PlatformSweepThresholdRegion].self, forKey: .regions)
        points = try values.decode(Int.self, forKey: .points)
        exceedancePoints = try values.decode(Int.self, forKey: .exceedancePoints)
        regionCount = try values.decode(Int.self, forKey: .regionCount)
        func invalid(_ text: String) -> DecodingError {
            .dataCorruptedError(forKey: .points, in: values, debugDescription: text)
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              sweep.isValid, levelUnit == "dBm", deltaUnit == "dB", comparison == "strictly_greater",
              (1...16_384).contains(points), frequenciesHz.count == points,
              sweep.levels.count == points, thresholdLevels.count == points, excessLevels.count == points,
              frequenciesHz.allSatisfy({ (0...PlatformSpectralMask.maximumFrequencyHz).contains($0) }),
              zip(frequenciesHz, frequenciesHz.dropFirst()).allSatisfy({ $0 <= $1 }),
              frequenciesHz.first! >= mask.vertices.first!.frequencyHz,
              frequenciesHz.last! <= mask.vertices.last!.frequencyHz,
              thresholdLevels.allSatisfy(\.isFinite), excessLevels.allSatisfy(\.isFinite),
              regionCount == regions.count, exceedancePoints == excessLevels.filter({ $0 > 0 }).count else {
            throw invalid("Некорректный результат спектральной маски")
        }
        // Validate returned derived numbers, without substituting client-calculated data.
        var segment = 0
        for index in 0..<points {
            let hz = frequenciesHz[index]
            while segment + 1 < mask.vertices.count - 1 && hz > mask.vertices[segment + 1].frequencyHz { segment += 1 }
            let a = mask.vertices[segment], b = mask.vertices[segment + 1]
            let ratio = Double(hz - a.frequencyHz) / Double(b.frequencyHz - a.frequencyHz)
            let expected: Double
            if hz == a.frequencyHz || a.limitDbm == b.limitDbm { expected = a.limitDbm }
            else if hz == b.frequencyHz { expected = b.limitDbm }
            else if (a.limitDbm >= 0) == (b.limitDbm >= 0) { expected = a.limitDbm + (b.limitDbm - a.limitDbm) * ratio }
            else { expected = (1 - ratio) * a.limitDbm + ratio * b.limitDbm }
            let tolerance = max(1, abs(expected), abs(thresholdLevels[index])) * 1e-12
            guard expected.isFinite, abs(thresholdLevels[index] - expected) <= tolerance,
                  excessLevels[index] == sweep.levels[index] - thresholdLevels[index] else {
                throw invalid("Несогласованные порог dBm или превышение dB")
            }
        }
        var cursor = 0
        for region in regions {
            while cursor < points && excessLevels[cursor] <= 0 { cursor += 1 }
            guard cursor < points, region.startIndex == cursor else { throw invalid("Неверное начало области") }
            var peak = cursor
            while cursor < points && excessLevels[cursor] > 0 {
                if excessLevels[cursor] > excessLevels[peak] { peak = cursor }
                cursor += 1
            }
            guard region.endIndex == cursor - 1, region.points == cursor - region.startIndex,
                  region.startFrequencyHz == frequenciesHz[region.startIndex], region.endFrequencyHz == frequenciesHz[cursor - 1],
                  region.peakIndex == peak, region.peakFrequencyHz == frequenciesHz[peak],
                  region.peakExcessDb.isFinite, region.peakExcessDb == excessLevels[peak] else { throw invalid("Неверная область или максимум") }
        }
        guard excessLevels.dropFirst(cursor).allSatisfy({ $0 <= 0 }) else { throw invalid("Пропущена область превышения") }
    }
}

extension PlatformClient {
    /// POST is explicitly read-only. Cancellation never implies an unknown archive write.
    public func sweepMaskAnalysis(sessionID: String, sweepID: String, maskJSON: String) async throws -> PlatformSweepMaskAnalysis {
        struct Body: Encodable, Sendable { let sweepId: String; let maskJson: String }
        guard [sessionID, sweepID].allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }),
              maskJSON.utf8.count <= PlatformSpectralMask.maximumBytes else {
            throw PlatformError.invalidCommand("Нужны ID сеанса/свипа и маска не более 65 536 байт UTF-8")
        }
        let body = Body(sweepId: sweepID, maskJson: maskJSON)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 524_288 else { throw PlatformError.invalidCommand("Запрос превышает 524 288 байт") }
        try Task.checkCancellation()
        let result: PlatformSweepMaskAnalysis = try await post(
            "/sessions/\(Self.segment(sessionID))/sweep-mask-analysis", body: body, expectedStatuses: [200], validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
                guard let envelope = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                      let data = envelope["data"] as? [String: Any], let mask = data["mask"] as? [String: Any] else {
                    throw PlatformError.invalidResponse("Отсутствует спектральная маска")
                }
                try validateMaskShape(mask)
                guard ["points", "exceedance_points", "region_count"].allSatisfy({ maskJSONInteger(data[$0]) }),
                      let frequencies = data["frequencies_hz"] as? [Any], frequencies.allSatisfy({ maskJSONInteger($0) }),
                      let regions = data["regions"] as? [[String: Any]], regions.allSatisfy({ region in
                          ["start_index", "end_index", "start_frequency_hz", "end_frequency_hz", "peak_index", "peak_frequency_hz", "points"]
                              .allSatisfy({ maskJSONInteger(region[$0]) })
                      }) else { throw PlatformError.invalidResponse("Ожидались целые индексы, частоты и счётчики") }
            })
        try Task.checkCancellation()
        guard PlatformExactID.same(result.sessionId, sessionID), PlatformExactID.same(result.sweep.id, sweepID),
              result.mask == (try PlatformSpectralMask.submitted(maskJSON)) else {
            throw PlatformError.invalidResponse("Сервер вернул другой свип или маску")
        }
        return result
    }
}


// Check raw field spellings and integer JSON types before snake-case DTO decoding.
private func maskJSONInteger(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
    return !["f", "d"].contains(String(cString: number.objCType))
}
private func validateMaskShape(_ mask: [String: Any]) throws {
    guard Set(mask.keys) == ["format", "format_version", "name", "frequency_unit", "level_unit", "interpolation", "vertices"],
          maskJSONInteger(mask["format_version"]), let vertices = mask["vertices"] as? [[String: Any]],
          vertices.allSatisfy({ Set($0.keys) == ["frequency_hz", "limit_dbm"] && maskJSONInteger($0["frequency_hz"]) }) else {
        throw PlatformError.invalidResponse("Некорректные поля или целые значения маски")
    }
}

/// Bounded raw scan prevents Foundation from silently collapsing duplicate JSON object keys.
/// JSONSerialization / JSONDecoder still perform complete grammar and value validation afterward.
// Also used by strict derived endpoints that must not lose provenance keys during Dictionary decoding.
struct MaskJSONScanner: Sendable {
    let bytes: [UInt8]
    // Only raw-preserving consumers may allow canonically equivalent but byte-distinct keys.
    var exactKeyIdentity = false
    var index = 0
    mutating func check() throws {
        try value(depth: 0); whitespace()
        guard index == bytes.count else { throw invalid() }
    }
    func invalid() -> PlatformError { .invalidResponse("Некорректный JSON или повторяющиеся поля") }
    mutating func whitespace() { while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 } }
    mutating func string() throws -> String {
        guard index < bytes.count && bytes[index] == 34 else { throw invalid() }
        let start = index; index += 1
        while index < bytes.count {
            let byte = bytes[index]; index += 1
            if byte == 34 { return try JSONDecoder().decode(String.self, from: Data(bytes[start..<index])) }
            if byte == 92 { guard index < bytes.count else { throw invalid() }; index += 1 }
        }
        throw invalid()
    }
    mutating func value(depth: Int) throws {
        whitespace(); guard depth <= 128, index < bytes.count else { throw invalid() }
        if bytes[index] == 34 { _ = try string(); return }
        if bytes[index] == 123 {
            index += 1; whitespace(); var keys = Set<String>(); var exactKeys = Set<Data>()
            if index < bytes.count && bytes[index] == 125 { index += 1; return }
            while true {
                whitespace(); let key = try string()
                guard exactKeyIdentity ? exactKeys.insert(Data(key.utf8)).inserted : keys.insert(key).inserted else { throw invalid() }
                whitespace(); guard index < bytes.count && bytes[index] == 58 else { throw invalid() }
                index += 1; try value(depth: depth + 1); whitespace()
                guard index < bytes.count else { throw invalid() }
                if bytes[index] == 125 { index += 1; return }
                guard bytes[index] == 44 else { throw invalid() }; index += 1
            }
        }
        if bytes[index] == 91 {
            index += 1; whitespace()
            if index < bytes.count && bytes[index] == 93 { index += 1; return }
            while true {
                try value(depth: depth + 1); whitespace(); guard index < bytes.count else { throw invalid() }
                if bytes[index] == 93 { index += 1; return }
                guard bytes[index] == 44 else { throw invalid() }; index += 1
            }
        }
        let start = index
        while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) { index += 1 }
        guard index > start else { throw invalid() }
    }
}
