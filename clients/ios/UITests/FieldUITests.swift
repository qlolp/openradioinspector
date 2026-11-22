import XCTest

final class FieldUITests: XCTestCase {
    @MainActor func testOfflineDemoNavigatesFromNoteToOriginalValuesAndGPS() throws {
        let app = XCUIApplication()
        app.launch()
        app.buttons["open-demo"].tap()
        let note = app.buttons["row-note"]
        XCTAssertTrue(note.waitForExistence(timeout: 15))
        note.tap()
        let original = app.buttons["linked-sweep"]
        if !original.isHittable { app.swipeUp() }
        original.tap()
        app.buttons["source-values"].tap()
        XCTAssertTrue(app.staticTexts.containing(NSPredicate(format: "label CONTAINS %@", "-100.125")).firstMatch.waitForExistence(timeout: 8))
        app.navigationBars.buttons.element(boundBy: 0).tap()
        let fix = app.buttons["linked-gps"]
        if !fix.isHittable { app.swipeUp() }
        fix.tap()
        XCTAssertTrue(app.staticTexts["-0.25, 0.125"].waitForExistence(timeout: 8))
    }
}
