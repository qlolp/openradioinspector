import SwiftUI

@main
struct DvoynoyObzorApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .defaultSize(width: 1200, height: NativeLocalService.isPersonalRC ? 780 : 840)
    }
}
