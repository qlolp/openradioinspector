import Foundation

public enum ContractError: Error, Equatable, Sendable {
    case invalid(String)
    case tooLarge(Int)
}

/// Byte-exact JSON names: Swift String dictionaries otherwise identify NFC and
/// NFD spellings as one key, which would discard valid unknown metadata.
public struct JSONObject: Equatable, Sendable, ExpressibleByDictionaryLiteral, Codable, Sequence {
    public typealias Key = String
    public typealias Value = JSONValue
    private struct RawKey: Hashable, Sendable {
        let text: String
        let bytes: [UInt8]
        init(_ text: String) { self.text=text; bytes=Array(text.utf8) }
        static func == (lhs: Self,rhs: Self) -> Bool { lhs.bytes == rhs.bytes }
        func hash(into hasher: inout Hasher) { hasher.combine(bytes) }
    }
    private var storage: [RawKey: JSONValue] = [:]
    public init() {}
    public init(_ values: [String: JSONValue]) { for (key,value) in values { self[key]=value } }
    public init(dictionaryLiteral elements: (String,JSONValue)...) { for (key,value) in elements { self[key]=value } }
    public var keys: [String] { storage.keys.map(\.text) }
    public var count: Int { storage.count }
    public var isEmpty: Bool { storage.isEmpty }
    public subscript(_ key: String) -> JSONValue? {
        get { storage[RawKey(key)] }
        set { storage[RawKey(key)] = newValue }
    }
    @discardableResult public mutating func removeValue(forKey key: String) -> JSONValue? { storage.removeValue(forKey: RawKey(key)) }
    public func makeIterator() -> AnyIterator<(key: String,value: JSONValue)> {
        var iterator=storage.makeIterator()
        return AnyIterator { iterator.next().map { (key:$0.key.text,value:$0.value) } }
    }
    public init(from decoder: any Decoder) throws { self.init(try decoder.singleValueContainer().decode([String: JSONValue].self)) }
    public func encode(to encoder: any Encoder) throws {
        guard Set(keys).count == count else { throw ContractError.invalid("Unicode-distinct keys require StrictJSON") }
        var container=encoder.singleValueContainer()
        try container.encode(Dictionary(uniqueKeysWithValues: map { ($0.key,$0.value) }))
    }
}

/// The integer token is retained verbatim, even beyond UInt64. Measurement
/// float64 values use .number and preserve negative zero through StrictJSON.
public indirect enum JSONValue: Equatable, Sendable, Codable {
    case null, bool(Bool), string(String), integer(String), number(Double)
    case array([JSONValue]), object(JSONObject)
    public var object: JSONObject? { if case let .object(value) = self { value } else { nil } }
    public var array: [JSONValue]? { if case let .array(value) = self { value } else { nil } }
    public var double: Double? {
        switch self { case let .number(value): value; case let .integer(value): Double(value); default: nil }
    }
    public func requireObject() throws -> JSONObject { guard let object else { throw ContractError.invalid("Expected object") }; return object }
    public func requireArray() throws -> [JSONValue] { guard let array else { throw ContractError.invalid("Expected array") }; return array }
    public func requireString() throws -> String { guard case let .string(value) = self else { throw ContractError.invalid("Expected string") }; return value }
    public func requireBool() throws -> Bool { guard case let .bool(value) = self else { throw ContractError.invalid("Expected boolean") }; return value }
    public func requireUInt64() throws -> UInt64 { guard case let .integer(text) = self, let value = UInt64(text) else { throw ContractError.invalid("Expected exact UInt64") }; return value }
    public func requireInt64() throws -> Int64 { guard case let .integer(text) = self, let value = Int64(text) else { throw ContractError.invalid("Expected exact Int64") }; return value }
    public func requireDouble() throws -> Double { guard let value = double, value.isFinite else { throw ContractError.invalid("Expected finite float64") }; return value }

    /// Codable is an adapter for existing clients. StrictJSON is the wire entry
    /// point: generic Foundation decoders cannot expose arbitrary integer tokens.
    public init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode(Double.self), value == 0, value.sign == .minus { self = .number(value) }
        else if let value = try? container.decode(Int64.self) { self = .integer(String(value)) }
        else if let value = try? container.decode(UInt64.self) { self = .integer(String(value)) }
        else if let value = try? container.decode(Double.self) {
            guard value.isFinite, !(value.rounded(.towardZero) == value && abs(value) >= 9007199254740992) else { throw ContractError.invalid("Large numeric tokens require StrictJSON") }
            self = .number(value)
        }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try container.decode(JSONObject.self)) }
    }
    public func encode(to encoder: any Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case let .bool(value): try container.encode(value)
        case let .string(value): try container.encode(value)
        case let .integer(text):
            if let value = Int64(text) { try container.encode(value) }
            else if let value = UInt64(text) { try container.encode(value) }
            else { throw ContractError.invalid("Large integer tokens require StrictJSON") }
        case let .number(value):
            guard value.isFinite else { throw ContractError.invalid("Nonfinite float64") }
            try container.encode(value)
        case let .array(value): try container.encode(value)
        case let .object(value): try container.encode(value)
        }
    }
}

