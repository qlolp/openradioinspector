import Foundation
import Testing
@testable import SpektrKomandy

@Suite(.serialized)
struct SweepOccupancyTests {
    private func fixture() throws -> Data {
        var url = URL(fileURLWithPath: #filePath)
        for _ in 0..<5 { url.deleteLastPathComponent() }
        return try Data(contentsOf: url.appendingPathComponent("tests/fixtures/sweep-occupancy-v1.json"))
    }

    @Test func coreFixtureAndLosslessExport() throws {
        let bytes = try fixture(), result = try PlatformSweepOccupancy.validatedResponse(bytes)
        #expect(result.occupancy.exceedanceCounts == [2, 2, 2])
        #expect(result.occupancy.sampleFractions == [0.4, 0.4, 0.4])
        #expect(result.occupancy.acquisitionGapCount == 4)
        #expect(result.context.items.count == 5)
        let output = try result.exportData()
        let text = String(decoding: output, as: UTF8.self)
        #expect(text.contains("9007199254740993"))
        #expect(text.contains("key_with_underscores"))
        #expect(output.range(of: try platformRawPayload(bytes)) != nil)
        let envelope = try #require(JSONSerialization.jsonObject(with: output) as? [String: Any])
        #expect(envelope["format"] as? String == "openradioinspector-sweep-occupancy")
        #expect(envelope["format_version"] as? Int == 1)
    }

    @Test func rejectsMalformedSummariesAndDuplicateKeys() throws {
        let raw = try fixture()
        let original = try #require(JSONSerialization.jsonObject(with: raw) as? [String: Any])
        let mutations: [(String, Any)] = [
            ("method", "rf_time"), ("comparison", "greater_or_equal"), ("sample_count", 4),
            ("exceedance_counts", [6, 2, 2]), ("sample_fractions", [0.4, 0, 0.4]),
            ("acquisition_span_seconds", -1), ("acquisition_covered_seconds", 9),
            ("acquisition_gap_seconds", 0), ("acquisition_gap_count", 5), ("rf_time_occupancy", 0),
        ]
        for (key, value) in mutations {
            var envelope = original
            var data = try #require(envelope["data"] as? [String: Any])
            var summary = try #require(data["occupancy"] as? [String: Any])
            summary[key] = value; data["occupancy"] = summary; envelope["data"] = data
            let bytes = try JSONSerialization.data(withJSONObject: envelope)
            #expect(throws: (any Error).self) { try PlatformSweepOccupancy.validatedResponse(bytes) }
        }
        let duplicate = String(decoding: raw, as: UTF8.self).replacingOccurrences(of: "\"sample_count\": 5", with: "\"sample_count\": 5, \"sample_count\": 5")
        #expect(throws: (any Error).self) { try PlatformSweepOccupancy.validatedResponse(Data(duplicate.utf8)) }
    }

    @Test func decodedOnlyResultCannotClaimLosslessExport() throws {
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let result = try decoder.decode(PlatformSweepOccupancy.self, from: platformRawPayload(fixture()))
        #expect(throws: (any Error).self) { try result.exportData() }
    }

    private final class Stub: URLProtocol {
        static var response = Data()
        static var lastRequest: URLRequest?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() {
            Self.lastRequest = request
            guard let url = request.url, let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil) else { return }
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Self.response)
            client?.urlProtocolDidFinishLoading(self)
        }
        override func stopLoading() {}
    }

    @Test func clientCorrelatesThresholdAndWindowAndRetainsWireJSON() async throws {
        Stub.response = try fixture(); Stub.lastRequest = nil
        let configuration = URLSessionConfiguration.ephemeral; configuration.protocolClasses = [Stub.self]
        let client = try PlatformClient(baseURL: "https://example.test", session: URLSession(configuration: configuration))
        let result = try await client.sweepOccupancy(sessionID: "session", sweepID: "sweep-3", before: 2, after: 2, thresholdDbm: -97)
        #expect(Stub.lastRequest?.url?.path == "/api/v1/sessions/session/sweep-occupancy")
        #expect(Stub.lastRequest?.httpMethod == "POST")
        #expect(String(decoding: try result.exportData(), as: UTF8.self).contains("9007199254740993"))
        for threshold in [-96.0, .infinity, .nan] {
            await #expect(throws: (any Error).self) {
                try await client.sweepOccupancy(sessionID: "session", sweepID: "sweep-3", before: 2, after: 2, thresholdDbm: threshold)
            }
        }
        await #expect(throws: (any Error).self) {
            try await client.sweepOccupancy(sessionID: "session", sweepID: "other", before: 2, after: 2, thresholdDbm: -97)
        }
    }
}
