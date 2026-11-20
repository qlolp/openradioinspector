import CoreGraphics
import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeSpectrumRasterWindowTests: XCTestCase {
    func testSmallPanReusesPreparedCoverageWithoutReducingViewportResolution() throws {
        let first = try XCTUnwrap(NativeSpectrumRasterWindow.covering(40...60, within: 0...100))
        XCTAssertEqual(first.range, 20...80)
        XCTAssertEqual(first.pixelWidth, 12_288)
        let panned = try XCTUnwrap(NativeSpectrumRasterWindow.covering(42...62, within: 0...100, previous: first))
        XCTAssertEqual(panned, first)
    }

    func testZoomRebuildsDetailAndPanOutsideCoverageRebuildsItsWindow() throws {
        let first = try XCTUnwrap(NativeSpectrumRasterWindow.covering(40...60, within: 0...100))
        let zoom = try XCTUnwrap(NativeSpectrumRasterWindow.covering(46...56, within: 0...100, previous: first))
        XCTAssertNotEqual(zoom, first)
        XCTAssertEqual(zoom.range, 36...66)
        let edge = try XCTUnwrap(NativeSpectrumRasterWindow.covering(80...100, within: 0...100, previous: first))
        XCTAssertEqual(edge.range, 60...100)
        XCTAssertEqual(edge.pixelWidth, 8192)
    }

    func testFullRangeCacheCannotHideDetailWhenZoomingIn() throws {
        let full = try XCTUnwrap(NativeSpectrumRasterWindow.covering(0...100, within: 0...100))
        XCTAssertEqual(full.pixelWidth, 4096)
        let zoom = try XCTUnwrap(NativeSpectrumRasterWindow.covering(25...75, within: 0...100, previous: full))
        XCTAssertEqual(zoom.range, 0...100)
        XCTAssertEqual(zoom.pixelWidth, 8192)
        XCTAssertNotEqual(zoom, full)
        XCTAssertNil(NativeSpectrumRasterWindow.covering(-1...20, within: 0...100))
        XCTAssertNil(NativeSpectrumRasterWindow.covering(10...10, within: 0...100))
        XCTAssertNil(NativeSpectrumRasterWindow.covering(10...Double.infinity, within: 0...100))
    }

    func testRepeatedPanDoesNotRerasterizeUnchangedRawRows() throws {
        let raw = Data(#"{"id":"peak","session_id":"s","stream_id":"t","level_unit":"dBm","frequencies_hz":[100,200,300],"levels":[-100,0,-100],"interval":{"started_at":"2026-11-17T00:00:00Z","ended_at":"2026-11-17T00:00:01Z"}}"#.utf8)
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let source = try decoder.decode(PlatformSweep.self, from: raw)
        let bounds = PlatformSpectrumScale(lower: -100, upper: 0)
        let window = try XCTUnwrap(NativeSpectrumRasterWindow.covering(180...220, within: 100...300))
        let original = try NativeSpectrumRenderCache.prepare([source], selected: source, bounds: bounds,
            viewport: window.range, rasterWidth: window.pixelWidth)
        var prepared = original
        for step in 0..<60 {
            let offset = Double(step % 2)
            let next = try XCTUnwrap(NativeSpectrumRasterWindow.covering((180+offset)...(220+offset), within: 100...300, previous: window))
            prepared = try NativeSpectrumRenderCache.prepare([source], selected: source, bounds: bounds,
                viewport: next.range, rasterWidth: next.pixelWidth, previous: prepared)
            XCTAssertEqual(prepared.rasterizedRows, 0)
            XCTAssertTrue(prepared.rows[0].image === original.rows[0].image)
            XCTAssertEqual(source.levels, [-100,0,-100])
        }
        XCTAssertEqual(prepared.rows[0].image.width, 12_288)
        let pixels = try XCTUnwrap(prepared.rows[0].image.dataProvider?.data) as Data
        XCTAssertEqual(Array(pixels.prefix(4)), [0,0,5,255])
        XCTAssertEqual(Array(pixels.suffix(4)), [0,0,5,255])
        let middle = (pixels.count / 8) * 4
        XCTAssertEqual(Array(pixels[middle..<(middle+4)]), [252,255,163,255])
    }
}
