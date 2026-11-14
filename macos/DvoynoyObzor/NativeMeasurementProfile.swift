import Foundation

/// Only receive parameters; never contains credentials, serial paths, or a generator command.
struct NativeMeasurementProfile: Codable, Equatable {
    let version: Int
    let name: String
    let startHz: Int64
    let stopHz: Int64
    let rbw: String

    init(name: String, draft: NativeAnalyzerDraft, edited: NativeAnalyzerDraft.Field) throws {
        var normalized = draft
        try normalized.synchronize(edited)
        version = 1
        self.name = name.trimmingCharacters(in: .whitespacesAndNewlines)
        startHz = try NativeAnalyzerDraft.hz(normalized.start)
        stopHz = try NativeAnalyzerDraft.hz(normalized.stop)
        rbw = normalized.rbw
        try validate()
    }
    func validate() throws {
        guard version == 1, !name.isEmpty, name.count <= 80,
              startHz >= 100_000, stopHz <= 800_000_000, startHz < stopHz,
              ["auto", "200", "1000", "3000", "10000", "30000", "100000", "300000", "600000", "850000"].contains(rbw) else {
            throw NativeAnalyzerDraft.DraftError.invalid("Неверный или неподдерживаемый профиль приёма")
        }
    }
    func draft() throws -> NativeAnalyzerDraft {
        try validate()
        var value = NativeAnalyzerDraft()
        value.start = NativeAnalyzerDraft.mhz(startHz); value.stop = NativeAnalyzerDraft.mhz(stopHz)
        value.rbw = rbw; try value.synchronize(.start)
        return value
    }
    static func decode(_ data: Data) throws -> Self {
        guard data.count <= 16_384 else { throw NativeAnalyzerDraft.DraftError.invalid("Профиль превышает 16 КБ") }
        let value = try JSONDecoder().decode(Self.self, from: data)
        try value.validate(); return value
    }
}
