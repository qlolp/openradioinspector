import Testing
@testable import SpektrKomandy

struct FrequencyTextBoundaryTests {
    @Test(arguments: [
        "", " \n\t", ".5", "1.", "1.2.3", "1,2.3", "-1", "+1",
        "1e2", "1 000", "1. 5", "1.000001", "１２３", "1.a",
        "999999999999999999999999999999"
    ])
    func rejectsMalformedMHz(text: String) {
        #expect(FrequencyText.parseMHz(text) == nil)
    }

    @Test(arguments: [
        ("0", Int64(0)), ("0.00001", 1), ("001.20", 120_000),
        (" \t145,51250\n", 14_551_250), ("1.1", 110_000),
        ("1.01", 101_000), ("1.001", 100_100), ("1.0001", 100_010)
    ])
    func parsesExactTenHertzUnits(text: String, expected: Int64) {
        #expect(FrequencyText.parseMHz(text) == expected)
    }

    @Test(arguments: [Int64(0), 1, 10_000, 14_551_250, 130_000_000, Int64.max])
    func machineFormattingRoundTrips(units: Int64) {
        #expect(FrequencyText.parseMHz(FrequencyText.formatMHzMachine(from10Hz: units)) == units)
    }

    @Test
    func formattingPreservesUnitsAndFractionalPrecision() {
        #expect(FrequencyText.formatMHzRussian(from10Hz: 14_551_250) == "145,5125")
        #expect(FrequencyText.formatKHzRussian(from10Hz: 125) == "1,25")
        #expect(FrequencyText.formatMHzRussian(fromHertz: 145_512_501) == "145,512501")
        #expect(FrequencyText.formatKHzRussian(fromHertz: -1_250) == "-1,25")
        #expect(FrequencyText.formatMHzRussian(fromHertz: -1) == "-0,000001")
        #expect(FrequencyText.formatMHzMachine(from10Hz: 0) == "0")
    }

    @Test
    func receiverLimitsAreInclusive() {
        #expect(!FrequencyText.arFrequencyAllowed(9_999))
        #expect(FrequencyText.arFrequencyAllowed(10_000))
        #expect(FrequencyText.arFrequencyAllowed(130_000_000))
        #expect(!FrequencyText.arFrequencyAllowed(130_000_001))
    }

    @Test
    func gridHandlesInvalidAndUnevenRanges() {
        #expect(FrequencyText.grid(from: 10, to: 0, step: 1).isEmpty)
        #expect(FrequencyText.grid(from: 0, to: 10, step: 0).isEmpty)
        #expect(FrequencyText.grid(from: 0, to: 10, step: -1).isEmpty)
        #expect(FrequencyText.grid(from: 10, to: 10, step: 3) == [10])
        #expect(FrequencyText.grid(from: 10, to: 20, step: 3) == [10, 13, 16, 19])
    }

    @Test
    func gridStopsBeforeIntegerOverflow() {
        #expect(FrequencyText.grid(from: Int64.max - 2, to: Int64.max, step: 2)
                == [Int64.max - 2, Int64.max])
        #expect(FrequencyText.grid(from: Int64.max - 1, to: Int64.max, step: 2)
                == [Int64.max - 1])
    }

    @Test
    func gridIncludesOneExtraPointSoCallerCanRejectOverBudgetScan() {
        let limit = ScanBudget.maximumPoints
        #expect(FrequencyText.grid(from: 0, to: Int64(limit - 1), step: 1).count == limit)
        let oversized = FrequencyText.grid(from: 0, to: Int64.max, step: 1)
        #expect(oversized.count == limit + 1)
        #expect(oversized.last == Int64(limit))
    }
}
