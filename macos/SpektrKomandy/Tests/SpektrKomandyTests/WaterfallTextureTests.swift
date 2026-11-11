import XCTest
@testable import SpektrKomandy

final class WaterfallTextureTests: XCTestCase {
    private func history(_ rows: [[Double]], capacity: Int = 3, grid: [Int64] = [100, 200]) -> WaterfallHistory {
        var value = WaterfallHistory(capacity: capacity)
        for row in rows { value.absorb(zip(grid, row).map { LevelBin(frequency: $0, level: $1) }) }
        return value
    }

    func testAppendingOneSweepUploadsOnlyOneTextureRowAndWrapsNewestFirst() throws {
        var texture = PlatformWaterfallTexture()
        let scale = PlatformSpectrumScale(lower: -100, upper: 0)!
        let first = try texture.update(history([[-100, 0], [-50, -25]]), scale: scale)
        XCTAssertTrue(first.reset)
        XCTAssertEqual(first.uploads.map(\.row), [0, 1])
        XCTAssertEqual(texture.newestRow, 1)
        let second = try texture.update(history([[-100, 0], [-50, -25], [-10, -20]]), scale: scale)
        XCTAssertFalse(second.reset)
        XCTAssertEqual(second.uploads.map(\.row), [2])
        let wrapped = try texture.update(history([[-50, -25], [-10, -20], [-30, -40]]), scale: scale)
        XCTAssertEqual(wrapped.uploads.map(\.row), [0])
        XCTAssertEqual(texture.newestRow, 0)
        XCTAssertEqual(texture.visibleRows, 3)
        XCTAssertEqual(texture.orderedRGBA().count, 2 * 3 * 4)
        XCTAssertEqual(Array(texture.orderedRGBA().prefix(8)), wrapped.uploads[0].rgba)
        XCTAssertEqual(try texture.update(history([[-50, -25], [-10, -20], [-30, -40]]), scale: scale).uploads.count, 0)
    }

    func testScaleAndGridChangesRecolorAllRowsWithExistingPaletteSemantics() throws {
        var texture = PlatformWaterfallTexture()
        let scale = PlatformSpectrumScale(lower: -100, upper: 0)!
        let first = try texture.update(history([[-100, 0]]), scale: scale)
        XCTAssertEqual(first.uploads.first?.rgba, [0, 0, 5, 255, 252, 255, 163, 255])
        let changed = try texture.update(history([[-100, 0]]), scale: PlatformSpectrumScale(lower: -50, upper: 50)!)
        XCTAssertTrue(changed.reset); XCTAssertEqual(changed.uploads.count, 1)
        let grid = try texture.update(history([[-100, 0]], grid: [101, 201]), scale: scale)
        XCTAssertTrue(grid.reset)
        XCTAssertEqual(texture.width, 2)
        let cleared = try texture.update(WaterfallHistory(), scale: scale)
        XCTAssertTrue(cleared.reset); XCTAssertEqual(texture.visibleRows, 0)
    }

    func testInvalidShapesAndPointBudgetAreRejectedBeforeAllocating() throws {
        var texture = PlatformWaterfallTexture()
        let scale = PlatformSpectrumScale(lower: -100, upper: 0)!
        var oversized = WaterfallHistory(); oversized.absorb((0..<16_385).map { LevelBin(frequency: Int64($0), level: -80) })
        XCTAssertThrowsError(try texture.update(oversized, scale: scale))
        XCTAssertEqual(texture.width, 0)
        XCTAssertThrowsError(try texture.update(history([[.infinity, 0]]), scale: scale))
    }
}
