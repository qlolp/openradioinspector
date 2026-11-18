import Foundation

enum NativeConsolePage: String, Codable, CaseIterable {
    case observation, archive
    var title: String { self == .observation ? "Наблюдение" : "Архив" }
}

enum NativeConsoleSettings: String, Codable, CaseIterable, Identifiable {
    case connection, guardSettings, signals, receiver
    var id: String { rawValue }
    var title: String {
        switch self {
        case .connection: "Приборы и подключение"
        case .guardSettings: "Охрана и исследование"
        case .signals: "Кандидаты и каталог"
        case .receiver: "Приёмник AR-DV1"
        }
    }
}

struct NativeConsoleDestination: Codable, Equatable {
    var page: NativeConsolePage = .observation
    var settings: NativeConsoleSettings?

    init(legacyTab: String = "") {
        if legacyTab == "archive" { page = .archive }
        if legacyTab == "devices" { settings = .connection }
        if legacyTab == "guard" { settings = .guardSettings }
    }
}

/// Data keys deliberately preserve NFC/NFD-distinct identifiers and exact origins.
struct NativeConsoleContext: Codable, Equatable {
    let components: [Data]
    init(server: String, project: String, session: String, stream: String) {
        components = [server, project, session, stream].map { Data($0.utf8) }
    }
}

struct NativeConsoleNavigation: Codable, Equatable {
    private struct Saved: Codable, Equatable {
        let context: NativeConsoleContext
        let destination: NativeConsoleDestination
    }
    private var saved: [Saved] = []
    private var active: NativeConsoleContext?
    var destination = NativeConsoleDestination()
    var savedContextCount: Int { saved.count }

    mutating func activate(_ context: NativeConsoleContext, legacyTab: String = "") {
        guard context != active else { return }
        if let active {
            saved.removeAll { $0.context == active }
            // A sheet is transient, not a command to reopen settings on every visit.
            var previous = destination
            previous.settings = nil
            saved.append(.init(context: active, destination: previous))
        }
        let next = saved.first { $0.context == context }?.destination
        destination = next ?? NativeConsoleDestination(legacyTab: active == nil ? legacyTab : "")
        saved = Array(saved.suffix(32))
        active = context
    }
}

enum NativeConsoleNextAction: Equatable {
    case connection, preparation, verifyStatus, storage, events

    static func resolve(coreReachable: Bool, projectSelected: Bool, statusCurrent: Bool,
                        armed: Bool, writesBlocked: Bool) -> Self {
        if !coreReachable { return .connection }
        if writesBlocked { return .storage }
        if !projectSelected { return .preparation }
        if !statusCurrent { return .verifyStatus }
        return armed ? .events : .preparation
    }
}

enum NativeConsoleFrequency {
    /// Explicit MHz, one decimal separator, at most six fractional digits.
    /// Integer arithmetic avoids introducing a display-to-device rounding error.
    static func hertz(_ text: String) -> String? {
        let parts = text.replacingOccurrences(of: ",", with: ".").split(separator: ".", omittingEmptySubsequences: false)
        guard (1...2).contains(parts.count), parts.allSatisfy({ !$0.isEmpty && $0.utf8.allSatisfy { (48...57).contains($0) } }),
              let whole = Int64(parts[0]), whole <= 800 else { return nil }
        let fraction = parts.count == 2 ? String(parts[1]) : ""
        guard fraction.count <= 6, let remainder = Int64(fraction + String(repeating: "0", count: 6 - fraction.count)) else { return nil }
        let value = whole * 1_000_000 + remainder
        guard (100_000...800_000_000).contains(value) else { return nil }
        return String(value)
    }
}
