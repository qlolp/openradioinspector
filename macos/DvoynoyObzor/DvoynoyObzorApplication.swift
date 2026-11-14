import SwiftUI
import Darwin

/// The macOS application implementation, hosted by the Xcode app target.
public struct DvoynoyObzorApplication: App {
    public init() {
        if CommandLine.arguments.contains("--self-check") {
            do {
                let identity = try NativeRCBundle.validateCurrentInstallation(requireWritable: false)
                let data = try JSONSerialization.data(withJSONObject: ["ok": true, "release_version": NativeRCBundle.release,
                    "backend_id": identity, "hardware_validation": "not_run", "service_started": false], options: [.sortedKeys])
                FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data("\n".utf8))
                Darwin.exit(0)
            } catch {
                FileHandle.standardError.write(Data((error.localizedDescription + "\n").utf8))
                Darwin.exit(1)
            }
        }
    }

    public var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .defaultSize(width: 1200, height: NativeLocalService.isPersonalRC ? 780 : 840)
    }
}
