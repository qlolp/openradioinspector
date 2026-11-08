import Foundation
import CoreFoundation

/// Portable, explicitly non-normative grid profile. Calculations below validate core responses only.
public struct PlatformFrequencyRoundingProfile: Codable, Equatable {
    public static let maximumBytes = 65_536
    public static let maximumFrequencyHz: Int64 = 9_007_199_254_740_991
    public struct Rule: Codable, Equatable {
        public let startFrequencyHz: Int64
        public let endFrequencyHz: Int64
        public let stepHz: Int64
        public let originFrequencyHz: Int64
        public let maxOffsetHz: Int64
        var valid: Bool {
            [startFrequencyHz, endFrequencyHz, stepHz, originFrequencyHz, maxOffsetHz]
                .allSatisfy { (0...PlatformFrequencyRoundingProfile.maximumFrequencyHz).contains($0) }
                && stepHz > 0 && startFrequencyHz <= endFrequencyHz && firstIndex <= lastIndex
        }
        var firstIndex: Int64 { -floorDivide(originFrequencyHz - startFrequencyHz, stepHz) }
        var lastIndex: Int64 { floorDivide(endFrequencyHz - originFrequencyHz, stepHz) }
        func nearest(_ frequency: Int64) -> Int64 {
            let lower = min(lastIndex, max(firstIndex, floorDivide(frequency - originFrequencyHz, stepHz)))
            let upper = min(lastIndex, max(firstIndex, lower + 1))
            let a = originFrequencyHz + lower * stepHz, b = originFrequencyHz + upper * stepHz
            return abs(frequency - a) <= abs(frequency - b) ? a : b
        }
    }
    public let format: String
    public let formatVersion: Int
    public let name: String
    public let frequencyUnit: String
    public let method: String
    public let tieBreak: String
    public let rules: [Rule]
    private enum CodingKeys: String, CodingKey { case format, formatVersion, name, frequencyUnit, method, tieBreak, rules }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        format = try c.decode(String.self, forKey: .format)
        formatVersion = try c.decode(Int.self, forKey: .formatVersion)
        name = try c.decode(String.self, forKey: .name)
        frequencyUnit = try c.decode(String.self, forKey: .frequencyUnit)
        method = try c.decode(String.self, forKey: .method)
        tieBreak = try c.decode(String.self, forKey: .tieBreak)
        rules = try c.decode([Rule].self, forKey: .rules)
        guard format == "openradioinspector-frequency-rounding", formatVersion == 1,
              frequencyUnit == "Hz", method == "nearest_grid", tieBreak == "lower",
              (try? normalizedSweepReferenceName(name)).map({ PlatformThreeSweepThresholds.sameID($0, name) }) == true,
              (1...128).contains(rules.count), rules.allSatisfy(\.valid),
              zip(rules, rules.dropFirst()).allSatisfy({ $0.endFrequencyHz < $1.startFrequencyHz }) else {
            throw PlatformError.invalidResponse("Некорректный канонический профиль округления")
        }
    }
    public static func submitted(_ text: String) throws -> Self {
        guard text.utf8.count <= maximumBytes else { throw PlatformError.invalidResponse("Профиль превышает 65 536 байт") }
        let bytes = Data(text.utf8)
        var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
        // JSONSerialization can strip a leading U+FEFF from NSString values/keys.
        // Decode original field spellings and all strings through JSONDecoder before any bridging.
        let raw = try JSONDecoder().decode(RoundingRawProfile.self, from: bytes)
        let name = try normalizedSweepReferenceName(raw.name)
        guard var object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
            throw PlatformError.invalidResponse("Некорректный JSON профиля")
        }
        try roundingProfileShape(object)
        object["name"] = name
        object["format"] = raw.format
        object["frequency_unit"] = raw.frequencyUnit
        object["method"] = raw.method
        object["tie_break"] = raw.tieBreak
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Self.self, from: JSONSerialization.data(withJSONObject: object))
    }
    public func canonicalData() throws -> Data {
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        let data = try encoder.encode(self)
        guard data.count <= Self.maximumBytes else { throw PlatformError.invalidResponse("Профиль превышает 65 536 байт") }
        return data
    }
}

