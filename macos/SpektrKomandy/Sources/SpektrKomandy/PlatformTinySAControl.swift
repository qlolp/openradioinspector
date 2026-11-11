import Foundation

/// Scope of the shared instrument UI. USB and measurement logic stay in core.
public struct PlatformTinySAControlContext: Equatable, Sendable {
    public let server: PlatformBaseURL
    public let sessionID: String
    public let streamID: String

    public init(server: String, sessionID: String, streamID: String) throws {
        self.server = try PlatformBaseURL(server)
        guard !sessionID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !streamID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            throw PlatformError.invalidCommand("Выберите сеанс и tinySA serial-поток")
        }
        self.sessionID = sessionID; self.streamID = streamID
    }

    public func url() throws -> URL {
        var components = URLComponents(url: server.root, resolvingAgainstBaseURL: false)
        components?.path = server.root.path + "/instrument"
        components?.queryItems = [URLQueryItem(name: "session_id", value: sessionID),
                                  URLQueryItem(name: "stream_id", value: streamID)]
        guard let value = components?.url else { throw PlatformError.invalidURL("Не удалось открыть панель tinySA") }
        return value
    }
}
