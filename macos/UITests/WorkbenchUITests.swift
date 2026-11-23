import Foundation
import XCTest

final class WorkbenchUITests: XCTestCase {
    private let fixtureAnalyzerPort = "/dev/cu.ui-fixture"
    private let fixtureReceiverPort = "/dev/cu.ui-receiver"

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
        let target = ProcessInfo.processInfo.environment["ORI_UI_TEST_APP"]
        let app = target.map { XCUIApplication(url: URL(fileURLWithPath: $0)) } ?? XCUIApplication()
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
        let selected = NSPredicate(format: "value CONTAINS %@", "Учебный сеанс 1")
        expectation(for: selected, evaluatedWith: session)
        waitForExpectations(timeout: 15)
        XCTAssertTrue((session.value as? String)?.contains("Учебный сеанс 1") == true,
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

    @MainActor private func enterPreparationPorts(in app: XCUIApplication) {
        let tiny = app.textFields["preparation.tinysa-port"], receiver = app.textFields["preparation.receiver-port"]
        replace(tiny, with: fixtureAnalyzerPort)
        replace(receiver, with: fixtureAnalyzerPort)
        XCTAssertEqual(tiny.value as? String, fixtureAnalyzerPort)
        XCTAssertEqual(receiver.value as? String, fixtureAnalyzerPort)
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(receiver, with: fixtureReceiverPort)
    }

    @MainActor private func actionableCreateButton(in app: XCUIApplication) throws -> XCUIElement {
        let create = app.buttons["preparation.create"]
        let fields = [(app.textFields["preparation.tinysa-port"], fixtureAnalyzerPort),
                      (app.textFields["preparation.receiver-port"], fixtureReceiverPort)]
        let committed = fields.map { field, port in
            XCTNSPredicateExpectation(predicate: NSPredicate(format: "value == %@", port), object: field)
        }
        // The root workbench also polls status and alerts while the sheet is open.
        let actionable = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == true AND enabled == true AND hittable == true"), object: create)
        let result = XCTWaiter.wait(for: committed + [actionable], timeout: 10)
        return try XCTUnwrap(result == .completed ? create : nil,
            "The two distinct fixture ports must be committed and Create must be enabled and hittable")
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
        XCTAssertEqual(source.value as? String, "ui-one-sweep-1")
        XCTAssertTrue(text(element("spectrum.context", in: app)).contains("ИСТОРИЯ"))
    }

    @MainActor func testHistoricalEventOpensReadOnlyOriginalWithoutSwitchingAcquisition() throws {
        let app = try launch(test: #function); defer { app.terminate() }
        let before = app.popUpButtons["workbench.session"].value as? String
        let event = element("observation.event.ui-event-older", in: app)
        XCTAssertTrue(event.waitForExistence(timeout: 15)); event.click()
        let open = app.buttons["observation.open-source"]
        expectation(for: NSPredicate(format: "enabled == true"), evaluatedWith: open)
        waitForExpectations(timeout: 10)
        XCTAssertFalse(app.buttons["observation.enqueue"].isEnabled,
            "An old-run source cannot be queued against the current receiver context")
        open.click()
        let original = element("source-sheet.id", in: app)
        XCTAssertTrue(original.waitForExistence(timeout: 10))
        XCTAssertEqual(original.label, "Идентификатор исходного свипа")
        expectation(for: NSPredicate(format: "value == %@", "ui-two-sweep-1"), evaluatedWith: original)
        waitForExpectations(timeout: 10)
        XCTAssertEqual(original.value as? String, "ui-two-sweep-1")
        app.buttons["source-sheet.close"].click()
        XCTAssertEqual(app.popUpButtons["workbench.session"].value as? String, before)
        XCTAssertTrue(text(element("continuity.core", in: app)).contains("Ядро на связи"))
    }

    @MainActor func testTrainingWindowCaptures() throws {
        let app = try launch(test: #function)
        defer { app.terminate() }
        let window = app.windows.firstMatch
        let event = element("observation.event.ui-event-one", in: app)
        let scope = element("engineering.scope", in: app), quality = element("spectrum.qualification", in: app)
        guard event.waitForExistence(timeout: 15), text(scope).contains("синтетические данные"),
              (app.popUpButtons["workbench.session"].value as? String)?.contains("Учебный сеанс 1") == true else {
            throw CocoaError(.coderValueNotFound)
        }
        let synthetic = NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "Учебные данные", "Учебные данные")
        guard XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: synthetic, object: quality)], timeout: 15) == .completed else {
            throw CocoaError(.coderValueNotFound)
        }
        try attachTrainingWindow(window, named: "macos-monitoring")

