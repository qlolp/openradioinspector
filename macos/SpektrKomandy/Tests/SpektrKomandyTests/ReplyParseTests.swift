import XCTest
@testable import SpektrKomandy

final class ReplyParseTests: XCTestCase {
    /// В описании LM одна строка записана как LMkkc, другая как LMkkkc.
    /// Сколько знаков у уровня, без живого ответа не подтверждено.
    func testLevelWidthsStayUnconfirmed() {
        let narrow = ArDv1Reply.parseLevel(body: "LM551")
        XCTAssertEqual(narrow?.level, 55)
        XCTAssertEqual(narrow?.digitCount, 2)
        XCTAssertEqual(narrow?.squelch, .open)
        XCTAssertEqual(narrow?.digitCountConfirmed, false)

        let wide = ArDv1Reply.parseLevel(body: "LM0551")
        XCTAssertEqual(wide?.level, 55)
        XCTAssertEqual(wide?.digitCount, 3)
        XCTAssertEqual(wide?.squelch, .open)
        XCTAssertEqual(wide?.digitCountConfirmed, false)

        let digital = ArDv1Reply.parseLevel(body: "LM1043")
        XCTAssertEqual(digital?.level, 104)
        XCTAssertEqual(digital?.squelch, .digitalDetected)
        XCTAssertNil(ArDv1Reply.parseLevel(body: "LM055"))
        XCTAssertNil(ArDv1Reply.parseLevel(body: "0551"))
    }

    /// Две цифры кода RE1 в описании есть, целой строки-образца нет.
    /// Разборщик ставит их в начало. На приёмнике это не проверено.
    func testResultCodePrefixIsMarkedUnconfirmed() {
        let line = ArDv1Reply.parse(raw: "20LM0550\r\n")
        XCTAssertEqual(line.resultCode?.kind, 2)
        XCTAssertEqual(line.resultCode?.continued, false)
        XCTAssertFalse(line.resultCodePlacementConfirmed)
        XCTAssertEqual(line.body, "LM0550")
        XCTAssertFalse(ArDv1Reply.isRejection(line))

        let continued = ArDv1Reply.parse(raw: "21VR1805A\r\n")
        XCTAssertEqual(continued.resultCode?.continued, true)
        XCTAssertEqual(continued.body, "VR1805A")
        XCTAssertFalse(continued.resultCodePlacementConfirmed)

        let bare = ArDv1Reply.parse(raw: "LM551\r\n")
        XCTAssertNil(bare.resultCode)
        XCTAssertEqual(bare.body, "LM551")

        let rejected = ArDv1Reply.parse(raw: "50\r\n")
        XCTAssertTrue(ArDv1Reply.isRejection(rejected))
        XCTAssertFalse(rejected.resultCodePlacementConfirmed)
    }

    func testFirmwareAndSerialKeepUnconfirmedTail() {
        let firmware = ArDv1Reply.parseFirmware(body: ArDv1Reply.parse(raw: "20VR1805A\r\n").body)
        XCTAssertEqual(firmware?.value, "1805A")
        XCTAssertEqual(firmware?.patternMatched, true)
        XCTAssertEqual(firmware?.lengthConfirmed, false)

        let serial = ArDv1Reply.parseSerial(body: "RN0952zzzz")
        XCTAssertEqual(serial?.value, "0952zzzz")
        XCTAssertEqual(serial?.lengthConfirmed, false)
        XCTAssertNil(ArDv1Reply.parseSerial(body: "RN"))
    }

    /// Раскладка `scan` с маской 3 в описании не приведена и с прибора не снималась.
    /// Строки ниже — гипотеза разборщика, не образец ответа.
    func testScanPairHypothesisIsNotADeviceSample() {
        let text = "ch>\nscan 100k 800M 290 3\n100000 -90.5\n800000000 -40\nnoise\n"
        let parsed = UltraScanParser.parse(text)
        XCTAssertFalse(parsed.layoutConfirmedOnDevice)
        XCTAssertEqual(parsed.points.count, 2)
        XCTAssertEqual(parsed.points[0].hertz, 100_000)
        XCTAssertEqual(parsed.points[0].dbm, -90.5, accuracy: 0.000_1)
        XCTAssertEqual(parsed.points[0].dbmToken, "-90.5")
        XCTAssertEqual(parsed.points[1].hertz, 800_000_000)
        XCTAssertEqual(parsed.unparsedLines, ["noise"])
    }

