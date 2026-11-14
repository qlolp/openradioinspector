import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite struct NativeSpectrumInspectionTests {
    private func sweep(_ id: String = "one", start: String = "2026-11-14T12:00:00Z", end: String = "2026-11-14T12:00:02Z",
                       frequencies: [Int64] = [88_000_000, 88_100_000, 88_300_000, 89_000_000], levels: [Double] = [-100, -70, -90, -80]) throws -> PlatformSweep {
        let data = try JSONSerialization.data(withJSONObject: ["id": id, "session_id": "s", "stream_id": "t", "level_unit": "dBm",
            "frequencies_hz": frequencies, "levels": levels, "interval": ["started_at": start, "ended_at": end]])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweep.self, from: data)
    }

    @Test func nearestBinUsesRealNonuniformGridAndClampsEdges() throws {
        let source = try sweep()
        #expect(NativeSpectrumInspection.nearestBin(88_250_000, in: source) == 2)
        #expect(NativeSpectrumInspection.nearestBin(1, in: source) == 0)
        #expect(NativeSpectrumInspection.nearestBin(999_000_000, in: source) == 3)
        #expect(NativeSpectrumInspection.nearestBin(.nan, in: source) == nil)
    }
    @Test func timelinePreservesDurationAndGapsInsteadOfInventingMeasurements() throws {
        let a = try sweep()
        let b = try sweep("two", start: "2026-11-14T12:00:05Z", end: "2026-11-14T12:00:09Z")
        let timeline = NativeSpectrumTimeline([b, a])
        #expect(timeline.duration == 9)
        #expect(timeline.row(at: 0)?.sweep.id == "two")
        #expect(timeline.row(at: 1)?.sweep.id == "one")
        #expect(timeline.row(at: 5.0 / 9) == nil)
        #expect(timeline.row(at: -0.1) == nil)
        #expect(timeline.fraction(timeline.rows[1].start) == 4.0 / 9)
    }
    @Test func fractionalUTCAndMidnightAreNotCollapsed() throws {
        let a = try sweep(start: "2026-11-14T23:59:59.500Z", end: "2026-11-15T00:00:01.250Z")
        #expect(NativeSpectrumTimeline([a]).duration == 1.75)
        #expect(NativeSpectrumInspection.interval(a).contains("23:59:59.500"))
    }
    @Test func peakLabelsSelectDistinctMaximaInVisibleRange() throws {
        let a = try sweep(frequencies: [0, 100, 200, 300, 400, 500, 600, 700], levels: [-100, -50, -50, -90, -80, -60, -95, -70])
        #expect(NativeSpectrumInspection.peaks(in: a, range: 0...700) == [1, 5])
        #expect(NativeSpectrumInspection.peaks(in: a, range: 300...700).first == 5)
        #expect(a.levels[1] == -50)
    }
    @Test func markersRetainRawSamplesAndDeltaUsesSweepEndpoints() throws {
        let first = try sweep()
        let second = try sweep("two", start: "2026-11-14T12:00:05Z", end: "2026-11-14T12:00:09Z")
        let a = NativeSpectrumSample(sweep: first, bin: 0), b = NativeSpectrumSample(sweep: second, bin: 1)
        #expect(a.frequency == 88_000_000)
        #expect(a.level == -100)
        let text = NativeSpectrumInspection.delta(a, b)
        #expect(text.contains("+100.000"))
        #expect(text.contains("+30.0"))
        #expect(text.contains("+7.000"))
    }
    @Test func plotsShareFrequencyExtentAndFitMinimumViewport() {
        let geometry = NativeSpectrumPlotLayout(size: CGSize(width: 900, height: 210))
        #expect(geometry.spectrum.minX == geometry.waterfall.minX)
        #expect(geometry.spectrum.width == geometry.waterfall.width)
        #expect(geometry.waterfall.maxY <= 210)
        #expect(geometry.waterfall.minY > geometry.spectrum.maxY)
    }
}

@Suite struct NativeSpectrumNavigationTests {
    @Test func zoomKeepsAnchorAndPanClampsToAcquiredRange() {
        let full = 88_000_000.0...108_000_000.0
        let zoom = NativeSpectrumNavigation.zoom(full, factor: 0.1, anchor: 93_000_000, within: full)
        #expect(zoom == 92_500_000...94_500_000)
        let pan = NativeSpectrumNavigation.fitted(center: 108_000_000, width: 2_000_000, within: full)
        #expect(pan == 106_000_000...108_000_000)
        #expect(NativeSpectrumNavigation.zoom(zoom, factor: 100, anchor: 93_000_000, within: full) == full)
        #expect(NativeSpectrumNavigation.zoom(full, factor: .nan, anchor: 93_000_000, within: full) == full)
    }
    @Test func centerSpanAndEdgesRoundTripInIntegerHz() throws {
        var draft = NativeAnalyzerDraft()
        draft.center = "100,25"; draft.span = "0.5"
        try draft.synchronize(.center)
        #expect(try NativeAnalyzerDraft.hz(draft.start) == 100_000_000)
        #expect(try NativeAnalyzerDraft.hz(draft.stop) == 100_500_000)
        draft.start = "99.123456"
        try draft.synchronize(.start)
        #expect(try NativeAnalyzerDraft.hz(draft.span) == 1_376_544)
        #expect(throws: (any Error).self) { try NativeAnalyzerDraft.hz("nan") }
        draft.stop = "1"
        #expect(throws: (any Error).self) { try draft.synchronize(.stop) }
        draft.center = "799"; draft.span = "10"
        #expect(throws: (any Error).self) { try draft.synchronize(.center) }
    }
}
