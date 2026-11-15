import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@MainActor @Suite(.serialized) struct NativeFrequencyHistoryTests {
    private func page(_ ids: [String], cursor: String? = nil, time: String = "2026-11-15T00:00:00Z") throws -> PlatformFrequencyHistoryPage {
        let rows = ids.enumerated().map { index, id in
            ["sweep_id":id,"interval":["started_at":time,"ended_at":"2026-11-15T00:00:01Z"],
             "level_unit":"dBm","segment_id":String(repeating:"a",count:64),
             "samples":[["requested_hz":100,"frequency_hz":100,"level":-80]]] as [String:Any]
        }
        let data = try JSONSerialization.data(withJSONObject:["session_id":"s","stream_id":"t","frequencies_hz":[100],
            "start":"2026-11-15T00:00:00Z","end":"2026-11-15T01:00:00Z","items":rows,"next_cursor":cursor as Any? ?? NSNull()])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformFrequencyHistoryPage.self,from:data)
    }
    @Test func resetDiscardsLateResponse() async throws {
        let browser = NativeFrequencyHistoryBrowser()
        var resume: CheckedContinuation<PlatformFrequencyHistoryPage, any Error>?
        let request = Task { await browser.load { _ in try await withCheckedThrowingContinuation { resume = $0 } } }
        while resume == nil { await Task.yield() }
        browser.reset()
        resume?.resume(returning:try page(["old"],cursor:"old-cursor"))
        await request.value
        #expect(browser.rows.isEmpty); #expect(browser.nextCursor == nil); #expect(!browser.busy)
    }
    @Test func duplicatePageDoesNotAppendAndCacheIsBounded() async throws {
        let browser = NativeFrequencyHistoryBrowser(maximumRows: 2)
        await browser.load { _ in try page(["z"],cursor:"next") }
        await browser.load(next:true) { _ in try page(["z"],cursor:"again") }
        #expect(browser.rows.count == 1)
        #expect(!browser.message.isEmpty)
        await browser.load(next:true) { _ in try page(["y"],cursor:"more") }
        #expect(browser.rows.count == 2); #expect(browser.nextCursor == nil)
        #expect(browser.message.contains("предел"))
    }
    @Test func submillisecondOrderingAcrossPagesUsesSourcePrecision() async throws {
        let browser = NativeFrequencyHistoryBrowser()
        await browser.load { _ in try page(["a"],cursor:"next",time:"2026-11-15T00:00:00.000900Z") }
        await browser.load(next:true) { _ in try page(["z"],time:"2026-11-15T00:00:00.000100Z") }
        #expect(browser.rows.map(\.sweepId) == ["a","z"])
        #expect(browser.nextCursor == nil)
    }
    @Test func importedArchiveFrequenciesSurviveDisplayAndSaveBeyondTuningRange() throws {
        let source = Data(#"{"schema_version":1,"name":"Архив","entries":[{"label":"ГГц","frequency_hz":1000000001},{"label":"Предел","frequency_hz":9007199254740991}]}"#.utf8)
        let imported = try PlatformFrequencyBookmarkDocument.importData(source)
        let edits = try imported.entries.map { entry in
            PlatformFrequencyBookmarkEntry(label:entry.label,frequencyHz:try NativeHistoryFrequencyInput.hz(NativeHistoryFrequencyInput.mhz(entry.frequencyHz)))
        }
        #expect(edits == imported.entries)
        let saved = PlatformFrequencyBookmarkDocument(name:imported.name,entries:edits)
        try saved.validate()
        #expect(try PlatformFrequencyBookmarkDocument.importData(saved.exportData()).entries == imported.entries)
        #expect(try NativeHistoryFrequencyInput.hz("1000,000001") == 1_000_000_001)
        #expect(try NativeHistoryFrequencyInput.hz("0.000001") == 1)
        for invalid in ["-1","NaN","inf","1e3","1.0000001","9007199254.740992","9999999999999999999999999"] {
            #expect(throws:(any Error).self) { try NativeHistoryFrequencyInput.hz(invalid) }
        }
    }

}
