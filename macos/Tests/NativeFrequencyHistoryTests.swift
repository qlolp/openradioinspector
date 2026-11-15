import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@MainActor @Suite(.serialized) struct NativeFrequencyHistoryTests {
    private func page(_ ids: [String], cursor: String? = nil) throws -> PlatformFrequencyHistoryPage {
        let rows = ids.enumerated().map { index, id in
            ["sweep_id":id,"interval":["started_at":"2026-11-15T00:00:00Z","ended_at":"2026-11-15T00:00:01Z"],
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
}
