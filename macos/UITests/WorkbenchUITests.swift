import Foundation
import XCTest

final class WorkbenchUITests: XCTestCase {
    @MainActor private func element(_ identifier: String, in app: XCUIApplication) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    @MainActor private func text(_ item: XCUIElement) -> String {
        item.label + " " + (item.value as? String ?? "")
    }

    @MainActor private func choose(_ title: String, in picker: XCUIElement) {
        let radio = picker.radioButtons[title]
        let control = radio.exists ? radio : picker.buttons[title]
        XCTAssertTrue(control.waitForExistence(timeout: 10))
        control.click()
    }

    @MainActor private func launch(test: String) throws -> XCUIApplication {
        let path = try XCTUnwrap(ProcessInfo.processInfo.environment["ORI_UI_TEST_PROFILES"],
            "Use macos/ci/ui_smoke.py to supply an isolated synthetic fixture")
        let profiles = try JSONDecoder().decode([String: String].self,
            from: Data(contentsOf: URL(fileURLWithPath: path)))
        let method = String(test.prefix(while: { $0 != "(" }))
        let profile = try XCTUnwrap(profiles[method], "An owned profile is required for this exact test")
        let app = XCUIApplication()
        var ready = false
        defer { if !ready { app.terminate() } }
        app.launchArguments = ["--acceptance-profile", profile]
        app.launch()
        print("Synthetic UI hierarchy: \(app.debugDescription.prefix(16000))")
        guard element("workbench.navigation", in: app).waitForExistence(timeout: 15),
              element("continuity.core", in: app).waitForExistence(timeout: 15) else {
            XCTFail("Workbench controls are missing; see the synthetic UI hierarchy")
            throw CocoaError(.coderValueNotFound)
        }
        let core = element("continuity.core", in: app)
        print("Core status label=\(core.label), value=\(String(describing: core.value))")
        let connected = NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "Ядро на связи", "Ядро на связи")
        guard XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: connected, object: core)], timeout: 15) == .completed else {
            XCTFail("The fixture core is not connected: \(text(core))")
            throw CocoaError(.coderValueNotFound)
        }
        let session = app.popUpButtons["workbench.session"]
        let selected = NSPredicate(format: "value CONTAINS %@", "UI fixture one")
        expectation(for: selected, evaluatedWith: session)
        waitForExpectations(timeout: 15)
        XCTAssertTrue((session.value as? String)?.contains("UI fixture one") == true,
            "Navigation must preserve a selected fixture session, not 'Не выбран'")
        ready = true
        return app
    }

    @MainActor private func tab(_ title: String, in app: XCUIApplication) {
        choose(title, in: element("workbench.navigation", in: app))
    }

    @MainActor private func replace(_ field: XCUIElement, with value: String) {
        field.click(); field.typeKey("a", modifierFlags: .command)
        field.typeKey(XCUIKeyboardKey.delete.rawValue, modifierFlags: [])
        if !value.isEmpty { field.typeText(value) }
    }

    @MainActor private func preparation(in app: XCUIApplication) {
        tab("Наблюдение", in: app)
        let open = app.buttons["preparation.open"]
        XCTAssertTrue(open.waitForExistence(timeout: 10)); XCTAssertTrue(open.isEnabled)
        open.click()
        XCTAssertTrue(app.textFields["preparation.tinysa-port"].waitForExistence(timeout: 10))
    }

    @MainActor func testNavigationPreservesSessionAndCoreConnection() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let before = app.popUpButtons["workbench.session"].value as? String
        XCTAssertNotNil(before)
        for title in ["Архив", "Наблюдение", "Архив", "Наблюдение"] {
            tab(title, in: app)
            XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
            XCTAssertTrue(text(element("continuity.core", in: app)).contains("Ядро на связи"))
            XCTAssertTrue(app.buttons["workbench.space"].exists)
        }
        XCTAssertTrue(element("observation.events", in: app).exists,
            "The event inbox must be available alongside the spectrum")
        app.buttons["workbench.settings"].click()
        XCTAssertTrue(app.buttons["settings.close"].waitForExistence(timeout: 10))
        app.buttons["settings.close"].click()
        XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
        app.typeKey("2", modifierFlags: .command)
        XCTAssertTrue(app.buttons["archive.search"].waitForExistence(timeout: 10))
        app.typeKey("1", modifierFlags: .command)
        XCTAssertTrue(element("observation.events", in: app).waitForExistence(timeout: 10))
    }

    @MainActor func testEventOpensExactOriginalAlongsideReception() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let event = element("observation.event.ui-event-one", in: app)
        XCTAssertTrue(event.waitForExistence(timeout: 15)); event.click()
        let open = app.buttons["observation.open-source"]
        let enabled = NSPredicate(format: "enabled == true")
        expectation(for: enabled, evaluatedWith: open)
        waitForExpectations(timeout: 10)
        open.click()
        let source = element("spectrum.range", in: app)
        let exact = NSPredicate(format: "value == %@", "ui-one-sweep-1")
        expectation(for: exact, evaluatedWith: source)
        waitForExpectations(timeout: 10)
        XCTAssertTrue(text(element("spectrum.context", in: app)).contains("ИСТОРИЯ"))
        XCTAssertTrue(text(element("continuity.core", in: app)).contains("Ядро на связи"))
        app.typeKey("2", modifierFlags: .command)
        app.typeKey("1", modifierFlags: .command)
        XCTAssertTrue(source.waitForExistence(timeout: 10))
        XCTAssertTrue(text(element("spectrum.context", in: app)).contains("ИСТОРИЯ"))
    }

    @MainActor func testPreparationRejectsDuplicatePortsAndAmbiguousFrequency() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        let tiny = app.textFields["preparation.tinysa-port"]
        let receiver = app.textFields["preparation.receiver-port"]
        replace(tiny, with: "/dev/cu.ui-fixture")
        replace(receiver, with: "/dev/cu.ui-fixture")
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(receiver, with: "/dev/cu.ui-receiver")
        replace(app.textFields["preparation.start-mhz"], with: "88e6")
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(app.textFields["preparation.start-mhz"], with: "88")
        XCTAssertTrue(app.buttons["preparation.create"].isEnabled)
    }

    @MainActor func testReplayFixtureCannotBePresentedAsLiveHardwarePreparation() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        replace(app.textFields["preparation.tinysa-port"], with: "/dev/cu.ui-fixture")
        replace(app.textFields["preparation.receiver-port"], with: "/dev/cu.ui-receiver")
        app.buttons["preparation.create"].click()
        let error = app.staticTexts["preparation.error"]
        XCTAssertTrue(error.waitForExistence(timeout: 10))
        XCTAssertTrue(text(error).contains("live Rust-служба"))
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
        let historical = NSPredicate(format: "label == %@ OR value == %@", "ИСТОРИЯ", "ИСТОРИЯ")
        expectation(for: historical, evaluatedWith: context)
        waitForExpectations(timeout: 10)
    }

    @MainActor func testWorkspaceSwitchKeepsCoreAndSelectedSession() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let before = app.popUpButtons["workbench.session"].value as? String
        app.buttons["workbench.space"].click()
        // A labelled macOS Picker also gives its caption this identifier.
        // Select the radio group itself, rather than that static-text caption.
        let modes = app.radioGroups["workspace.mode-switch"]
        XCTAssertTrue(modes.waitForExistence(timeout: 10))
        XCTAssertFalse(app.staticTexts["workspace.mode-switch"].exists,
            "The picker caption must not cover its radio buttons in accessibility hit-testing")
        choose("Исследование", in: modes)
        print("Workspace picker after action: \(modes.debugDescription.prefix(4000))")
        let space = app.buttons["workbench.space"]
        print("Workspace after selection: label=\(space.label), value=\(String(describing: space.value))")
        let research = NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "Исследование", "Исследование")
        expectation(for: research, evaluatedWith: space)
        waitForExpectations(timeout: 10)
        app.buttons["settings.close"].click()
        tab("Наблюдение", in: app)
        XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
        XCTAssertTrue(text(element("continuity.core", in: app)).contains("Ядро на связи"))
    }
}
