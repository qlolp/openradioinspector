import XCTest
@testable import SpektrKomandy

final class SweepTests: XCTestCase {
    func testExceedanceMergesInsideFiveWidths() {
        let first = [
            LevelBin(frequency: 0, level: 1),
            LevelBin(frequency: 10, level: 5),
            LevelBin(frequency: 20, level: 6),
            LevelBin(frequency: 30, level: 1),
            LevelBin(frequency: 40, level: 9),
        ]
        let rows = ExceedanceList.update(existing: [], bins: first, threshold: 5)
        XCTAssertEqual(rows.count, 2)
        XCTAssertEqual(rows[0].low, 10)
        XCTAssertEqual(rows[0].high, 20)
        XCTAssertEqual(rows[0].center, 15)
        XCTAssertEqual(rows[0].width, 10)
        XCTAssertEqual(rows[0].hits, 1)
        XCTAssertEqual(rows[1].low, 40)
        XCTAssertEqual(rows[1].width, 10)
        XCTAssertEqual(rows[1].hits, 1)

        let again = ExceedanceList.update(existing: rows, bins: first, threshold: 5)
        XCTAssertEqual(again.map(\.hits), [2, 2])

        let narrow = ExceedanceList.update(
            existing: [],
            bins: [LevelBin(frequency: 0, level: 5), LevelBin(frequency: 10, level: 5)],
            threshold: 5
        )
        let tooWide = ExceedanceList.update(
            existing: narrow,
            bins: [LevelBin(frequency: 5, level: 9), LevelBin(frequency: 85, level: 0)],
            threshold: 5
        )
        XCTAssertEqual(tooWide.count, 2)
        XCTAssertEqual(tooWide[0].hits, 1)

        let stillFits = ExceedanceList.update(
            existing: narrow,
            bins: [LevelBin(frequency: 5, level: 9), LevelBin(frequency: 45, level: 0)],
            threshold: 5
        )
        XCTAssertEqual(stillFits.count, 1)
        XCTAssertEqual(stillFits[0].hits, 2)
    }

    func testJaggedSweepCanIncrementOneRowMoreThanOnce() {
        let seed = ExceedanceList.update(
            existing: [],
            bins: [LevelBin(frequency: 0, level: 5), LevelBin(frequency: 1_000, level: 5)],
            threshold: 5
        )
        let jagged = ExceedanceList.update(
            existing: seed,
            bins: [
                LevelBin(frequency: 10, level: 6),
                LevelBin(frequency: 20, level: 0),
                LevelBin(frequency: 30, level: 6),
            ],
            threshold: 5
        )
        XCTAssertEqual(jagged.count, 1)
        XCTAssertEqual(jagged[0].hits, 3)
    }

    func testStuckWatchStopsOnARunAndResetsWhenTheLevelChanges() {
        var watch = StuckMeterWatch(limit: StuckMeterWatch.startingLimit)
        for _ in 0..<9 {
            XCTAssertFalse(watch.observe(4))
        }
        XCTAssertTrue(watch.observe(4))
        var restarted = StuckMeterWatch(limit: 3)
        XCTAssertFalse(restarted.observe(1))
        XCTAssertFalse(restarted.observe(2))
        XCTAssertFalse(restarted.observe(2))
        XCTAssertTrue(restarted.observe(2))
    }

    func testEnvelopeKeepsMinimumAndMaximum() {
        var envelope = TraceEnvelope()
        envelope.absorb([LevelBin(frequency: 10, level: 3), LevelBin(frequency: 20, level: -5)])
        envelope.absorb([LevelBin(frequency: 10, level: 1), LevelBin(frequency: 20, level: 4)])
        XCTAssertEqual(envelope.minimumSeries().map(\.level), [1, -5])
        XCTAssertEqual(envelope.maximumSeries().map(\.level), [3, 4])
    }

    func testTraceFileCarriesTheSessionFieldsAndNotAForeignContainer() throws {
        let ar = ArTraceDocument(
            f1MHz: "144",
            f2MHz: "146",
            step: "010.00",
            mode: "FM",
            firmware: "1805A",
            sweeps: [ArSweep(time: "2026-11-04T17:00:00Z", frequenciesMHz: ["144", "144.01"], levels: [12, 40])]
        )
        let arData = try TraceArchive.encode(ar)
        let decoded = try JSONDecoder().decode(ArTraceDocument.self, from: arData)
        XCTAssertEqual(decoded.format, "dvoynoy-obzor-trassa-1")
        XCTAssertEqual(decoded.levelUnit, "s-meter-count")
        XCTAssertEqual(decoded.firmware, "1805A")
        XCTAssertEqual(decoded.step, "010.00")
        XCTAssertEqual(decoded.sweeps.first?.levels, [12, 40])
        let arText = String(decoding: arData, as: UTF8.self)
        XCTAssertFalse(arText.contains(".pan"))

        let ultra = UltraTraceDocument(
            version: "tinySA4_example",
            sweeps: [UltraSweep(time: "2026-11-04T17:00:00Z", frequenciesHz: ["100000"], levelsDbm: ["-90.5"], unparsed: [])]
        )
        let ultraData = try TraceArchive.encode(ultra)
        let ultraDecoded = try JSONDecoder().decode(UltraTraceDocument.self, from: ultraData)
        XCTAssertEqual(ultraDecoded.levelUnit, "dBm")
        XCTAssertEqual(ultraDecoded.scan, "scan 100k 800M 290 3")
        XCTAssertEqual(ultraDecoded.lna, "off")
        XCTAssertEqual(ultraDecoded.ultraMode, "not-sent")
        XCTAssertFalse(ultraDecoded.terminatorConfirmed)
        XCTAssertFalse(ultraDecoded.scanLayoutConfirmed)
        XCTAssertEqual(ultraDecoded.sweeps.first?.levelsDbm, ["-90.5"])
        XCTAssertEqual(TraceClock.stamp(Date(timeIntervalSince1970: 0)), "1970-01-01T00:00:00Z")
        XCTAssertEqual(TraceClock.fileToken(Date(timeIntervalSince1970: 0)), "19700101T000000Z")
    }
}
