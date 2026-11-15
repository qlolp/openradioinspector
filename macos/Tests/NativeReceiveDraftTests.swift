import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite struct NativeReceiveDraftTests {
    @Test func rejectsNonfiniteAndOutOfRangeBeforeSending() {
        var draft = NativeReceiveDraft()
        for value in ["nan", "inf", "-1", "61", ""] {
            draft.sweepSeconds = value
            #expect(throws: (any Error).self) { try draft.parameters() }
        }
        draft.sweepSeconds = "0"; draft.points = "451"
        #expect(throws: (any Error).self) { try draft.parameters() }
    }
    @Test func lnaCannotSilentlyCoexistWithManualAttenuation() {
        var draft = NativeReceiveDraft(); draft.lna = true; draft.attenuation = "10"
        #expect(throws: (any Error).self) { try draft.parameters() }
        draft.attenuation = "0"
        #expect(throws: Never.self) { try draft.parameters() }
    }
    @Test func vbwAndRepeatAreMutuallyExclusiveRequests() throws {
        var draft = NativeReceiveDraft(); draft.useVBW = true; draft.vbw = "1/10"
        let values = try draft.parameters()
        #expect(values["vbw_ratio"] == .string("1/10"))
        #expect(values["repeat"] == nil)
        draft.useVBW = false
        #expect(try draft.parameters()["repeat"] == .integer(1))
    }
    @Test func bandsUseIntegerHertzAndRejectReversedRange() throws {
        var band = NativeWatchBand(); band.start = "95.125"; band.stop = "96"
        #expect(try band.value()["start_hz"] == .integer(95_125_000))
        band.stop = "94"
        #expect(throws: (any Error).self) { try band.value() }
    }
}