private struct RoundingRawKey: CodingKey {
    let stringValue: String
    var intValue: Int? { nil }
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}
private func roundingExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let c = try decoder.container(keyedBy: RoundingRawKey.self)
    guard Set(c.allKeys.map(\.stringValue)) == expected else {
        throw PlatformError.invalidResponse("Некорректные исходные поля JSON округления")
    }
}
private struct RoundingRawProfile: Decodable {
    let name: String
    let format: String
    let frequencyUnit: String
    let method: String
    let tieBreak: String
    private enum CodingKeys: String, CodingKey {
        case name, format, method, rules
        case frequencyUnit = "frequency_unit"
        case tieBreak = "tie_break"
    }
    private struct Rule: Decodable {
        init(from decoder: Decoder) throws {
            try roundingExactKeys(decoder, ["start_frequency_hz", "end_frequency_hz", "step_hz", "origin_frequency_hz", "max_offset_hz"])
        }
    }
    init(from decoder: Decoder) throws {
        try roundingExactKeys(decoder, ["format", "format_version", "name", "frequency_unit", "method", "tie_break", "rules"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        format = try c.decode(String.self, forKey: .format)
        frequencyUnit = try c.decode(String.self, forKey: .frequencyUnit)
        method = try c.decode(String.self, forKey: .method)
        tieBreak = try c.decode(String.self, forKey: .tieBreak)
        _ = try c.decode([Rule].self, forKey: .rules)
    }
}

private struct RoundingRawEnvelope: Decodable {
    private enum CodingKeys: String, CodingKey { case data }
    private struct Payload: Decodable {
        private enum CodingKeys: String, CodingKey { case candidate, profile, result }
        private struct Candidate: Decodable {
            private enum CodingKeys: String, CodingKey { case interval }
            private struct Interval: Decodable {
                init(from decoder: Decoder) throws {
                    try roundingExactKeys(decoder, ["started_at", "ended_at"])
                }
            }
            init(from decoder: Decoder) throws {
                try roundingExactKeys(decoder, ["id", "source_stream_id", "interval", "center_frequency_hz", "level_unit"])
                let c = try decoder.container(keyedBy: CodingKeys.self)
                _ = try c.decode(Interval.self, forKey: .interval)
            }
        }
        private struct Result: Decodable {
            init(from decoder: Decoder) throws {
                try roundingExactKeys(decoder, ["status", "rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"])
            }
        }
        init(from decoder: Decoder) throws {
            try roundingExactKeys(decoder, ["session_id", "candidate", "profile", "frequency_unit", "method", "tie_break", "result"])
            let c = try decoder.container(keyedBy: CodingKeys.self)
            _ = try c.decode(Candidate.self, forKey: .candidate)
            _ = try c.decode(RoundingRawProfile.self, forKey: .profile)
            _ = try c.decode(Result.self, forKey: .result)
        }
    }
    init(from decoder: Decoder) throws {
        try roundingExactKeys(decoder, ["contract", "contract_version", "data"])
        let c = try decoder.container(keyedBy: CodingKeys.self)
        _ = try c.decode(Payload.self, forKey: .data)
    }
}

private func floorDivide(_ numerator: Int64, _ denominator: Int64) -> Int64 {
    numerator / denominator - (numerator % denominator < 0 ? 1 : 0)
}
private func roundingScopeID(_ id: String) -> Bool {
    // Python str.strip whitespace, not Foundation's broader set (which includes U+200B).
    // Test blankness only; preserve every original UTF-8 byte in IDs and units.
    !id.isEmpty && !id.unicodeScalars.allSatisfy { scalar in
        switch scalar.value {
        case 0x0009...0x000D, 0x001C...0x0020, 0x0085, 0x00A0, 0x1680,
             0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000: return true
        default: return false
        }
    }
}
private func roundingCandidateID(_ id: String) -> Bool { roundingScopeID(id) && id.unicodeScalars.count <= 128 }

/// Immutable frequency provenance, deliberately excluding candidate state, observations and levels.
public struct PlatformRoundingCandidate: Decodable, Equatable {
    public let id: String
    public let sourceStreamId: String
    public let interval: PlatformInterval
    public let centerFrequencyHz: Int64
    public let levelUnit: String
    var valid: Bool {
        roundingCandidateID(id) && roundingScopeID(sourceStreamId) && averageInterval(interval) != nil
            && (1...PlatformFrequencyRoundingProfile.maximumFrequencyHz).contains(centerFrequencyHz)
            && roundingScopeID(levelUnit)
    }
    public func matches(_ source: PlatformCandidate) -> Bool {
        PlatformThreeSweepThresholds.sameID(id, source.id)
            && PlatformThreeSweepThresholds.sameID(sourceStreamId, source.sourceStreamId)
            && PlatformThreeSweepThresholds.sameID(interval.startedAt, source.interval.startedAt)
            && PlatformThreeSweepThresholds.sameID(interval.endedAt, source.interval.endedAt)
            && centerFrequencyHz == source.centerFrequencyHz
            && PlatformThreeSweepThresholds.sameID(levelUnit, source.levelUnit)
    }
    public static func == (a: Self, b: Self) -> Bool {
        PlatformThreeSweepThresholds.sameID(a.id, b.id) && PlatformThreeSweepThresholds.sameID(a.sourceStreamId, b.sourceStreamId)
            && PlatformThreeSweepThresholds.sameID(a.interval.startedAt, b.interval.startedAt)
            && PlatformThreeSweepThresholds.sameID(a.interval.endedAt, b.interval.endedAt)
            && a.centerFrequencyHz == b.centerFrequencyHz && PlatformThreeSweepThresholds.sameID(a.levelUnit, b.levelUnit)
    }
}

public struct PlatformCandidateFrequencyRounding: Decodable, Equatable {
    public struct Result: Decodable, Equatable {
        public let status: String
        public let ruleIndex: Int?
        public let nearestFrequencyHz: Int64?
        public let offsetHz: Int64?
        public let roundedFrequencyHz: Int64?
    }
    public let sessionId: String
    public let candidate: PlatformRoundingCandidate
    public let profile: PlatformFrequencyRoundingProfile
    public let frequencyUnit: String
    public let method: String
    public let tieBreak: String
    public let result: Result
    private enum CodingKeys: String, CodingKey { case sessionId, candidate, profile, frequencyUnit, method, tieBreak, result }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try c.decode(String.self, forKey: .sessionId)
        candidate = try c.decode(PlatformRoundingCandidate.self, forKey: .candidate)
        profile = try c.decode(PlatformFrequencyRoundingProfile.self, forKey: .profile)
        frequencyUnit = try c.decode(String.self, forKey: .frequencyUnit)
        method = try c.decode(String.self, forKey: .method)
        tieBreak = try c.decode(String.self, forKey: .tieBreak)
        result = try c.decode(Result.self, forKey: .result)
        guard roundingScopeID(sessionId), candidate.valid, frequencyUnit == "Hz", method == "nearest_grid", tieBreak == "lower" else {
            throw PlatformError.invalidResponse("Некорректное происхождение округления")
        }
        let frequency = candidate.centerFrequencyHz
        if let index = profile.rules.firstIndex(where: { ($0.startFrequencyHz...$0.endFrequencyHz).contains(frequency) }) {
            let rule = profile.rules[index], nearest = rule.nearest(frequency)
            let offset = nearest - frequency, accepted = abs(offset) <= rule.maxOffsetHz
            let status = !accepted ? "outside_tolerance" : offset == 0 ? "unchanged" : "rounded"
            guard result.ruleIndex == index, result.nearestFrequencyHz == nearest, result.offsetHz == offset,
                  result.roundedFrequencyHz == (accepted ? nearest : nil), result.status == status else {
                throw PlatformError.invalidResponse("Несогласованные частота, допуск или статус округления")
            }
        } else {
            guard result.status == "outside_ranges", result.ruleIndex == nil, result.nearestFrequencyHz == nil,
                  result.offsetHz == nil, result.roundedFrequencyHz == nil else {
                throw PlatformError.invalidResponse("За диапазонами результат должен быть пустым")
            }
        }
    }
    public static func validSource(_ source: PlatformCandidate) -> Bool {
        roundingScopeID(source.sessionId) && roundingCandidateID(source.id) && roundingScopeID(source.sourceStreamId)
            && averageInterval(source.interval) != nil && roundingScopeID(source.levelUnit)
            && (1...PlatformFrequencyRoundingProfile.maximumFrequencyHz).contains(source.centerFrequencyHz)
    }
    public func validate(source: PlatformCandidate, profileJSON: String) throws {
        guard Self.validSource(source), PlatformThreeSweepThresholds.sameID(sessionId, source.sessionId), candidate.matches(source),
              profile == (try PlatformFrequencyRoundingProfile.submitted(profileJSON)) else {
            throw PlatformError.invalidResponse("Ответ не соответствует зафиксированному кандидату или профилю")
        }
    }
    public static func == (a: Self, b: Self) -> Bool {
        PlatformThreeSweepThresholds.sameID(a.sessionId, b.sessionId) && a.candidate == b.candidate && a.profile == b.profile
            && a.frequencyUnit == b.frequencyUnit && a.method == b.method && a.tieBreak == b.tieBreak && a.result == b.result
    }
}

private func roundingInteger(_ value: Any?) -> Bool {
    guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { return false }
    return !["f", "d"].contains(String(cString: number.objCType))
}
private func roundingProfileShape(_ object: [String: Any]) throws {
    let fields: Set<String> = ["start_frequency_hz", "end_frequency_hz", "step_hz", "origin_frequency_hz", "max_offset_hz"]
    guard Set(object.keys) == ["format", "format_version", "name", "frequency_unit", "method", "tie_break", "rules"],
          roundingInteger(object["format_version"]), let rules = object["rules"] as? [[String: Any]],
          rules.allSatisfy({ rule in Set(rule.keys) == fields && fields.allSatisfy { roundingInteger(rule[$0]) } }) else {
        throw PlatformError.invalidResponse("Некорректные поля или целые значения профиля")
    }
}

extension PlatformClient {
    public func candidateFrequencyRounding(sessionID: String, candidateID: String, profileJSON: String) async throws -> PlatformCandidateFrequencyRounding {
        struct Body: Encodable { let candidateId: String; let profileJson: String }
        guard roundingScopeID(sessionID), roundingCandidateID(candidateID), profileJSON.utf8.count <= PlatformFrequencyRoundingProfile.maximumBytes else {
            throw PlatformError.invalidCommand("Нужны сеанс, ID кандидата до 128 символов и профиль до 65 536 байт UTF-8")
        }
        let body = Body(candidateId: candidateID, profileJson: profileJSON)
        let encoder = JSONEncoder(); encoder.keyEncodingStrategy = .convertToSnakeCase
        guard try encoder.encode(body).count <= 524_288 else { throw PlatformError.invalidCommand("Запрос превышает 524 288 байт") }
        try Task.checkCancellation()
        let response: PlatformCandidateFrequencyRounding = try await post(
            "/sessions/\(Self.segment(sessionID))/candidate-frequency-rounding", body: body, expectedStatuses: [200], timeoutInterval: 6, validateResponse: { bytes in
                var scanner = MaskJSONScanner(bytes: Array(bytes)); try scanner.check()
                _ = try JSONDecoder().decode(RoundingRawEnvelope.self, from: bytes)
                guard let envelope = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
                      Set(envelope.keys) == ["contract", "contract_version", "data"], roundingInteger(envelope["contract_version"]),
                      let data = envelope["data"] as? [String: Any],
                      Set(data.keys) == ["session_id", "candidate", "profile", "frequency_unit", "method", "tie_break", "result"],
                      let profile = data["profile"] as? [String: Any], let source = data["candidate"] as? [String: Any],
                      Set(source.keys) == ["id", "source_stream_id", "interval", "center_frequency_hz", "level_unit"],
                      roundingInteger(source["center_frequency_hz"]), let interval = source["interval"] as? [String: Any],
                      Set(interval.keys) == ["started_at", "ended_at"], let result = data["result"] as? [String: Any],
                      Set(result.keys) == ["status", "rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"],
                      ["rule_index", "nearest_frequency_hz", "offset_hz", "rounded_frequency_hz"].allSatisfy({
                          result[$0] is NSNull || roundingInteger(result[$0])
                      }) else { throw PlatformError.invalidResponse("Некорректный строгий ответ округления") }
                try roundingProfileShape(profile)
            })
        try Task.checkCancellation()
        guard PlatformThreeSweepThresholds.sameID(response.sessionId, sessionID),
              PlatformThreeSweepThresholds.sameID(response.candidate.id, candidateID),
              response.profile == (try PlatformFrequencyRoundingProfile.submitted(profileJSON)) else {
            throw PlatformError.invalidResponse("Сервер вернул другой источник или профиль округления")
        }
        return response
    }
}
