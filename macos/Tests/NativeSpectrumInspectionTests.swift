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
    @Test func timelineRetainsPrecisionOfDeviceTimestamps() throws {
        let start = try #require(NativeSpectrumTimeline.date("2026-11-15T21:52:53+03:00"))
        let end = try #require(NativeSpectrumTimeline.date("2026-11-15T18:52:53.689157Z"))
        #expect(abs(end.timeIntervalSince(start) - 0.689157) < 0.000_001)
        #expect(NativeSpectrumTimeline.date("invalid") == nil)
    }
    @Test func selectedSweepUsesTheExactArchiveID() throws {
        let first = try sweep("trace-\u{00E9}", levels: [-90, -80, -90, -80])
        let second = try sweep("trace-e\u{0301}", levels: [-100, -70, -90, -80])
        #expect(first.exactID != second.exactID)
        let selected = NativeSpectrumInspection.selectedSweep(second.exactID, in: [first, second])
        #expect(selected?.exactID == second.exactID)
        #expect(selected?.levels[1] == -70)
    }
    @Test func waterfallPaintsOnlyTheRecordedInterval() throws {
        let first = try sweep(end: "2026-11-14T12:00:01Z")
        let last = try sweep("last", start: "2026-11-14T12:01:39.900Z", end: "2026-11-14T12:01:40Z")
        let instant = try sweep("instant", start: "2026-11-14T12:00:59Z", end: "2026-11-14T12:00:59Z")
        let timeline = NativeSpectrumTimeline([first, last, instant])
        let canvas = CGRect(x: 5, y: 40, width: 100, height: 100)
        let band = try #require(timeline.waterfallRect(for: timeline.rows[2], in: canvas))
        #expect(abs(band.height - 0.1) < 0.000_01)
        #expect(!band.contains(CGPoint(x: 10, y: 40.5)))
        #expect(timeline.row(at: 0.005) == nil)
        #expect(timeline.waterfallRect(for: timeline.rows[1], in: canvas) == nil)
    }
    @Test func aRidgeIncludesAnIsolatedMeasuredBin() throws {
        let source = try sweep()
        let result = NativeSpectrumInspection.visibleBins(in: source, range: 88_099_000...88_101_000)
        #expect(Array(result) == [1])
        #expect(NativeSpectrumInspection.visibleBins(in: source, range: 88_105_000...88_110_000).isEmpty)
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
    @Test func pinchAnchorUsesPlotCoordinates() {
        let width = 1000.0
        let layout = NativeSpectrumPlotLayout(size: CGSize(width: width, height: 500))
        let x = layout.spectrum.minX + layout.spectrum.width * 0.25
        let anchor = layout.pinchAnchor(x / width, viewWidth: width, in: 88_000_000...108_000_000)
        #expect(abs(anchor - 93_000_000) < 0.000_01)
    }
    @Test func receiveDraftPreservesAutomaticBandwidthFromTheSweep() {
        var draft = NativeAnalyzerDraft()
        draft.loadBandwidth(.string("auto"))
        #expect(draft.rbw == "auto")
        draft.loadBandwidth(.number(30_000))
        #expect(draft.rbw == "30000")
    }
    @Test func manualRangeExtendsOnlyWithUltraEnabled() throws {
        var draft = NativeAnalyzerDraft()
        draft.center = "5175"; draft.span = "50"
        #expect(throws: (any Error).self) { try draft.synchronize(.center) }
        try draft.synchronize(.center, ultra: true)
        #expect(try NativeAnalyzerDraft.hz(draft.start, ultra: true) == 5_150_000_000)
        #expect(try NativeAnalyzerDraft.hz(draft.stop, ultra: true) == 5_200_000_000)
        draft.stop = "6000.001"
        #expect(throws: (any Error).self) { try draft.synchronize(.stop, ultra: true) }
    }
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

@Suite struct NativeWorkbenchTests {
    @Test func timeWindowIsBoundedAndKeepsAcquisitionGaps() throws {
        let end = try #require(NativeSpectrumTimeline.date("2026-11-14T12:02:00Z"))
        let window = NativeTimeWindow.range(end: end, seconds: 30)
        #expect(window.upperBound.timeIntervalSince(window.lowerBound) == 30)
        let timeline = NativeSpectrumTimeline([], window: window)
        #expect(timeline.duration == 30)
        #expect(timeline.row(at: 0.5) == nil)
        #expect(timeline.fraction(end.addingTimeInterval(-15)) == 0.5)
        #expect(NativeTimeWindow.range(end: end, seconds: .nan).upperBound.timeIntervalSince(NativeTimeWindow.range(end: end, seconds: .nan).lowerBound) == 120)
    }
    @Test func dividerPreservesAlignedAxesAtBothExtremes() {
        for fraction in [0.25, 0.5, 0.75] {
            let layout = NativeSpectrumPlotLayout(size: CGSize(width: 1000, height: 500), spectrumFraction: fraction)
            #expect(layout.spectrum.width == layout.waterfall.width)
            #expect(layout.spectrum.height >= 100)
            #expect(layout.waterfall.height >= 100)
            #expect(layout.waterfall.maxY <= 500)
        }
    }
    @Test func profileRoundTripPreservesIntegerHzAndDoesNotStoreHardwareIdentity() throws {
        var draft = NativeAnalyzerDraft()
        draft.start = "96.123456"; draft.stop = "100"; draft.rbw = "30000"
        let profile = try NativeMeasurementProfile(name: "  Узкая полоса  ", draft: draft, edited: .start)
        let data = try JSONEncoder().encode(profile)
        let decoded = try NativeMeasurementProfile.decode(data)
        #expect(decoded == profile)
        #expect(decoded.name == "Узкая полоса")
        #expect(try NativeAnalyzerDraft.hz(decoded.draft().start) == 96_123_456)
        let object = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(Set(object.keys) == Set(["version", "name", "startHz", "stopHz", "rbw"]))
    }
    @Test func malformedOrFutureProfilesCannotPopulateReceiveControls() throws {
        for text in [
            #"{"version":2,"name":"x","startHz":88000000,"stopHz":108000000,"rbw":"10000"}"#,
            #"{"version":1,"name":"x","startHz":108000000,"stopHz":88000000,"rbw":"10000"}"#,
            #"{"version":1,"name":"x","startHz":88000000,"stopHz":108000000,"rbw":"generator"}"#
        ] { #expect(throws: (any Error).self) { try NativeMeasurementProfile.decode(Data(text.utf8)) } }
        #expect(throws: (any Error).self) { try NativeMeasurementProfile.decode(Data(repeating: 32, count: 16_385)) }
    }
}
