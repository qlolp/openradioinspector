import SwiftUI
import Darwin

/// The macOS application implementation, hosted by the Xcode app target.
public struct DvoynoyObzorApplication: App {
    public init() {
        if let index=CommandLine.arguments.firstIndex(of:"--acceptance-host-info") {
            do {
                guard CommandLine.arguments.count==3,index==1 else{throw CocoaError(.coderInvalidValue)}
                let facts=try NativeAcceptanceHostProbe.run(directory:URL(fileURLWithPath:CommandLine.arguments[index+1],isDirectory:true))
                let data=try JSONSerialization.data(withJSONObject:facts,options:[.sortedKeys])
                FileHandle.standardOutput.write(data);FileHandle.standardOutput.write(Data("\n".utf8));Darwin.exit(0)
            } catch {
                FileHandle.standardError.write(Data((error.localizedDescription+"\n").utf8));Darwin.exit(1)
            }
        }
        do {try NativeAcceptanceTrace.activate()}
        catch {FileHandle.standardError.write(Data(("Acceptance disabled: "+error.localizedDescription+"\n").utf8));Darwin.exit(1)}
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
                .defaultAppStorage(NativeAcceptanceTrace.shared?.preferences ?? .standard)
        }
        .defaultSize(width: 1200, height: 792)
        .commands { NativeConsoleCommands() }
    }
}
