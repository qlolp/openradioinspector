import Foundation
import Testing
@testable import SpektrKomandy

struct TraceArchiveContractTests {
    @Test
    func receiverJSONPreservesRawCountsAndDecimalFrequencyText() throws {
        let document = ArTraceDocument(
            f1MHz: "144.00001", f2MHz: "144.00002", step: "000.01",
            mode: "FM", firmware: "synthetic",
            sweeps: [ArSweep(time: "2026-01-02T03:04:05Z",
                             frequenciesMHz: ["144.00001", "144.00002"], levels: [0, 255])]
        )
        let data = try TraceArchive.encode(document)
        let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(json["format"] as? String == "dvoynoy-obzor-trassa-1")
        #expect(json["level_unit"] as? String == "s-meter-count")
        #expect(json["f1_mhz"] as? String == "144.00001")
        #expect(json["f2_mhz"] as? String == "144.00002")
        let sweeps = try #require(json["sweeps"] as? [[String: Any]])
        let sweep = try #require(sweeps.first)
        #expect(sweep["frequencies_mhz"] as? [String] == ["144.00001", "144.00002"])
        #expect(sweep["levels"] as? [Int] == [0, 255])
        #expect(sweep["levels_dbm"] == nil)
        #expect(try JSONDecoder().decode(ArTraceDocument.self, from: data) == document)
    }

    @Test(arguments: [(false, false), (false, true), (true, false), (true, true)])
    func analyzerJSONPreservesConfirmationFlagsAndUnparsedReply(
        terminatorConfirmed: Bool, scanLayoutConfirmed: Bool
    ) throws {
        let document = UltraTraceDocument(
            version: "synthetic/firmware",
            sweeps: [UltraSweep(time: "2026-01-02T03:04:05Z",
                                frequenciesHz: ["100001"], levelsDbm: ["-090.500"],
                                unparsed: ["unknown \"field\"\r\n", "Ошибка / scan"])],
            terminatorConfirmed: terminatorConfirmed,
            scanLayoutConfirmed: scanLayoutConfirmed
        )
        let data = try TraceArchive.encode(document)
        let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(json["level_unit"] as? String == "dBm")
        #expect(json["terminator_confirmed"] as? Bool == terminatorConfirmed)
        #expect(json["scan_layout_confirmed"] as? Bool == scanLayoutConfirmed)
        #expect(json["ultra_mode"] as? String == "not-sent")
        let sweeps = try #require(json["sweeps"] as? [[String: Any]])
        let sweep = try #require(sweeps.first)
        #expect(sweep["frequencies_hz"] as? [String] == ["100001"])
        #expect(sweep["levels_dbm"] as? [String] == ["-090.500"])
        #expect(sweep["unparsed"] as? [String] == document.sweeps[0].unparsed)
        #expect(try JSONDecoder().decode(UltraTraceDocument.self, from: data) == document)
    }

    @Test
    func emptySessionRetainsAnEmptySweepArray() throws {
        let document = UltraTraceDocument(version: "synthetic", sweeps: [])
        let data = try TraceArchive.encode(document)
        let json = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect((json["sweeps"] as? [Any])?.isEmpty == true)
        #expect(try JSONDecoder().decode(UltraTraceDocument.self, from: data) == document)
    }

    @Test(arguments: [
        (-1.0, "1969-12-31T23:59:59Z", "19691231T235959Z"),
        (951_827_696.75, "2000-02-29T12:34:56Z", "20000229T123456Z"),
        (1_767_323_045.0, "2026-01-02T03:04:05Z", "20260102T030405Z")
    ])
    func clockFormatsUTCCalendarBoundaries(seconds: Double, stamp: String, token: String) {
        let date = Date(timeIntervalSince1970: seconds)
        #expect(TraceClock.stamp(date) == stamp)
        #expect(TraceClock.fileToken(date) == token)
    }
}
