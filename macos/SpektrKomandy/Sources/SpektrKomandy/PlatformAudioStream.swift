import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

private struct AudioStreamErrorEnvelope: Decodable, Sendable {
    struct Detail: Decodable, Sendable { let code: String; let message: String }
    let error: Detail
}

/// Runtime status of the network audio fan-out; delivery only, nothing recorded.
public struct PlatformAudioStreamStatus: Decodable, Equatable, Sendable {
    public let sessionId: String
    public let streamId: String
    public let subscribers: Int
    public let maxSubscribers: Int
    public let queuedBlocks: Int
    public let droppedBlocks: Int
    private enum CodingKeys: String, CodingKey, Sendable {
        case sessionId, streamId, subscribers, maxSubscribers, queuedBlocks, droppedBlocks
    }
    public init(from decoder: Decoder) throws {
        let v = try decoder.container(keyedBy: CodingKeys.self)
        sessionId = try v.decode(String.self, forKey: .sessionId)
        streamId = try v.decode(String.self, forKey: .streamId)
        subscribers = try v.decode(Int.self, forKey: .subscribers)
        maxSubscribers = try v.decode(Int.self, forKey: .maxSubscribers)
        queuedBlocks = try v.decode(Int.self, forKey: .queuedBlocks)
        droppedBlocks = try v.decode(Int.self, forKey: .droppedBlocks)
        func invalid() -> DecodingError {
            .dataCorruptedError(forKey: .subscribers, in: v, debugDescription: "Некорректное состояние сетевой трансляции")
        }
        guard !sessionId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamId.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              maxSubscribers > 0,
              subscribers >= 0, subscribers <= maxSubscribers,
              queuedBlocks >= 0, droppedBlocks >= 0 else { throw invalid() }
    }
}

/// One bounded PCM sample read from the live delivery stream.
public struct PlatformAudioStreamSample: Equatable, Sendable {
    public let pcm: Data
    public let sampleRateHz: Int
    public let sampleFormat: String
    public init(pcm: Data, sampleRateHz: Int, sampleFormat: String) {
        self.pcm = pcm
        self.sampleRateHz = sampleRateHz
        self.sampleFormat = sampleFormat
    }
}

extension PlatformClient {
    public func audioStreamStatus(sessionID: String, streamID: String) async throws -> PlatformAudioStreamStatus {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID аудиопотока")
        }
        return try await get("/sessions/\(Self.segment(sessionID))/audio/status",
                             query: [.init(name: "stream_id", value: streamID)], expectedStatuses: [200])
    }

    /// Bounded read of the endless PCM delivery: at most `maxBytes`, then the connection is dropped.
    /// Continuous consumption is a client loop; this verifies delivery without holding the stream.
    public func audioStreamSample(
        sessionID: String, streamID: String, maxBytes: Int = 65_536
    ) async throws -> PlatformAudioStreamSample {
        guard !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Нужен ID аудиопотока")
        }
        guard (1...262_144).contains(maxBytes) else {
            throw PlatformError.invalidCommand("Размер выборки должен быть в диапазоне 1…262144 байт")
        }
        var request = URLRequest(url: try baseURL.endpoint(
            path: "/sessions/\(Self.segment(sessionID))/audio/stream",
            query: [.init(name: "stream_id", value: streamID)]))
        request.httpMethod = "GET"
        request.timeoutInterval = 5
        request.setValue("audio/pcm", forHTTPHeaderField: "Accept")
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await receivePrefix(request, maxBytes: maxBytes)
        } catch let error as PlatformError {
            if Task.isCancelled { throw CancellationError() }
            throw error
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                throw CancellationError()
            }
            throw PlatformError.connection(error.localizedDescription)
        }
        try Task.checkCancellation()
        guard let http = response as? HTTPURLResponse else {
            throw PlatformError.invalidResponse("Ответ сервера не получен")
        }
        guard (200...299).contains(http.statusCode) else {
            if let envelope = try? JSONDecoder().decode(AudioStreamErrorEnvelope.self, from: data) {
                throw PlatformError.http(
                    status: http.statusCode, code: envelope.error.code, message: envelope.error.message
                )
            }
            throw PlatformError.http(status: http.statusCode, code: "http_error", message: "HTTP \(http.statusCode)")
        }
        let contentType = http.value(forHTTPHeaderField: "Content-Type") ?? ""
        guard contentType.contains("audio/pcm") else {
            throw PlatformError.invalidResponse("Сервер не отдаёт audio/pcm")
        }
        let format = http.value(forHTTPHeaderField: "X-Sample-Format") ?? ""
        guard format == "pcm16le" else {
            throw PlatformError.invalidResponse("Формат отсчётов не pcm16le")
        }
        guard let rate = Int(http.value(forHTTPHeaderField: "X-Sample-Rate") ?? ""), rate > 0 else {
            throw PlatformError.invalidResponse("Не указана частота дискретизации")
        }
        return PlatformAudioStreamSample(
            pcm: Data(data.prefix(maxBytes)), sampleRateHz: rate, sampleFormat: format)
    }
}
