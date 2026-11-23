import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite struct NativeRFExplorerSurveyTests {
    @Test func surveyPresetsAndIntegerCenterSpanProduceTheSameRequest() throws {
        var draft = NativeRFExplorerDraft()
        draft.applySurveyRange(80_000_000...120_000_000)
        #expect(draft.plannedSurveyTiles == 36)
        draft.port = "/dev/cu.fixture"
        #expect(try draft.configuration().plannedSurveyTiles == 36)
        draft.applySurveyRange(100_001_000...400_001_000)
        try draft.switchStyle(to: .centerSpan)
        #expect(try NativeHistoryFrequencyInput.hz(draft.centerMHz) == 250_001_000)
        #expect(try NativeHistoryFrequencyInput.hz(draft.spanMHz) == 300_000_000)
        let custom = try draft.configuration()
        #expect(custom.startHz == 100_001_000 && custom.stopHz == 400_001_000 && custom.plannedSurveyTiles == 268)
        draft.centerMHz = "250,002"
        try draft.switchStyle(to: .startStop)
        #expect(try NativeHistoryFrequencyInput.hz(draft.startMHz) == 100_002_000)
        #expect(try NativeHistoryFrequencyInput.hz(draft.stopMHz) == 400_002_000)
        let saved = try draft.savedRange(name: "  Выезд  ")
        let loaded = try JSONDecoder().decode(NativeRFExplorerPreset.self, from: JSONEncoder().encode(saved))
        draft.applySurveyRange(nil)
        #expect(try draft.configuration().startHz == nil)
        #expect(try draft.configuration().plannedSurveyTiles == nil)
        draft.applySurveyRange(loaded.bounds)
        #expect(try draft.configuration().startHz == 100_002_000 && loaded.name == "Выезд")
        try draft.switchStyle(to: .centerSpan)
        draft.spanMHz = "1,110001"
        #expect(throws: PlatformError.self) { try draft.configuration() }
    }

    @Test func moduleLimitsRequireAConfirmedReadback() {
        var settings: [String: PlatformComparisonSetting] = ["active_board": .string("expansion"),
            "active_input": .string("right"), "board_min_hz": .number(15_000_000), "board_max_hz": .number(2_700_000_000)]
        #expect(NativeRFExplorerReadback(settings) == nil)
        settings["settings_source"] = .string("device_readback")
        settings["settings_readback"] = .boolean(true)
        #expect(NativeRFExplorerReadback(settings)?.bounds == 15_000_000...2_700_000_000)
        #expect(NativeRFExplorerReadback(settings)?.label.contains("правый вход SMA") == true)
        settings["settings_readback"] = .boolean(false)
        #expect(NativeRFExplorerReadback(settings) == nil)
    }

    @Test @MainActor func changingPassDiscardsAResponseThatCompletesAfterTheNewSelection() async throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        func page(_ id: String) throws -> PlatformRFExplorerPassPage {
            let json = """
            {"items":[{"pass_id":"\(id)","pass":1,"tiles":1,"received_tiles":1,"unique_points":112,
             "expected_points":112,"start_hz":80000000,"stop_hz":81110000,"step_hz":10000,
             "started_at":"2026-11-22T12:00:00Z","ended_at":"2026-11-22T12:00:01Z","complete":true,
             "simulated":true,"duration_s":1,"revisit_s":null,"active_board":"main","active_input":"left",
             "detector":"normal","rbw_hz":15000,"offset_db":0}],"next_before":null}
            """
            return try decoder.decode(PlatformRFExplorerPassPage.self, from: Data(json.utf8))
        }
        let older = try page("older"), recent = try page("recent")
        var held: CheckedContinuation<PlatformRFExplorerPassPage, Never>?
        let browser = NativeRFExplorerPassBrowser()
        let oldQuery = NativeRFExplorerPassQuery(pass: "older")
        let old = Task { @MainActor in
            await browser.load(oldQuery, list: { _ in await withCheckedContinuation { held = $0 } },
                detail: { _, _, _ in throw PlatformError.connection("late") })
        }
        while held == nil { await Task.yield() }
        let newQuery = NativeRFExplorerPassQuery(pass: "recent")
        await browser.load(newQuery, list: { _ in recent }, detail: { _, _, _ in throw PlatformError.connection("current") })
        held?.resume(returning: older)
        await old.value
        #expect(browser.query == newQuery)
        #expect(browser.page?.items.first?.passId == "recent")
        #expect(browser.selectedSummary?.startedAt == "2026-11-22T12:00:00Z")
        #expect(browser.message.contains("current") && !browser.message.contains("late"))
    }
}
