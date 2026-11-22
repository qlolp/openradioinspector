import SwiftUI
import DvoynoyObzorSession

@main
struct DvoynoyObzorApp: App {
    @NSApplicationDelegateAdaptor(NativeFieldOpenDelegate.self) private var fileHandler
    private let application = DvoynoyObzorApplication()

    var body: some Scene {
        application.body
    }
}