public protocol ContractModel: Codable, Sendable {
    init(json: JSONValue) throws
    var json: JSONValue { get }
}
public extension ContractModel {
    init(from decoder: any Decoder) throws { try self.init(json: JSONValue(from: decoder)) }
    func encode(to encoder: any Encoder) throws { try json.encode(to: encoder) }
}

public enum StrictJSON {
    public static let defaultMaxBytes = 16 * 1024 * 1024
    public static func parse(_ data: Data, maxBytes: Int = defaultMaxBytes) throws -> JSONValue {
        guard maxBytes >= 0, data.count <= maxBytes else { throw ContractError.tooLarge(maxBytes) }
        guard String(data: data, encoding: .utf8) != nil else { throw ContractError.invalid("Invalid UTF-8") }
        var parser = Parser(bytes: Array(data))
        let value = try parser.value(depth: 0)
        parser.space()
        guard parser.index == parser.bytes.count else { throw ContractError.invalid("Trailing JSON data") }
        return value
    }
    public static func decode<T: ContractModel>(_ type: T.Type, from data: Data, maxBytes: Int = defaultMaxBytes) throws -> T { try T(json: parse(data,maxBytes: maxBytes)) }
    public static func encode<T: ContractModel>(_ value: T) throws -> Data { try serialize(value.json) }
    public static func serialize(_ value: JSONValue) throws -> Data { Data(try write(value,depth: 0).utf8) }