        event.click()
        let source = app.buttons["observation.open-source"]
        expectation(for: NSPredicate(format: "enabled == true"), evaluatedWith: source)
        waitForExpectations(timeout: 15)
        try attachTrainingWindow(window, named: "macos-event")
    }

    @MainActor private func attachTrainingWindow(_ window: XCUIElement, named name: String) throws {
        guard window.exists, window.frame.width >= 920, window.frame.height >= 580 else {
            throw CocoaError(.coderValueNotFound)
        }
        let attachment = XCTAttachment(data: window.screenshot().pngRepresentation, uniformTypeIdentifier: "public.png")
        attachment.name = name + ".png"
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    @MainActor func testPreparationRejectsDuplicatePortsAndAmbiguousFrequency() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        enterPreparationPorts(in: app)
        replace(app.textFields["preparation.start-mhz"], with: "88e6")
        XCTAssertFalse(app.buttons["preparation.create"].isEnabled)
        replace(app.textFields["preparation.start-mhz"], with: "88")
        _ = try actionableCreateButton(in: app)

        app.buttons["Закрыть"].click()
        app.buttons["workbench.settings"].click()
        let closeSettings = app.buttons["settings.close"]
        XCTAssertTrue(closeSettings.waitForExistence(timeout: 10))
        let open = app.buttons["connection.open-devices"]
        XCTAssertTrue(open.waitForExistence(timeout: 10))
        XCTAssertTrue(open.isHittable)
        XCTAssertTrue(open.isEnabled)
        open.click()
        choose("RF Explorer 3G Combo", in: app)
        choose("Автообзор 10 кГц", in: app)
        let start = app.textFields["rf-explorer.start-mhz"], stop = app.textFields["rf-explorer.stop-mhz"]
        let hint = app.staticTexts["rf-explorer.port-hint"]
        XCTAssertTrue(hint.waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["rf-explorer.connect"].isEnabled)
        XCTAssertFalse(start.exists)
        XCTAssertFalse(stop.exists)
        choose("Свои границы", in: app)
        replace(start, with: "100")
        replace(stop, with: "400")
        XCTAssertEqual(start.value as? String, "100")
        XCTAssertEqual(stop.value as? String, "400")
        choose("Center / Span", in: app)
        let center = app.textFields["rf-explorer.center-mhz"], span = app.textFields["rf-explorer.span-mhz"]
        XCTAssertEqual(center.value as? String, "250.000000")
        XCTAssertEqual(span.value as? String, "300.000000")
        choose("Start / Stop", in: app)
        XCTAssertEqual(start.value as? String, "100.000000")
        app.buttons["rf-explorer.preset-80"].click()
        XCTAssertEqual(start.value as? String, "80.000000")
        XCTAssertEqual(stop.value as? String, "120.000000")
        choose("Весь активный вход", in: app)
        XCTAssertFalse(start.exists)
        XCTAssertFalse(stop.exists)
        replace(app.textFields["rf-explorer.port"], with: "/dev/cu.ui-fixture")
        XCTAssertFalse(hint.exists)
    }

    @MainActor func testReplayFixtureCannotBePresentedAsLiveHardwarePreparation() throws {
        let app = try launch(test: #function); defer { app.terminate() }; preparation(in: app)
        enterPreparationPorts(in: app)
        let errors = app.descendants(matching: .any).matching(identifier: "preparation.error")
        let required = "live Rust-служба"
        let refusal = errors.matching(NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", required, required)).firstMatch
        XCTAssertFalse(refusal.exists)
        let create = try actionableCreateButton(in: app)
        create.click()
        XCTAssertTrue(refusal.waitForExistence(timeout: 10),
            "Expected the live-service refusal after Create. " +
            "Wizard error: \(errors.firstMatch.exists ? text(errors.firstMatch) : "<missing>"). " +
            "Create enabled: \(create.exists && create.isEnabled)")
        XCTAssertFalse(element("preparation.arm", in: app).exists)
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
