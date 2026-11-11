import Foundation

private struct PhonoKey: CodingKey, Sendable {
    let stringValue: String
    let intValue: Int? = nil
    init?(stringValue: String) { self.stringValue = stringValue }
    init?(intValue: Int) { return nil }
}

private func phonoExactKeys(_ decoder: Decoder, _ expected: Set<String>) throws {
    let values = try decoder.container(keyedBy: PhonoKey.self)
    guard Set(values.allKeys.map(\.stringValue)) == expected else {
        throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unexpected phonogram fields"))
    }
}

/// One completed phonogram: the split between pre-history and recorded part.
public struct PlatformPhonogram: Decodable, Equatable, Sendable {
    public let sampleRateHz: Int
    public let preHistorySamples: Int
    public let recordedSamples: Int
    public let preHistorySeconds: Double
    public let recordedSeconds: Double
    public let durationSeconds: Double
    public let startedAt: String
    public let endedAt: String
    private enum CodingKeys: String, CodingKey, Sendable {
        case sampleRateHz, preHistorySamples, recordedSamples
        case preHistorySeconds, recordedSeconds, durationSeconds
        case startedAt, endedAt
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sampleRateHz = try v.decode(Int.self, forKey: .sampleRateHz)
        preHistorySamples = try v.decode(Int.self, forKey: .preHistorySamples)
        recordedSamples = try v.decode(Int.self, forKey: .recordedSamples)
        preHistorySeconds = try v.decode(Double.self, forKey: .preHistorySeconds)
        recordedSeconds = try v.decode(Double.self, forKey: .recordedSeconds)
        durationSeconds = try v.decode(Double.self, forKey: .durationSeconds)
        startedAt = try v.decode(String.self, forKey: .startedAt)
        endedAt = try v.decode(String.self, forKey: .endedAt)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .durationSeconds, in: v, debugDescription: "Некорректная фонограмма")
        }
        guard sampleRateHz > 0, preHistorySamples >= 0, recordedSamples >= 0,
              preHistorySeconds.isFinite, recordedSeconds.isFinite, durationSeconds.isFinite,
              preHistorySeconds >= 0, recordedSeconds >= 0,
              abs(durationSeconds - (preHistorySeconds + recordedSeconds)) < 1e-6,
              !startedAt.isEmpty, !endedAt.isEmpty else { throw invalid() }
    }
    public static func sameID(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return a == b }
        return Data(a.utf8) == Data(b.utf8)
    }
}

/// Runtime recorder status; the archive is never written.
public struct PlatformPhonogramStatus: Decodable, Equatable, Sendable {
    public static let preHistorySeconds = 5.0
    public let sessionId: String
    public let streamId: String
    public let sampleRateHz: Int
    public let recording: Bool
    public let preHistorySecondsField: Double
    public let bufferedSeconds: Double
    public let recordedSeconds: Double
    public let startedAt: String?
    public let last: PlatformPhonogram?
    private enum CodingKeys: String, CodingKey, Sendable {
        case sessionId, streamId, sampleRateHz, recording, preHistorySeconds
        case bufferedSeconds, recordedSeconds, startedAt, last
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        sampleRateHz = try v.decode(Int.self, forKey: .sampleRateHz)
        recording = try v.decode(Bool.self, forKey: .recording)
        preHistorySecondsField = try v.decode(Double.self, forKey: .preHistorySeconds)
        bufferedSeconds = try v.decode(Double.self, forKey: .bufferedSeconds)
        recordedSeconds = try v.decode(Double.self, forKey: .recordedSeconds)
        startedAt = try v.decodeIfPresent(String.self, forKey: .startedAt)
        last = try v.decodeIfPresent(PlatformPhonogram.self, forKey: .last)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .bufferedSeconds, in: v, debugDescription: "Некорректное состояние фонограммы")
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              sampleRateHz > 0,
              abs(preHistorySecondsField - Self.preHistorySeconds) < 1e-9,
              bufferedSeconds.isFinite, recordedSeconds.isFinite,
              bufferedSeconds >= 0, recordedSeconds >= 0 else { throw invalid() }
    }
}

extension PlatformClient {
    public func phonogramStatus(sessionID: String, streamID: String) async throws -> PlatformPhonogramStatus {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID аудиопотока")
        }
        return try await get("/sessions/\(Self.segment(sessionID))/phonogram/status",
                             query: [.init(name: "stream_id", value: streamID)], expectedStatuses: [200])
    }
    /// Start freezes the five-second buffer window as the recording pre-history.
    public func phonogramStart(sessionID: String, streamID: String) async throws -> PlatformPhonogramStatus {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID аудиопотока")
        }
        return try await post("/sessions/\(Self.segment(sessionID))/phonogram/start",
                              body: ["stream_id": streamID], expectedStatuses: [200])
    }
    /// Stop returns the finished split; runtime state only, nothing enters the archive.
    public func phonogramStop(sessionID: String, streamID: String) async throws -> PlatformPhonogramStatus {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID аудиопотока")
        }
        return try await post("/sessions/\(Self.segment(sessionID))/phonogram/stop",
                              body: ["stream_id": streamID], expectedStatuses: [200])
    }
}
