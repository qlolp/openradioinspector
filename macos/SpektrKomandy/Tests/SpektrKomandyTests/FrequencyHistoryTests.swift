import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import SpektrKomandy

@Suite(.serialized) struct FrequencyHistoryTests {
    // Swift Testing runs different suites concurrently. Keep this protocol's
    // handler independent of the archive suite, even though each is serialized.
    final class Stub: URLProtocol {
        nonisolated(unsafe) static var handler: ((URLRequest) throws -> [String: Any])?
        nonisolated(unsafe) static var status = 200
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            do {
                let body = try Self.handler?(bufferedTestRequest(request)) ?? [:]
                let data = try JSONSerialization.data(withJSONObject: ["contract":"openradioinspector-platform","contract_version":1,"data":body])
                guard let url = request.url, let response = HTTPURLResponse(url:url,statusCode:Self.status,httpVersion:nil,headerFields:nil) else { return }
                client?.urlProtocol(self,didReceive:response,cacheStoragePolicy:.notAllowed)
                client?.urlProtocol(self,didLoad:data); client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self,didFailWithError:error) }
        }
        override func stopLoading() {}
    }
    private func api() throws -> (PlatformClient, URLSession) {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [Stub.self]
        let session = URLSession(configuration: config)
        return (try PlatformClient(baseURL: "https://example.test", session: session), session)
    }
    private let start = "2026-11-15T00:00:00Z", end = "2026-11-15T01:00:00Z"
    private var page: [String: Any] {
        ["session_id":"s", "stream_id":"t", "frequencies_hz":[150,450], "start":start,"end":end,
         "next_cursor": NSNull(), "items":[["sweep_id":"a", "interval":["started_at":start,"ended_at":"2026-11-15T00:00:01Z"],
            "level_unit":"dBm","segment_id":String(repeating:"a",count:64),
            "samples":[["requested_hz":150,"frequency_hz":100,"level":-80.25],
                       ["requested_hz":450,"frequency_hz":NSNull(),"level":NSNull()]]]]]
    }
    @Test func preservesBinsGapsAndIntervalsAndRejectsForeignScope() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        Stub.handler = { request in
            #expect(request.httpMethod == "POST")
            #expect(request.url?.path == "/api/v1/sessions/s/frequency-history")
            return page
        }
        let result = try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        #expect(result.items[0].samples[0].frequencyHz == 100)
        #expect(result.items[0].samples[0].level == -80.25)
        #expect(result.items[0].samples[1].level == nil)
        await #expect(throws:(any Error).self) {
            try await client.frequencyHistory(sessionID:"s",streamID:"other",frequencies:[150,450],start:start,end:end)
        }
    }
    @Test func badSelectionNeverSendsAndMalformedRowsAreRejected() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        Stub.handler = { _ in Issue.record("Invalid request sent"); return [:] }
        for frequencies: [Int64] in [[],[1,1],[1,2,3,4,5],[-1]] {
            await #expect(throws:(any Error).self) {
                try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:frequencies,start:start,end:end)
            }
        }
        var bad = page; bad["frequencies_hz"] = [151,450]; let response = bad
        Stub.handler = { _ in response }
        await #expect(throws:(any Error).self) {
            try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        }
    }
    @Test func portableBookmarksRoundTripAndRejectCommands() throws {
        let source = Data(#"{"schema_version":1,"name":"Пост","entries":[{"label":"Канал","frequency_hz":100000001}]}"#.utf8)
        let document = try PlatformFrequencyBookmarkDocument.importData(source)
        #expect(document.entries[0].frequencyHz == 100_000_001)
        #expect(try PlatformFrequencyBookmarkDocument.importData(document.exportData()).name == "Пост")
        let command = Data(#"{"schema_version":1,"name":"Пост","entries":[{"label":"Канал","frequency_hz":100000001,"action":"tune"}]}"#.utf8)
        #expect(throws:(any Error).self) { try PlatformFrequencyBookmarkDocument.importData(command) }
    }

    @Test func saveIsMetadataOnlyAndCapabilitiesAndRecoveryAreScoped() async throws {
        let (client, session) = try api()
        defer { session.invalidateAndCancel(); Stub.handler = nil; Stub.status = 200 }
        let entries: [[String:Any]] = [["label":"Канал","frequency_hz":150]]
        let saved: [String:Any] = ["id":"v1","session_id":"s","schema_version":1,"name":"Пост",
            "revision":1,"created_at":start,"entries":entries]
        Stub.status = 201
        Stub.handler = { request in
            #expect(request.url?.path == "/api/v1/sessions/s/frequency-bookmarks")
            let bytes = try #require(request.httpBody)
            let body = try #require(JSONSerialization.jsonObject(with:bytes) as? [String:Any])
            #expect(Set(body.keys) == ["name","entries"])
            return saved
        }
        let result = try await client.saveFrequencyBookmarks(sessionID:"s",document:.init(name:"Пост",entries:[.init(label:"Канал",frequencyHz:150)]))
        #expect(result.revision == 1)
        Stub.status = 200
        Stub.handler = { _ in ["features":["frequency_history":true,"frequency_bookmarks":"true"]] }
        #expect(try await client.workbenchFeatures() == ["frequency_history"])
        Stub.handler = { request in
            #expect(request.url?.path == "/api/v1/sessions/s/streams/t/recover")
            return ["session_id":"foreign","stream_id":"t","state":"running","alive":true]
        }
        await #expect(throws:(any Error).self) { try await client.recoverStream(sessionID:"s",streamID:"t") }
    }

    @Test func unicodeDistinctSourceIDsRemainDistinctInSelection() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        var payload = page
        let rows = try #require(payload["items"] as? [[String:Any]])
        var composed = rows[0], decomposed = rows[0]
        composed["sweep_id"] = "é"; decomposed["sweep_id"] = "e\u{0301}"
        payload["items"] = [composed,decomposed]; let response = payload
        Stub.handler = { _ in response }
        let result = try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        #expect(Set(result.items.map(\.id)).count == 2)
    }
    @Test func submillisecondSourceOrderAndIntervalsRemainExact() async throws {
        let (client, session) = try api(); defer { session.invalidateAndCancel(); Stub.handler = nil }
        var payload = page
        let sourceRows = try #require(payload["items"] as? [[String:Any]])
        var newer = sourceRows[0], older = sourceRows[0]
        newer["sweep_id"] = "a"; older["sweep_id"] = "z"
        newer["interval"] = ["started_at":"2026-11-15T00:00:00.000900Z","ended_at":"2026-11-15T00:00:00.000950Z"]
        older["interval"] = ["started_at":"2026-11-15T00:00:00.000100Z","ended_at":"2026-11-15T00:00:00.000150Z"]
        payload["items"] = [newer,older]; let valid = payload
        Stub.handler = { _ in valid }
        let result = try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        #expect(result.items.map(\.sweepId) == ["a","z"])
        newer["interval"] = ["started_at":"2026-11-15T00:00:00.000900Z","ended_at":"2026-11-15T00:00:00.000100Z"]
        payload["items"] = [newer]; let inverted = payload
        Stub.handler = { _ in inverted }
        await #expect(throws:(any Error).self) {
            try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        }
        payload = valid; payload["start"] = "2026-11-15T00:00:00.000001Z"; let shiftedWindow = payload
        Stub.handler = { _ in shiftedWindow }
        await #expect(throws:(any Error).self) {
            try await client.frequencyHistory(sessionID:"s",streamID:"t",frequencies:[150,450],start:start,end:end)
        }
    }

}
