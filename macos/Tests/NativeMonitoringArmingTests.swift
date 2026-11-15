import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeMonitoringArmingTests: XCTestCase {
    @MainActor
    func testScanPlanRequiresAnExplicitBaselineForEverySavedBand() {
        let model = workspace()
        let plan: MonitoringValue = .object(["config": .object(["bands": .array([
            .object(["id": .string("fm")]),
            .object(["id": .string("air")]),
        ])])])
        var project = simulatedProject
        project["scan_plan"] = plan
        project["baseline_id"] = .string("baseline-fm")
        project["band_baselines"] = .object(["fm": .string("baseline-fm"), "obsolete": .string("baseline-old")])
        model.status = .object(["project": .object(project)])
        model.preflight = .object(["baseline_ready": .boolean(true)])
        XCTAssertFalse(model.canArm)

        project["band_baselines"] = .object(["fm": .string("baseline-fm"), "air": .string("baseline-air")])
        model.status = .object(["project": .object(project)])
        model.preflight = .object(["baseline_ready": .boolean(false)])
        XCTAssertTrue(model.canArm)
    }

    @MainActor
    func testLegacyProjectCanArmWithItsSingleSavedBaseline() {
        let model = workspace()
        model.status = .object(["project": .object(simulatedProject)])
        XCTAssertFalse(model.canArm)

        var project = simulatedProject
        project["baseline_id"] = .string("baseline")
        model.status = .object(["project": .object(project)])
        XCTAssertTrue(model.canArm)
    }

    @MainActor
    private func workspace() -> MonitoringWorkspaceModel {
        let model = MonitoringWorkspaceModel()
        model.selected = "project"
        return model
    }

    private var simulatedProject: [String: MonitoringValue] {
        [
            "profile": .object([
                "analyzer": .object(["settings": .object(["adapter": .string("tinysa-sim")])]),
                "receiver": .object(["settings": .object(["adapter": .string("ardv1-sim")])]),
            ]),
        ]
    }
}
