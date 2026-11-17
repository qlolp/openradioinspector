import Foundation

@MainActor
enum NativeAcceptancePreferences {
    private static func name(_ runID:String)->String {"org.openradioinspector.acceptance."+runID}
    static func make(runID:String) throws -> UserDefaults {
        guard let value=UserDefaults(suiteName:name(runID)) else{throw CocoaError(.coderInvalidValue)}
        return value
    }
    static func remove(runID:String) {UserDefaults.standard.removePersistentDomain(forName:name(runID))}
}
