import Foundation

@MainActor
enum NativeAcceptanceDisplayReadiness {
    static func wait(timeout:TimeInterval=10,visible:()->Bool) async throws {
        let deadline=ProcessInfo.processInfo.systemUptime+timeout
        while !visible() {
            try Task.checkCancellation()
            guard ProcessInfo.processInfo.systemUptime<deadline else {
                throw NSError(domain:"ORI.Acceptance",code:2,userInfo:[NSLocalizedDescriptionKey:
                    "Разблокируйте Mac и оставьте окно тестового графика видимым. Фактический показ кадров пока не измерен."])
            }
            try await Task.sleep(for:.milliseconds(20))
        }
    }
}
