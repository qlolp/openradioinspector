import Foundation

/// Contract identifiers are opaque UTF-8, with no Unicode normalization.
/// `rawValue` is the original API value; this wrapper is for local keys/selection only.
public struct PlatformExactID: RawRepresentable, Hashable, Comparable, Sendable, ExpressibleByStringLiteral {
    public let rawValue: String
    public init(_ value: String) { rawValue = value }
    public init(rawValue: String) { self.rawValue = rawValue }
    public init(stringLiteral value: String) { rawValue = value }
    public static func == (left: Self, right: Self) -> Bool { left.rawValue.utf8.elementsEqual(right.rawValue.utf8) }
    public func hash(into hasher: inout Hasher) { hasher.combine(Data(rawValue.utf8)) }
    public static func < (left: Self, right: Self) -> Bool { left.rawValue.utf8.lexicographicallyPrecedes(right.rawValue.utf8) }
    public static func same(_ left: String?, _ right: String?) -> Bool {
        switch (left, right) {
        case let (left?, right?): return left.utf8.elementsEqual(right.utf8)
        case (nil, nil): return true
        default: return false
        }
    }
}

public extension Identifiable where ID == String {
    var exactID: PlatformExactID { PlatformExactID(id) }
}
