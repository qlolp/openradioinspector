import XCTest
@testable import SpektrKomandy

final class WaterfallTests: XCTestCase {
    func testFirstSweepSetsTheGridAndLaterSweepsAppend() {
        var history = WaterfallHistory(capacity: 10)
        let first = [
            LevelBin(frequency: 100, level: -80),
            LevelBin(frequency: 200, level: -60),
        ]
        XCTAssertFalse(history.absorb(first))
        XCTAssertEqual(history.grid, [100, 200])
        XCTAssertEqual(history.rows, [[-80, -60]])

        let second = [
            LevelBin(frequency: 100, level: -70),
            LevelBin(frequency: 200, level: -50),
        ]
        XCTAssertFalse(history.absorb(second))
        XCTAssertEqual(history.rows, [[-80, -60], [-70, -50]])
        XCTAssertEqual(history.absorbedSweeps, 2)
        XCTAssertEqual(history.droppedRows, 0)
    }

    func testGridChangeRestartsHistoryAndIsReported() {
        var history = WaterfallHistory(capacity: 10)
        history.absorb([
            LevelBin(frequency: 100, level: 1),
            LevelBin(frequency: 200, level: 2),
        ])
        let restarted = history.absorb([LevelBin(frequency: 300, level: 5)])
        XCTAssertTrue(restarted)
        XCTAssertEqual(history.grid, [300])
        XCTAssertEqual(history.rows, [[5]])
        XCTAssertEqual(history.droppedRows, 0)
        XCTAssertEqual(history.absorbedSweeps, 2)
    }

    func testCapacityDropsOldestRowsAndKeepsNewest() {
        var history = WaterfallHistory(capacity: 3)
        for index in 0..<5 {
            history.absorb([LevelBin(frequency: 100, level: Double(index))])
        }
        XCTAssertEqual(history.rows, [[2], [3], [4]])
        XCTAssertEqual(history.droppedRows, 2)
        XCTAssertEqual(history.absorbedSweeps, 5)
    }

    func testCapacityBelowOneIsRaisedToOne() {
        var history = WaterfallHistory(capacity: 0)
        XCTAssertEqual(history.capacity, 1)
        history.absorb([LevelBin(frequency: 1, level: 1)])
        history.absorb([LevelBin(frequency: 1, level: 2)])
        XCTAssertEqual(history.rows, [[2]])
        XCTAssertEqual(history.droppedRows, 1)
    }

    func testColumnOrderComesFromTheFirstSweepEvenWhenUnsorted() {
        var history = WaterfallHistory()
        history.absorb([
            LevelBin(frequency: 300, level: 3),
            LevelBin(frequency: 100, level: 1),
        ])
        XCTAssertEqual(history.grid, [300, 100])
        XCTAssertEqual(history.rows, [[3, 1]])
    }

    func testAutoScaleFindsMinAndMaxAcrossVisibleRows() {
        let scale = WaterfallScale.auto(rows: [[-90, -40], [-70, -55]])
        XCTAssertEqual(scale?.low, -90)
        XCTAssertEqual(scale?.high, -40)

        XCTAssertNil(WaterfallScale.auto(rows: []))
        XCTAssertNil(WaterfallScale.auto(rows: [[.nan]]))

        let partial = WaterfallScale.auto(rows: [[.nan, -60]])
        XCTAssertEqual(partial?.low, -60.5)
        XCTAssertEqual(partial?.high, -59.5)
    }

    func testScaleFractionClampsAndEqualLevelsExpand() {
        let scale = WaterfallScale(low: -80, high: -40)
        XCTAssertEqual(scale.fraction(-80), 0)
        XCTAssertEqual(scale.fraction(-40), 1)
        XCTAssertEqual(scale.fraction(-60), 0.5, accuracy: 1e-12)
        XCTAssertEqual(scale.fraction(-100), 0)
        XCTAssertEqual(scale.fraction(0), 1)

        let flat = WaterfallScale(low: -60, high: -60)
        XCTAssertEqual(flat.low, -60.5)
        XCTAssertEqual(flat.high, -59.5)
        XCTAssertEqual(flat.fraction(-60), 0.5, accuracy: 1e-12)
    }

    func testPaletteEndpointsClampAndChannelsStayInsideUnitRange() {
        let dark = WaterfallPalette.color(0)
        XCTAssertEqual(dark.red, 0, accuracy: 1e-12)
        XCTAssertLessThan(dark.blue, 0.1)

        let bright = WaterfallPalette.color(1)
        XCTAssertGreaterThan(bright.red, 0.9)
        XCTAssertGreaterThan(bright.green, 0.9)

        let below = WaterfallPalette.color(-3)
        XCTAssertEqual(below.red, dark.red, accuracy: 1e-12)
        let above = WaterfallPalette.color(7)
        XCTAssertEqual(above.green, bright.green, accuracy: 1e-12)

        for step in stride(from: 0.0, through: 1.0, by: 0.05) {
            let color = WaterfallPalette.color(step)
            XCTAssert((0...1).contains(color.red))
            XCTAssert((0...1).contains(color.green))
            XCTAssert((0...1).contains(color.blue))
        }
    }

    func testPaletteMidpointInterpolatesBetweenStops() {
        let color = WaterfallPalette.color(0.125)
        let low = WaterfallPalette.stops[0]
        let high = WaterfallPalette.stops[1]
        XCTAssertEqual(color.red, (low.red + high.red) / 2, accuracy: 1e-12)
        XCTAssertEqual(color.green, (low.green + high.green) / 2, accuracy: 1e-12)
        XCTAssertEqual(color.blue, (low.blue + high.blue) / 2, accuracy: 1e-12)
    }
}
