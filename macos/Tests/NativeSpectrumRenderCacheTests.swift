import CoreGraphics
import Foundation
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeSpectrumRenderCacheTests: XCTestCase {
    private func sweep(_ id:String, frequencies:[Int64]=[100,200,300],levels:[Double]=[-100,-50,-80]) throws -> PlatformSweep {
        let data=try JSONSerialization.data(withJSONObject:["id":id,"session_id":"s","stream_id":"t","level_unit":"dBm",
            "frequencies_hz":frequencies,"levels":levels,"interval":["started_at":"2026-11-16T00:00:00Z","ended_at":"2026-11-16T00:00:01Z"]])
        let decoder=JSONDecoder();decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(PlatformSweep.self,from:data)
    }
    func testAppendingOneSweepReusesUnchangedWaterfallImagesAndEvictsOldRows() throws {
        let a=try sweep("a"),b=try sweep("b"),c=try sweep("c")
        let bounds=try XCTUnwrap(PlatformSpectrumScale(lower:-120,upper:0))
        let first=try NativeSpectrumRenderCache.prepare([a,b],selected:b,bounds:bounds)
        let next=try NativeSpectrumRenderCache.prepare([b,c],selected:c,bounds:bounds,previous:first)
        XCTAssertEqual(next.rows.map(\.id),[b.exactID,c.exactID])
        XCTAssertTrue(first.rows[1].image === next.rows[0].image)
        XCTAssertEqual(next.rasterizedRows,1)
        let same=try NativeSpectrumRenderCache.prepare([b,c],selected:c,bounds:bounds,previous:next)
        XCTAssertEqual(same.rasterizedRows,0) // Panning is a draw transform, not a cache key.
    }
    func testEnvelopeUsesOnlyTheSelectedGridAndKeepsRawPeaks() throws {
        let a=try sweep("a",levels:[-90,-50,-80]),b=try sweep("b",levels:[-100,-40,-70])
        let unrelated=try sweep("other",frequencies:[101,201,301],levels:[0,0,0])
        let cache=try NativeSpectrumRenderCache.prepare([a,b,unrelated],selected:b,
            bounds:PlatformSpectrumScale(lower:-120,upper:0))
        XCTAssertEqual(cache.minimumLevels,[-100,-50,-80]);XCTAssertEqual(cache.maximumLevels,[-90,-40,-70])
        XCTAssertEqual(b.levels,[-100,-40,-70]);XCTAssertNotNil(cache.current)
    }
    func testExactIDsAndChangedScaleDoNotReuseTheWrongRaster() throws {
        let a=try sweep("trace-\u{00e9}"),b=try sweep("trace-e\u{0301}")
        let first=try NativeSpectrumRenderCache.prepare([a],selected:a,bounds:PlatformSpectrumScale(lower:-120,upper:0))
        let next=try NativeSpectrumRenderCache.prepare([b],selected:b,bounds:first.bounds,previous:first)
        XCTAssertEqual(next.rasterizedRows,1);XCTAssertFalse(first.rows[0].image === next.rows[0].image)
        let rescaled=try NativeSpectrumRenderCache.prepare([a],selected:a,
            bounds:PlatformSpectrumScale(lower:-100,upper:0),previous:first)
        XCTAssertEqual(rescaled.rasterizedRows,1)
    }
    func testRasterReductionKeepsGlobalPeakIncludingFinalBin() throws {
        let count=8193
        let a=try sweep("large",frequencies:(0..<count).map(Int64.init),
            levels:Array(repeating:-100,count:count-1)+[0])
        let cache=try NativeSpectrumRenderCache.prepare([a],selected:a,bounds:PlatformSpectrumScale(lower:-100,upper:0))
        let image=try XCTUnwrap(cache.rows.first?.image)
        XCTAssertLessThanOrEqual(image.width,4096)
        let bytes=try XCTUnwrap(image.dataProvider?.data) as Data
        XCTAssertEqual(Array(bytes.suffix(4)),[252,255,163,255])
    }
}
