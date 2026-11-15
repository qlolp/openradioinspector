import Foundation
import Testing
import SpektrKomandy

@Suite struct NativeAnalyzerAPITests {
    @Test func acquisitionOnlyCacheIsInspectedBeforeSettingsAreUsed() async throws {
        let host = "inspect-\(UUID().uuidString.lowercased()).example"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeNodeConnectionTests.Stub.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); NativeNodeConnectionTests.Stub.remove([host]) }
        NativeNodeConnectionTests.Stub.register(host: host) { stub in
            if stub.request.url?.path.hasSuffix("/settings") == true {
                try stub.reply(["revision": 0, "profile_verified": false, "settings": [:]])
            } else if stub.request.httpMethod == "POST" {
                #expect(stub.request.url?.path.hasSuffix("/operations") == true)
                try stub.reply(["operation_id": "inspect"], status: 202)
            } else {
                try stub.reply(["state": "succeeded", "context_current": true,
                    "result": ["status": ["revision": 0, "profile_verified": true, "settings": ["start_hz": 88_000_000]]]])
            }
        }
        let client = try PlatformClient(baseURL: "https://\(host)", session: session)
        let status = try await client.tinySASettings(sessionID: "s", streamID: "t")
        #expect(status["profile_verified"].bool == true)
        #expect(status["settings"]["start_hz"].integer == 88_000_000)
    }
    @Test func acknowledgedSettingReturnsOriginalRevisionAndFailureDoesNotSucceed() async throws {
        let host = "instrument-\(UUID().uuidString.lowercased()).example"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeNodeConnectionTests.Stub.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel(); NativeNodeConnectionTests.Stub.remove([host]) }
        NativeNodeConnectionTests.Stub.register(host: host) { stub in
            if stub.request.httpMethod == "POST" {
                #expect(stub.request.url?.path.hasSuffix("/instrument/settings") == true)
                try stub.reply(["operation_id": "original"], status: 202)
            } else {
                try stub.reply(["state": "succeeded", "context_current": true,
                                "result": ["revision": 42, "mode": "monitoring", "settings_readback": false]])
            }
        }
        let client = try PlatformClient(baseURL: "https://\(host)", session: session)
        let result = try await client.setTinySASetting(sessionID: "s", streamID: "t", name: "rbw_hz", value: .integer(30_000), revision: 41)
        #expect(result["revision"].integer == 42)
        #expect(result["settings_readback"].bool == false)
        NativeNodeConnectionTests.Stub.register(host: host) { stub in
            if stub.request.httpMethod == "POST" { try stub.reply(["operation_id": "original"], status: 202) }
            else { try stub.reply(["state": "failed", "error": "revision conflict"]) }
        }
        await #expect(throws: (any Error).self) {
            try await client.setTinySASetting(sessionID: "s", streamID: "t", name: "start_hz", value: .integer(96_000_000), revision: 41)
        }
    }
}
