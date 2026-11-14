import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeRCPreparationTests: XCTestCase {
    func testDraftRequiresTwoDistinctNativePortsAndAnIntegerReceiveGrid() throws {
        var draft = NativeRCPreparationDraft()
        draft.tinySAPort = "/dev/cu.tiny"; draft.receiverPort = "/dev/cu.aor"
        XCTAssertEqual(try draft.validated().points, 201)
        draft.receiverPort = draft.tinySAPort
        XCTAssertThrowsError(try draft.validated())
        draft.receiverPort = "sim://ardv1"
        XCTAssertThrowsError(try draft.validated())
        draft.receiverPort = "/dev/cu.aor"; draft.startHz = "88e6"
        XCTAssertThrowsError(try draft.validated())
    }
    func testMasterRefusesShadowReplayAndServersWithoutBackupBudgetSupport() throws {
        func capabilities(_ mode: String, backup: Bool) -> MonitoringValue {
            .object(["candidate": .object(["mode": .string(mode)]), "features": .object([
                "explicit_serial_adapters": .boolean(true), "monitoring_backup_reserve": .boolean(backup)])])
        }
        try NativeRCPreparationDraft.validateCapabilities(capabilities("live", backup: true))
        for mode in ["shadow", "replay", "unknown"] {
            XCTAssertThrowsError(try NativeRCPreparationDraft.validateCapabilities(capabilities(mode, backup: true)))
        }
        XCTAssertThrowsError(try NativeRCPreparationDraft.validateCapabilities(capabilities("live", backup: false)))
    }
    func testBaselineRequiresBothTwentySweepsAndSixtySeconds() {
        func report(_ count: Int64, _ seconds: Double) -> MonitoringValue {
            .object(["baseline_collection": .object(["ready": .boolean(true), "sweeps": .integer(count), "elapsed_seconds": .number(seconds)])])
        }
        XCTAssertFalse(NativeRCReadiness(preflight: report(19, 61)).baselineCollected)
        XCTAssertFalse(NativeRCReadiness(preflight: report(20, 59.99)).baselineCollected)
        XCTAssertTrue(NativeRCReadiness(preflight: report(20, 60)).baselineCollected)
    }
    func testPrimaryOnlyOrUnknownRateNeverShowsAnEightHourBudgetReady() {
        let base: [String: MonitoringValue] = ["ready": .boolean(true), "required_retention_seconds": .integer(43_200),
            "write_probe": .boolean(true), "bytes_per_second": .number(4096), "backup_reserve_requested": .boolean(true)]
        XCTAssertTrue(NativeRCReadiness(preflight: .object(["storage": .object(base)])).budgetReady)
        for patch: [String: MonitoringValue] in [["bytes_per_second": .null], ["backup_reserve_requested": .boolean(false)],
                                                ["required_retention_seconds": .integer(3600)], ["write_probe": .boolean(false)]] {
            XCTAssertFalse(NativeRCReadiness(preflight: .object(["storage": .object(base.merging(patch) { _, new in new })])).budgetReady)
        }
    }
}
