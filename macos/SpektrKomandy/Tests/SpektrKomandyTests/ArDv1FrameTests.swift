import XCTest
@testable import SpektrKomandy

final class ArDv1FrameTests: XCTestCase {
    func testFramesEndWithCarriageReturnAndHaveNoSpace() throws {
        var commands = [
            ArDv1CommandFactory.resultCodesOn(),
            ArDv1CommandFactory.readFirmware(),
            ArDv1CommandFactory.readSerial(),
            ArDv1CommandFactory.selectVfoA(),
            ArDv1CommandFactory.analogMode(.fm),
            ArDv1CommandFactory.analogMode(.am),
            ArDv1CommandFactory.readLevel(),
            ArDv1CommandFactory.leaveRemote(),
            try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("145.5"))),
            try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("0,1"))),
            try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("1300"))),
        ]
        commands.append(contentsOf: ArDv1Step.all.map(ArDv1CommandFactory.step))

        for command in commands {
            let frame = command.frame()
            XCTAssertFalse(command.text.contains(" "))
            XCTAssertFalse(frame.contains(0x20), command.text)
            XCTAssertFalse(frame.contains(0x0A), command.text)
            XCTAssertEqual(frame.last, 0x0D, command.text)
            XCTAssertEqual(Data(frame.dropLast()), Data(command.text.utf8))
        }
    }

    func testFrequencyAndStepText() throws {
        let fm = ArDv1CommandFactory.analogMode(.fm)
        let am = ArDv1CommandFactory.analogMode(.am)
        XCTAssertEqual(fm.text, "MD0F0")
        XCTAssertEqual(am.text, "MD0F1")
        XCTAssertEqual(try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("145.5"))).text, "RF0145.50000")
        XCTAssertEqual(try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("0.1"))).text, "RF0000.10000")
        XCTAssertEqual(try ArDv1CommandFactory.frequency(units: try XCTUnwrap(FrequencyText.parseMHz("1300"))).text, "RF1300.00000")

        let step = try XCTUnwrap(ArDv1Step.all.first { $0.parameter == "012.50" })
        XCTAssertEqual(ArDv1CommandFactory.step(step).text, "ST012.50")
        let fine = try XCTUnwrap(ArDv1Step.all.first { $0.parameter == "008.33" })
        XCTAssertEqual(ArDv1CommandFactory.step(fine).text, "ST008.33")
        let smallest = try XCTUnwrap(ArDv1Step.all.first { $0.parameter == "000.01" })
        XCTAssertEqual(ArDv1CommandFactory.step(smallest).text, "ST000.01")
        let widest = try XCTUnwrap(ArDv1Step.all.first { $0.parameter == "500.00" })
        XCTAssertEqual(ArDv1CommandFactory.step(widest).text, "ST500.00")
    }

    func testStepListMatchesItsOwnDecimalAndTheGrid() {
        XCTAssertEqual(ArDv1Step.all.count, 20)
        for step in ArDv1Step.all {
            XCTAssertNotNil(step.parameter.range(of: #"^\d{3}\.\d{2}$"#, options: .regularExpression))
            let parts = step.parameter.split(separator: ".")
            let whole = Int64(parts[0]) ?? -1
            let fraction = Int64(parts[1]) ?? -1
            XCTAssertEqual(step.unitsOf10Hz, whole * 100 + fraction)
        }

        let start = FrequencyText.parseMHz("144")
        let end = FrequencyText.parseMHz("144,025")
        let points = FrequencyText.grid(from: start ?? 0, to: end ?? 0, step: 1_250)
        XCTAssertEqual(points.count, 3)
        XCTAssertEqual(FrequencyText.formatMHzMachine(from10Hz: points[1]), "144.0125")
    }

    func testFrequencyOutsideTheReceiverIsRejected() {
        XCTAssertThrowsError(try ArDv1CommandFactory.frequency(units: FrequencyText.parseMHz("0.05") ?? 0)) { error in
            XCTAssertEqual(error as? SessionProblem, .frequencyRange)
        }
        XCTAssertThrowsError(try ArDv1CommandFactory.frequency(units: FrequencyText.parseMHz("1300.1") ?? 0))
        XCTAssertNil(FrequencyText.parseMHz("abc"))
        XCTAssertEqual(FrequencyText.parseMHz("145,5"), FrequencyText.parseMHz("145.5"))
    }

    func testPauseAndStuckFlagsAreNotClaimedAsConfirmed() {
        XCTAssertEqual(ArDv1Timing.startingPauseMilliseconds, 25)
        XCTAssertFalse(ArDv1Timing.pauseConfirmedOnCurrentFirmware)
        XCTAssertEqual(StuckMeterWatch.startingLimit, 10)
        XCTAssertFalse(StuckMeterWatch.startingLimitConfirmedOnCurrentFirmware)
    }
}
