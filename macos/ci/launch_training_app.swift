// Launch the SHA-bound fixture using the same macOS open-application event as Finder.
import AppKit
import Foundation

let args = Array(CommandLine.arguments.dropFirst())
guard args.count == 2 else { throw CocoaError(.coderInvalidValue) }
let url = URL(fileURLWithPath: args[0]).resolvingSymlinksInPath()
guard let bundle = Bundle(url: url), let id = bundle.bundleIdentifier,
      NSRunningApplication.runningApplications(withBundleIdentifier: id).isEmpty else {
    throw CocoaError(.fileWriteFileExists)
}
let configuration = NSWorkspace.OpenConfiguration()
configuration.createsNewApplicationInstance = true
configuration.addsToRecentItems = false
configuration.arguments = ["--acceptance-profile", args[1]]
let application = try await NSWorkspace.shared.openApplication(at: url, configuration: configuration)
guard application.bundleURL?.resolvingSymlinksInPath() == url else { throw CocoaError(.executableLoad) }
let clock = ContinuousClock()
let deadline = clock.now.advanced(by: .seconds(70))
while !application.isTerminated && clock.now < deadline {
    try await Task.sleep(for: .milliseconds(150))
}
if !application.isTerminated {
    // This is the new instance returned by our launch, separate from every prior process.
    application.terminate()
    let cleanup = clock.now.advanced(by: .seconds(5))
    while !application.isTerminated && clock.now < cleanup { try await Task.sleep(for: .milliseconds(100)) }
    if !application.isTerminated { application.forceTerminate() }
    throw CocoaError(.userCancelled, userInfo: [NSLocalizedDescriptionKey: "Истёк срок получения учебного окна"])
}