    private static func quoted(_ value: String) throws -> String {
        // Foundation is used only for escaping a single Unicode string.
        guard let result = String(data: try JSONEncoder().encode(value),encoding: .utf8) else { throw ContractError.invalid("Invalid string") }
        return result
    }
    private static func validInteger(_ token: String) -> Bool { token.range(of: #"^-?(0|[1-9][0-9]*)\z"#,options: .regularExpression) != nil }
    private static func write(_ value: JSONValue, depth: Int) throws -> String {
        guard depth <= 128 else { throw ContractError.invalid("JSON nesting exceeds 128") }
        switch value {
        case .null: return "null"
        case let .bool(value): return value ? "true" : "false"
        case let .string(value): return try quoted(value)
        case let .integer(token):
            guard validInteger(token) else { throw ContractError.invalid("Invalid integer token") }
            return token
        case let .number(value):
            guard value.isFinite else { throw ContractError.invalid("Nonfinite float64") }
            return value == 0 && value.sign == .minus ? "-0.0" : String(value)
        case let .array(values): return "[" + (try values.map { try write($0,depth: depth+1) }).joined(separator: ",") + "]"
        case let .object(values): return "{" + (try values.keys.sorted { Array($0.utf8).lexicographicallyPrecedes(Array($1.utf8)) }.map { try quoted($0) + ":" + write(values[$0]!,depth: depth+1) }).joined(separator: ",") + "}"
        }
    }

    private struct Parser {
        let bytes: [UInt8]
        var index = 0
        var current: UInt8? { index < bytes.count ? bytes[index] : nil }
        mutating func space() { while let byte = current, [9,10,13,32].contains(byte) { index += 1 } }
        mutating func take(_ byte: UInt8) -> Bool { if current == byte { index += 1; return true }; return false }
        mutating func string() throws -> String {
            let start = index
            guard take(34) else { throw ContractError.invalid("Expected string") }
            while let byte = current {
                index += 1
                if byte == 92 { index += 1 }
                else if byte == 34 {
                    return try JSONDecoder().decode(String.self,from: Data(bytes[start..<index]))
                }
            }
            throw ContractError.invalid("Unterminated string")
        }
        mutating func value(depth: Int) throws -> JSONValue {
            guard depth <= 128 else { throw ContractError.invalid("JSON nesting exceeds 128") }
            space()
            if current == 34 { return .string(try string()) }
            if take(123) {
                var object: JSONObject = [:]
                space(); if take(125) { return .object(object) }
                while true {
                    space(); let key = try string()
                    guard object[key] == nil else { throw ContractError.invalid("Duplicate JSON key: " + key) }
                    space(); guard take(58) else { throw ContractError.invalid("Expected colon") }
                    object[key] = try value(depth: depth+1)
                    space(); if take(125) { return .object(object) }
                    guard take(44) else { throw ContractError.invalid("Expected object separator") }
                }
            }
            if take(91) {
                var array: [JSONValue] = []
                space(); if take(93) { return .array(array) }
                while true {
                    array.append(try value(depth: depth+1))
                    space(); if take(93) { return .array(array) }
                    guard take(44) else { throw ContractError.invalid("Expected array separator") }
                }
            }
            for (word,value) in [("true", JSONValue.bool(true)),("false",.bool(false)),("null",.null)] {
                let token = Array(word.utf8)
                if bytes.count-index >= token.count, Array(bytes[index..<index+token.count]) == token { index += token.count; return value }
            }
            return try number()
        }
        mutating func number() throws -> JSONValue {
            let start = index
            _ = take(45)
            if take(48) {} else {
                guard let byte = current, (49...57).contains(byte) else { throw ContractError.invalid("Invalid JSON value") }
                while let byte = current, (48...57).contains(byte) { index += 1 }
            }
            var isFloat = false
            if take(46) {
                isFloat = true
                guard let byte = current, (48...57).contains(byte) else { throw ContractError.invalid("Invalid fraction") }
                while let byte = current, (48...57).contains(byte) { index += 1 }
            }
            if take(101) || take(69) {
                isFloat = true
                if !take(43) { _ = take(45) }
                guard let byte = current, (48...57).contains(byte) else { throw ContractError.invalid("Invalid exponent") }
                while let byte = current, (48...57).contains(byte) { index += 1 }
            }
            let token = String(decoding: bytes[start..<index],as: UTF8.self)
            if !isFloat && token != "-0" { return .integer(token) }
            guard let value = Double(token), value.isFinite else { throw ContractError.invalid("Nonfinite JSON number") }
            return .number(value)
        }
    }
}

/// Exact integer calendar/offset arithmetic. Excess legacy fractional digits are
/// truncated to microseconds before UTC normalization, as in the Python core.
public struct UtcTimestamp: Codable, Equatable, Hashable, Comparable, Sendable {
    public let rawValue: String
    public let unixMicros: Int64
    public var json: JSONValue { .string(rawValue) }
    public init(json: JSONValue) throws { try self.init(json.requireString()) }
    public init(_ text: String) throws {
        let regex = try NSRegularExpression(pattern: #"^([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[T ]([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]+))?)?)?(Z|[+-][0-9]{2}:?[0-9]{2})?$"#)
        guard let match = regex.firstMatch(in: text,range: NSRange(text.startIndex...,in: text)), match.range.length == text.utf16.count else { throw ContractError.invalid("Invalid ISO timestamp") }
        func group(_ index: Int) -> String? { guard let range = Range(match.range(at: index),in: text) else { return nil }; return String(text[range]) }
        let y = Int64(group(1)!)!, m = Int64(group(2)!)!, d = Int64(group(3)!)!, h = Int64(group(4) ?? "0")!, minute = Int64(group(5) ?? "0")!, second = Int64(group(6) ?? "0")!
        guard y>=1, m>=1, m<=12, d>=1, h<=23, minute<=59, second<=59 else { throw ContractError.invalid("Invalid timestamp component") }
        let leap = y%4 == 0 && (y%100 != 0 || y%400 == 0)
        let monthDays: [Int64] = [31,leap ? 29:28,31,30,31,30,31,31,30,31,30,31]
        guard d<=monthDays[Int(m-1)] else { throw ContractError.invalid("Invalid calendar date") }
        let fraction = String((group(7) ?? "").prefix(6))
        let micros = Int64(fraction + String(repeating: "0",count: 6-fraction.count))!
        var offset: Int64 = 0
        if let zone = group(8), zone != "Z" {
            let digits = zone.dropFirst().replacingOccurrences(of: ":",with: "")
            let zh = Int64(digits.prefix(2))!, zm = Int64(digits.suffix(2))!
            guard zh<=23, zm<=59 else { throw ContractError.invalid("Invalid timestamp offset") }
            offset = (zone.first == "+" ? 1 : -1) * (zh*3600 + zm*60)
        }
        let seconds = Self.daysFromCivil(y,m,d)*86400+h*3600+minute*60+second-offset
        unixMicros = seconds*1_000_000+micros
        let dayNumber = Self.floorDivide(seconds,86400)
        let daySeconds = seconds-dayNumber*86400
        let (uy,um,ud) = Self.civilFromDays(dayNumber)
        guard uy>=1, uy<=9999 else { throw ContractError.invalid("UTC year outside four-digit range") }
        rawValue = String(format:"%04lld-%02lld-%02lldT%02lld:%02lld:%02lld.%06lldZ",uy,um,ud,daySeconds/3600,(daySeconds%3600)/60,daySeconds%60,micros)
    }
    public init(from decoder: any Decoder) throws { try self.init(decoder.singleValueContainer().decode(String.self)) }
    public func encode(to encoder: any Encoder) throws { var container = encoder.singleValueContainer(); try container.encode(rawValue) }
    public static func < (lhs: Self,rhs: Self) -> Bool { lhs.unixMicros < rhs.unixMicros }
    private static func floorDivide(_ a: Int64,_ b: Int64) -> Int64 { let q=a/b; return a%b<0 ? q-1:q }
    private static func daysFromCivil(_ year: Int64,_ month: Int64,_ day: Int64) -> Int64 {
        let y=year-(month<=2 ? 1:0), era=floorDivide(y,400), yoe=y-era*400
        let mp=month+(month>2 ? -3:9), doy=(153*mp+2)/5+day-1
        return era*146097+yoe*365+yoe/4-yoe/100+doy-719468
    }
    private static func civilFromDays(_ dayNumber: Int64) -> (Int64,Int64,Int64) {
        let z=dayNumber+719468, era=floorDivide(z,146097), doe=z-era*146097
        let yoe=(doe-doe/1460+doe/36524-doe/146096)/365
        var y=yoe+era*400
        let doy=doe-(365*yoe+yoe/4-yoe/100), mp=(5*doy+2)/153
        let d=doy-(153*mp+2)/5+1, m=mp+(mp<10 ? 3:-9)
        y += m<=2 ? 1:0
        return (y,m,d)
    }
}

public func encodePathSegment(_ id: String) -> String {
    id.utf8.map { byte in
        (byte>=65 && byte<=90 || byte>=97 && byte<=122 || byte>=48 && byte<=57 || [45,46,95,126].contains(byte)) ? String(UnicodeScalar(byte)) : String(format:"%%%02X",byte)
    }.joined()
}
public func operationPath(_ template: String,parameters: [String: String]) throws -> String {
    let regex = try NSRegularExpression(pattern: #"\{([^}]+)\}"#)
    var result = template
    for match in regex.matches(in: template,range: NSRange(template.startIndex...,in: template)).reversed() {
        let name = String(template[Range(match.range(at: 1),in: template)!])
        guard let value=parameters[name] else { throw ContractError.invalid("Missing path parameter " + name) }
        result.replaceSubrange(Range(match.range,in: result)!,with: encodePathSegment(value))
    }
    return result
}
