import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import SpektrKomandy

@Suite(.serialized) struct WorkbenchArchiveTests {
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest) throws -> [String: Any])?
        struct UnknownRoute: Error {}
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do {
                let body = try Self.handler?(bufferedTestRequest(request)) ?? [:]
                let data = try JSONSerialization.data(withJSONObject: ["contract": "openradioinspector-platform", "contract_version": 1, "data": body])
                guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else { return }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data); client?.urlProtocolDidFinishLoading(self)
            } catch is UnknownRoute {
                guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: 404, httpVersion: nil, headerFields: nil) else { return }
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: Data(#"{"contract":"openradioinspector-platform","contract_version":1,"error":{"code":"not_found","message":"Route unavailable"}}"#.utf8))
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
        override func stopLoading() {}
    }
    private func api() throws -> (PlatformClient, URLSession) {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        return (try PlatformClient(baseURL: "https://example.test", session: session), session)
    }
    private var row: [String: Any] {
        ["id": "sweep", "session_id": "session", "stream_id": "tiny", "interval": ["started_at": "2026-11-14T12:00:00Z", "ended_at": "2026-11-14T12:00:05Z"],
         "point_count": 2, "level_unit": "dBm", "min_frequency_hz": 88000000, "max_frequency_hz": 108000000,
         "peak_frequency_hz": 88000000, "peak_level": -90.25]
    }
    @Test func paginatedSearchPreservesFiltersAndOpaqueCursor() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        Stub.handler = { request in
            #expect(request.httpMethod == "GET")
            #expect(request.url?.path == "/api/v1/sessions/session/sweep-summaries")
            let query = URLComponents(url: try #require(request.url), resolvingAgainstBaseURL: false)?.queryItems ?? []
            let values = Dictionary(uniqueKeysWithValues: query.map { ($0.name, $0.value ?? "") })
            #expect(values["stream_id"] == "tiny")
            #expect(values["limit"] == "32")
            #expect(values["after"] == "opaque+/=")
            #expect(values["min_hz"] == "88000000")
            return ["items": [row], "next_cursor": "next"]
        }
        let result = try await client.archiveSweepPage(sessionID: "session", streamID: "tiny", minimumHz: 88_000_000, after: "opaque+/=")
        #expect(result.items.count == 1); #expect(result.nextCursor == "next")
    }
    @Test func foreignStreamAndOversizedPageAreRejected() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        var wrong = row; wrong["stream_id"] = "other"
        let bad = wrong
        Stub.handler = { _ in ["items": [bad], "next_cursor": NSNull()] }
        await #expect(throws: (any Error).self) { try await client.archiveSweepPage(sessionID: "session", streamID: "tiny") }
        Stub.handler = { _ in ["items": Array(repeating: row, count: 33), "next_cursor": NSNull()] }
        await #expect(throws: (any Error).self) { try await client.archiveSweepPage(sessionID: "session", streamID: "tiny") }
    }
    @Test func originalRetainsRawLevelsUTCSettingsAndRejectsWrongStream() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        Stub.handler = { request in
            #expect(request.httpMethod == "GET")
            #expect(request.url?.path == "/api/v1/sessions/session/sweeps/sweep")
            return ["id": "sweep", "session_id": "session", "stream_id": "tiny",
                    "interval": row["interval"] ?? [:], "frequencies_hz": [88000000, 108000000],
                    "levels": [-90.25, -100.125], "level_unit": "dBm", "settings": ["rbw_hz": 10000, "sequence": 42]]
        }
        let source = try await client.archiveOriginalSweep(sessionID: "session", streamID: "tiny", sweepID: "sweep")
        #expect(source.levels == [-90.25, -100.125])
        #expect(source.frequenciesHz == [88_000_000, 108_000_000])
        #expect(source.settings?["sequence"] == .number(42))
        #expect(source.interval.endedAt == "2026-11-14T12:00:05Z")
        await #expect(throws: (any Error).self) { try await client.archiveOriginalSweep(sessionID: "session", streamID: "other", sweepID: "sweep") }
    }
    @Test func legacyRouteFallsBackToOriginalContext() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        Stub.handler = { request in
            if request.httpMethod == "GET" { throw Stub.UnknownRoute() }
            #expect(request.httpMethod == "POST")
            let bytes = try #require(request.httpBody)
            let object = try JSONSerialization.jsonObject(with: bytes)
            let sent = try #require(object as? [String: Any])
            #expect(sent["before"] as? Int == 0); #expect(sent["after"] as? Int == 0)
            return ["session_id": "session", "stream_id": "tiny", "anchor_id": "sweep", "order": "started_at_then_id",
                    "requested": ["before": 0, "after": 0], "available": ["before": 0, "after": 0],
                    "missing": ["before": 0, "after": 0], "complete": true,
                    "frequencies_hz": [88000000, 108000000], "level_unit": "dBm", "points": 2,
                    "items": [["id": "sweep", "offset": 0, "interval": row["interval"] ?? [:], "levels": [-90.25, -100.125],
                               "level_unit": "dBm", "settings": ["rbw_hz": 10000, "sequence": 42]]]]
        }
        let source = try await client.archiveOriginalSweep(sessionID: "session", streamID: "tiny", sweepID: "sweep")
        #expect(source.levels == [-90.25, -100.125])
        #expect(source.frequenciesHz == [88_000_000, 108_000_000])
        #expect(source.settings?["sequence"] == .number(42))
        #expect(source.interval.endedAt == "2026-11-14T12:00:05Z")
        await #expect(throws: (any Error).self) { try await client.archiveOriginalSweep(sessionID: "session", streamID: "other", sweepID: "sweep") }
    }
}
