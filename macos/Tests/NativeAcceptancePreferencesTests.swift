import Foundation
import XCTest
@testable import DvoynoyObzorSession

final class NativeAcceptancePreferencesTests:XCTestCase {
    @MainActor func testEngineeringRunsCannotChangeOperatorPreferencesOrEachOther() throws {
        let a="preferences-a-"+UUID().uuidString,b="preferences-b-"+UUID().uuidString
        let key="native.plot.seconds",old=UserDefaults.standard.object(forKey:key)
        let first=try NativeAcceptancePreferences.make(runID:a),second=try NativeAcceptancePreferences.make(runID:b)
        defer {NativeAcceptancePreferences.remove(runID:a);NativeAcceptancePreferences.remove(runID:b)}
        first.set(15,forKey:key)
        XCTAssertNil(second.object(forKey:key))
        XCTAssertEqual(try NativeAcceptancePreferences.make(runID:a).integer(forKey:key),15)
        XCTAssertEqual(UserDefaults.standard.object(forKey:key) as? Double,old as? Double)
    }
}
