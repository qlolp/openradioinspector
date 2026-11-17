import Foundation
import XCTest

final class WorkbenchUITests: XCTestCase {
    @MainActor private func launch(test: String) throws -> XCUIApplication {
        let path = try XCTUnwrap(ProcessInfo.processInfo.environment["ORI_UI_TEST_PROFILES"],
            "Use macos/ci/ui_smoke.py to supply an isolated synthetic fixture")
        let profiles = try JSONDecoder().decode([String: String].self,
            from: Data(contentsOf: URL(fileURLWithPath: path)))
        let method = String(test.prefix(while: { $0 != "(" }))
        let profile = try XCTUnwrap(profiles[method], "An owned profile is required for this exact test")
        let app = XCUIApplication()
        app.launchArguments = ["--acceptance-profile", profile]
        app.launch()
        XCTAssertTrue(app.segmentedControls["workbench.navigation"].waitForExistence(timeout: 15))
        XCTAssertTrue(app.staticTexts["continuity.core"].waitForExistence(timeout: 15))
        let connected = NSPredicate(format: "label CONTAINS %@", "Ядро на связи")
        expectation(for: connected, evaluatedWith: app.staticTexts["continuity.core"])
        waitForExpectations(timeout: 15)
        let session = app.popUpButtons["workbench.session"]
        let selected = NSPredicate(format: "value CONTAINS %@", "UI fixture one")
        expectation(for: selected, evaluatedWith: session)
        waitForExpectations(timeout: 15)
        XCTAssertTrue((session.value as? String)?.contains("UI fixture one") == true,
            "Navigation must preserve a selected fixture session, not 'Не выбран'")
        return app
    }

    @MainActor private func tab(_ title: String, in app: XCUIApplication) {
        app.segmentedControls["workbench.navigation"].buttons[title].click()
    }

    @MainActor private func replace(_ field: XCUIElement, with value: String) {
        field.click(); field.typeKey("a", modifierFlags: .command)
        field.typeKey(XCUIKeyboardKey.delete.rawValue, modifierFlags: [])
        if !value.isEmpty { field.typeText(value) }
    }

    @MainActor private func preparation(in app: XCUIApplication) {
        tab("Приборы", in: app)
        let open = app.buttons["preparation.open"]
        XCTAssertTrue(open.waitForExistence(timeout: 10)); XCTAssertTrue(open.isEnabled)
        open.click()
        XCTAssertTrue(app.textFields["preparation.tinysa-port"].waitForExistence(timeout: 10))
    }

    @MainActor func testNavigationPreservesSessionAndCoreConnection() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let before = app.popUpButtons["workbench.session"].value as? String
        XCTAssertNotNil(before)
        for title in ["Приборы", "Сигналы", "Архив", "Охрана и исследование", "Контроль диапазона"] {
            tab(title, in: app)
            XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
            XCTAssertTrue(app.staticTexts["continuity.core"].label.contains("Ядро на связи"))
            XCTAssertTrue(app.buttons["workbench.space"].exists)
        }
    }

    @MainActor func testPreparationRejectsDuplicatePortsAndAmbiguousFrequency() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        let tiny = app.textFields["preparation.tinysa-port"]
        let receiver = app.textFields["preparation.receiver-port"]
        replace(tiny, with: "/dev/cu.ui-fixture")
        replace(receiver, with: "/dev/cu.ui-fixture")
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(receiver, with: "/dev/cu.ui-receiver")
        replace(app.textFields["preparation.start-hz"], with: "88e6")
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(app.textFields["preparation.start-hz"], with: "88000000")
        XCTAssertTrue(app.buttons["preparation.create"].isEnabled)
    }

    @MainActor func testReplayFixtureCannotBePresentedAsLiveHardwarePreparation() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        replace(app.textFields["preparation.tinysa-port"], with: "/dev/cu.ui-fixture")
        replace(app.textFields["preparation.receiver-port"], with: "/dev/cu.ui-receiver")
        app.buttons["preparation.create"].click()
        let error = app.staticTexts["preparation.error"]
        XCTAssertTrue(error.waitForExistence(timeout: 10))
        XCTAssertTrue(error.label.contains("live Rust-служба"))
        XCTAssertFalse(app.buttons["preparation.arm"].exists)
    }

    @MainActor func testArchiveOpensOriginalSpectrumAndMarksHistory() throws {
        let app = try launch(test: #function); defer { app.terminate() }; tab("Архив", in: app)
        XCTAssertFalse(app.buttons["archive.open-source"].isEnabled)
        app.buttons["archive.search"].click()
        let source = app.staticTexts["archive.source.ui-one-sweep-1"]
        XCTAssertTrue(source.waitForExistence(timeout: 10)); source.click()
        XCTAssertTrue(app.buttons["archive.open-source"].isEnabled)
        app.buttons["archive.open-source"].click()
        let context = app.staticTexts["spectrum.context"]
        XCTAssertTrue(context.waitForExistence(timeout: 10))
        let historical = NSPredicate(format: "label == %@", "ИСТОРИЯ")
        expectation(for: historical, evaluatedWith: context)
        waitForExpectations(timeout: 10)
    }

    @MainActor func testWorkspaceSwitchKeepsCoreAndSelectedSession() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let before = app.popUpButtons["workbench.session"].value as? String
        app.buttons["workbench.space"].click()
        let modes = app.segmentedControls["workspace.mode-switch"]
        XCTAssertTrue(modes.waitForExistence(timeout: 10))
        modes.buttons["Исследование"].click()
        XCTAssertTrue(app.buttons["workbench.space"].label.contains("Исследование"))
        tab("Контроль диапазона", in: app)
        XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
        XCTAssertTrue(app.staticTexts["continuity.core"].label.contains("Ядро на связи"))
    }
}
