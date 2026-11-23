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

    @Test func panoramaKeepsHolesAndOpensTheHeldPeakFromItsOriginalPass() async throws {
        let (client, session) = try api()
        defer { session.invalidateAndCancel(); Stub.handler = nil }
        let start = "2026-11-22T12:00:01Z", end = "2026-11-22T12:00:01.012345Z"
        let prior = "2026-11-22T11:59:01Z", priorEnd = "2026-11-22T11:59:01.012345Z"
        let summary: [String: Any] = ["pass_id": "new", "pass": 2, "start_hz": 80_000_000,
            "stop_hz": 83_350_000, "step_hz": 10_000, "tiles": 3, "received_tiles": 2, "unique_points": 224,
            "expected_points": 336, "started_at": start, "ended_at": end, "complete": false,
            "duration_s": NSNull(), "revisit_s": 60.0, "active_board": "main", "active_input": "left",
            "rbw_hz": 15_000, "detector": "normal", "offset_db": 0, "simulated": true]
        let current: [String: Any] = ["frequency_hz": 80_010_000, "level_dbm": -95, "sweep_id": "now",
            "point_index": 1, "started_at": start, "ended_at": end, "last_measured_at": end, "tile": 1]
        let held: [String: Any] = ["frequency_hz": 80_010_000, "level_dbm": -82.5, "sweep_id": "original",
            "point_index": 1, "started_at": prior, "ended_at": priorEnd, "last_measured_at": end, "tile": 1]
        let last: [String: Any] = ["frequency_hz": 82_240_000, "level_dbm": -90, "sweep_id": "last",
            "point_index": 0, "started_at": start, "ended_at": end, "last_measured_at": end, "tile": 3]
        let coverage = [["start_hz": 80_000_000, "stop_hz": 81_110_000], ["start_hz": 82_240_000, "stop_hz": 83_350_000]]
        Stub.handler = { request in
            let url = try #require(request.url)
            #expect(request.httpMethod == "GET")
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let values = Dictionary(uniqueKeysWithValues: query.map { ($0.name, $0.value ?? "") })
            switch url.path {
            case "/api/v1/sessions/s/streams/r/rf-explorer/passes":
                #expect(values["before"] == "opaque+/=")
                return ["items": [summary], "next_before": "older"]
            case "/api/v1/sessions/s/streams/r/rf-explorer/passes/new":
                #expect(values["columns"] == "3")
                #expect(values["hold_since"] == prior)
                return ["summary": summary, "columns": [current, NSNull(), last], "hold_columns": [held, NSNull(), last],
                        "coverage": coverage, "hold_coverage": coverage]
            case "/api/v1/sessions/s/sweeps/original":
                return ["id": "original", "session_id": "s", "stream_id": "r", "level_unit": "dBm",
                    "interval": ["started_at": prior, "ended_at": priorEnd], "frequencies_hz": [80_000_000, 80_010_000],
                    "levels": [-93, -82.5], "settings": ["survey": ["pass_id": "old", "tile": 1]]]
            case "/api/v1/sessions/s/streams/r/rf-explorer/passes/old/windows":
                #expect(values["after_tile"] == "0")
                return ["items": [["tile": 1, "sweep_id": "original", "started_at": prior, "ended_at": priorEnd],
                    ["tile": 3, "sweep_id": "later", "started_at": start, "ended_at": end]], "next_after_tile": NSNull()]
            default: throw Stub.UnknownRoute()
            }
        }
        let page = try await client.rfExplorerPasses(sessionID: "s", streamID: "r", before: "opaque+/=")
        #expect(page.nextBefore == "older")
        #expect(page.items.first?.receivedTiles == 2)
        let panorama = try await client.rfExplorerPass(sessionID: "s", streamID: "r", passID: "new", columns: 3, holdSince: prior)
        #expect(panorama.summary.durationS == nil && panorama.summary.revisitS == 60)
        #expect(panorama.gaps == [81_120_000...82_230_000])
        #expect(panorama.columns[1] == nil)
        #expect(panorama.point(at: 81_800_000, held: true) == nil)
        #expect(panorama.point(at: 80_000_000, held: true)?.sweepId == "original")
        #expect(panorama.point(at: 82_260_000, held: false)?.sweepId == "last")
        let peak = try #require(panorama.holdColumns?.first ?? nil)
        let raw = try await client.archiveOriginalSweep(sessionID: "s", streamID: "r", sweepID: peak.sweepId)
        try peak.validate(source: raw)
        #expect(throws: PlatformError.self) { try panorama.columns[0]?.validate(source: raw) }
        let windows = try await client.rfExplorerWindows(sessionID: "s", streamID: "r", passID: "old", afterTile: 0)
        #expect(windows.items.map(\.tile) == [1, 3])
        #expect(try windows.items[0].seconds(until: windows.items[1]) == 60)
    }

    @Test func archiveAnalysisKeepsTheSelectedSweepRBWAndUncoveredBins() async throws {
        let (client, session) = try api()
        defer { session.invalidateAndCancel(); Stub.handler = nil }
        let interval = ["started_at": "2026-11-22T01:02:03Z", "ended_at": "2026-11-22T01:02:03.250000Z"]
        func original(_ id: String, start: Int, levels: [Double]) -> [String: Any] {
            let frequencies = (0..<levels.count).map { start + 10_000 * $0 }
            let settings: [String: Any] = ["adapter": "rf-explorer-3g-serial", "device_mode": "spectrum_analyzer",
                "receive_profile": "rf-explorer-3g-combo-standard-1.34", "device_identity": ["main_model": 3, "expansion_model": 5, "firmware": "01.34"],
                "active_board": "expansion", "active_input": "right", "board_min_hz": 15_000_000, "board_max_hz": 2_700_000_000,
                "board_max_span_hz": 600_000_000, "amplitude_bottom_dbm": -120, "amplitude_top_dbm": -10,
                "start_hz": start, "stop_hz": frequencies.last!, "step_hz": 10_000, "points": 16,
                "rbw_hz": 15_000, "offset_db": 0, "detector": "normal", "detector_code": 0, "artifact_kind": "raw_spectrum",
                "settings_source": "device_readback", "settings_readback": true, "simulated": true,
                "level_encoding": "dBm = offset_db - u8 / 2", "timing_source": "host_frame_read"]
            return ["id": id, "session_id": "s", "stream_id": "r", "interval": interval,
                    "level_unit": "dBm", "frequencies_hz": frequencies, "levels": levels, "settings": settings]
        }
        let raw = original("raw", start: 80_000_000, levels: [-78] + Array(repeating: -81, count: 15))
        let ref = original("reference", start: 79_990_000, levels: Array(repeating: -81, count: 16))
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let source = try decoder.decode(PlatformSweep.self, from: JSONSerialization.data(withJSONObject: raw))
        let reference = try decoder.decode(PlatformSweep.self, from: JSONSerialization.data(withJSONObject: ref))
        let power: [String: Any] = ["source": raw, "start_index": 0, "stop_index": 2, "start_hz": 80_000_000,
            "stop_hz": 80_020_000, "rbw_hz": 15000, "unit": "dBm", "value": -79.2, "integration": "trapezoid_over_actual_grid",
            "enbw_correction": "not_calibrated", "uncertainty_db": NSNull(), "hardware_validation": "not_run"]
        let bins: [[String: Any]] = (0..<16).map { i in
            if i == 15 {
                return ["frequency_hz": 80_150_000, "target_index": 15, "reference_index": NSNull(),
                        "lower_dbm": NSNull(), "upper_dbm": NSNull(), "delta_db": NSNull(), "classification": "uncovered"]
            }
            return ["frequency_hz": 80_000_000 + i * 10_000, "target_index": i, "reference_index": i + 1,
                    "lower_dbm": -84, "upper_dbm": -79, "delta_db": i == 0 ? 3 : 0, "classification": i == 0 ? "above" : "within"]
        }
        Stub.handler = { request in
            #expect(request.httpMethod == "POST")
            let body = try JSONSerialization.jsonObject(with: #require(request.httpBody)) as? [String: Any]
            switch request.url?.path {
            case "/api/v1/sessions/s/rf-explorer-band-power":
                #expect(body?["sweep_id"] as? String == "raw" && body?["stop_hz"] as? Int == 80_020_000)
                return power
            case "/api/v1/sessions/s/sweep-tolerance-envelope":
                #expect(body?["reference_id"] as? String == "reference" && body?["target_id"] as? String == "raw")
                #expect(body?["upper_deviation_db"] as? Double == 2 && body?["lower_deviation_db"] as? Double == 3)
                return ["session_id": "s", "stream_id": "r", "reference": ref, "target": raw, "level_unit": "dBm",
                    "delta_unit": "dB", "upper_deviation_db": 2, "lower_deviation_db": 3, "bins": bins,
                    "counts": ["above": 1, "below": 0, "within": 14, "uncovered": 1]]
            default: throw Stub.UnknownRoute()
            }
        }
        let powerResult = try await client.rfExplorerBandPower(source: source, startHz: 80_000_000, stopHz: 80_020_000)
        #expect(powerResult.value == -79.2 && powerResult.rbw_hz == 15_000 && powerResult.uncertainty_db == nil)
        let envelope = try await client.sweepTolerance(reference: reference, target: source, upperDb: 2, lowerDb: 3)
        #expect(envelope.bins.last?.classification == .uncovered && envelope.bins.last?.upper_dbm == nil)
        #expect(envelope.counts.above == 1 && envelope.counts.uncovered == 1)
        Stub.handler = { _ in power }
        await #expect(throws: PlatformError.self) { try await client.rfExplorerBandPower(source: reference, startHz: 79_990_000, stopHz: 80_010_000) }
    }

    @Test func exactNamedMarkerResolvesItsOwnFrequencyEvenIfTheDisplayColumnHasAStrongerNeighbour() async throws {
        let (client, session) = try api()
        defer { session.invalidateAndCancel(); Stub.handler = nil }
        let at = "2026-11-22T01:02:03.123456Z"
        let point: [String: Any] = ["frequency_hz": 100_000_000, "level_dbm": -98, "sweep_id": "exact/100",
            "point_index": 4, "started_at": at, "ended_at": at, "last_measured_at": at, "tile": 2]
        let stronger = point.merging(["frequency_hz": 101_000_000, "level_dbm": -71]) { _, new in new }
        Stub.handler = { request in
            let url = try #require(request.url)
            #expect(request.httpMethod == "GET" && url.path == "/api/v1/sessions/s/streams/r/rf-explorer/passes/run-1/point")
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            #expect(items.contains(.init(name: "frequency_hz", value: "100000000")))
            #expect(items.contains(.init(name: "hold_since", value: at)))
            return ["current": point, "held": point]
        }
        let exact = try await client.rfExplorerMarker(sessionID: "s", streamID: "r", passID: "run-1", frequencyHz: 100_000_000, holdSince: at)
        #expect(exact.current?.frequencyHz == 100_000_000 && exact.held?.sweepId == "exact/100")
        Stub.handler = { _ in ["current": stronger, "held": NSNull()] }
        await #expect(throws: PlatformError.self) {
            try await client.rfExplorerMarker(sessionID: "s", streamID: "r", passID: "run-1", frequencyHz: 100_000_000)
        }
        Stub.handler = { _ in ["current": NSNull(), "held": NSNull()] }
        let gap = try await client.rfExplorerMarker(sessionID: "s", streamID: "r", passID: "run-1", frequencyHz: 100_000_000)
        #expect(gap.current == nil && gap.held == nil)
    }
}