    /// Раскладка строки `scan` подтверждена на реальном приборе tinySA Ultra (3 столбца, уровень в экспоненциальной записи).
    func testScanDeviceSampleWithThreeColumnsIsConfirmed() {
        let text = "ch>\r\nscan 100k 800M 290 3\r\n100000 -5.156250e+00 0.000000000 \r\n800000000 -8.700000e+01 0.000000000 \r\nch> "
        let parsed = UltraScanParser.parse(text)
        XCTAssertTrue(parsed.layoutConfirmedOnDevice)
        XCTAssertEqual(parsed.points.count, 2)
        XCTAssertEqual(parsed.points[0].hertz, 100_000)
        XCTAssertEqual(parsed.points[0].dbm, -5.15625, accuracy: 0.0001)
        XCTAssertEqual(parsed.points[1].hertz, 800_000_000)
        XCTAssertEqual(parsed.points[1].dbm, -87.0, accuracy: 0.0001)
        XCTAssertTrue(parsed.unparsedLines.isEmpty)
    }

    func testUltraCommandsUseTheUnconfirmedCarriageReturn() {
        XCTAssertFalse(UltraCommand.terminatorNeedsInstrumentCheck)
        XCTAssertEqual(UltraCommand.terminator, Data([0x0D]))
        XCTAssertFalse(UltraPortBaud.confirmedByDevicePages)
        XCTAssertTrue(UltraReadTiming.quietGapConfirmedOnDevice)
        XCTAssertTrue(UltraIdentity.currentFirmwareTextConfirmed)

        let commands = UltraSessionScript.identityCommands()
            + UltraSessionScript.setupCommands()
            + [UltraSessionScript.scanCommand()]
        XCTAssertEqual(commands.map(\.text), [
            "info",
            "version",
            "deviceid",
            "output off",
            "mode input",
            "lna off",
            "attenuate auto",
            "rbw 30",
            "calc off",
            "scan 100k 800M 290 3",
        ])
        let forbidden = [
            "lna on",
            "ultra",
            "scanraw",
            "hop",
            "mode high",
            "mode output",
            "leveloffset",
            "correction",
            "clearconfig",
            "lna2",
            "agc",
        ]
        for command in commands {
            let frame = command.frame()
            XCTAssertEqual(Data(frame.suffix(UltraCommand.terminator.count)), UltraCommand.terminator)
            XCTAssertFalse(frame.contains(0x0A), command.text)
            XCTAssertEqual(Data(frame.dropLast(UltraCommand.terminator.count)), Data(command.text.utf8))
            for item in forbidden {
                XCTAssertFalse(command.text == item || command.text.hasPrefix(item + " "), command.text)
            }
        }
        XCTAssertTrue(UltraIdentity.matchesConsoleSnapshot(info: "tinySA Ultra\n", version: "tinySA4_v1.3-82-g857be08"))
        XCTAssertTrue(UltraIdentity.matchesConsoleSnapshot(info: "tinySA ULTRA ZS405\n", version: "tinySA4_v1.4-235-gea71dfd"))
        XCTAssertFalse(UltraIdentity.matchesConsoleSnapshot(info: "other", version: "tinySA4_"))
    }

    func testMissingPortMessageDoesNotDependOnADevice() {
        XCTAssertEqual(SessionProblem.emptyPath.errorDescription, "Укажите путь к порту.")
        XCTAssertEqual(
            SessionProblem.missingPort("/dev/cu.missing").errorDescription,
            "Порт «/dev/cu.missing» не найден."
        )
    }
}
