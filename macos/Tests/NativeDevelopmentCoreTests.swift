import Foundation
import Testing
@testable import DvoynoyObzorSession

@MainActor
private final class DevelopmentChildFixture: NativeDevelopmentCoreChild {
    var isRunning = true
    var closed = false
    var exitsOnClose = true
    func closeLifeline() {
        closed = true
        if exitsOnClose { isRunning = false }
    }
}

@Suite @MainActor
struct NativeDevelopmentCoreTests {
    private var configuration: NativeDevelopmentCoreConfiguration {
        .init(executable: URL(fileURLWithPath: "/fixture/python"), prefixArguments: ["-m", "radioinspector.launcher"],
              checkout: URL(fileURLWithPath: "/fixture"), dataDirectory: URL(fileURLWithPath: "/fixture/archive"))
    }

    @Test func validatesPrivateLoopbackReadiness() throws {
        #expect(try NativeDevelopmentCore.readinessEndpoint(Data("http://127.0.0.1:12345\n".utf8)).port == 12345)
        for address in ["http://example.com:12345", "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://user@127.0.0.1:12345", "http://127.0.0.1:12345/path", "http://127.0.0.1:12345?x=1"] {
            #expect(throws: (any Error).self) { try NativeDevelopmentCore.readinessEndpoint(Data(address.utf8)) }
        }
    }

    @Test func argumentsNeverConnectHardwareOrUseFixedPort() {
        let args = configuration.arguments(readyFile: URL(fileURLWithPath: "/private/ready with spaces"))
        #expect(args == ["-m", "radioinspector.launcher", "--data-dir", "/fixture/archive", "--host", "127.0.0.1", "--port", "0", "--ready-file", "/private/ready with spaces", "--stdin-lifeline"])
        #expect(NativeDevelopmentCoreConfiguration.defaultDataDirectory.path.hasSuffix("OpenRadioInspector/macOS-Development"))
    }

    @Test(arguments: [nil, "RFExplorerSurvey"] as [String?])
    func bundledRustPrecedesCompatibilityHelpersAndKeepsItsOwnArchive(namespace: String?) throws {
        let files = FileManager.default
        let root = files.temporaryDirectory.appendingPathComponent("ori-launcher-" + UUID().uuidString)
        let bundleURL = root.appendingPathComponent("Spectrum Test.app")
        let contents = bundleURL.appendingPathComponent("Contents")
        let helpers = contents.appendingPathComponent("Helpers")
        let rust = helpers.appendingPathComponent("ori-core")
        let legacy = helpers.appendingPathComponent("openradioinspector-core/openradioinspector-core")
        let checkout = root.appendingPathComponent("checkout")
        let python = URL(fileURLWithPath: "/usr/bin/true")
        try files.createDirectory(at: legacy.deletingLastPathComponent(), withIntermediateDirectories: true)
        defer { try? files.removeItem(at: root) }
        var info = ["CFBundleIdentifier": "org.example.ori-launcher-" + UUID().uuidString, "CFBundlePackageType": "APPL"]
        info["ORIDevelopmentArchiveNamespace"] = namespace
        try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
            .write(to: contents.appendingPathComponent("Info.plist"))
        for helper in [rust, legacy] {
            try Data().write(to: helper)
            try files.setAttributes([.posixPermissions: 0o700], ofItemAtPath: helper.path)
        }
        let launcher = checkout.appendingPathComponent("radioinspector/launcher.py")
        try files.createDirectory(at: launcher.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: launcher)
        let bundle = try #require(Bundle(url: bundleURL))
        func discover() throws -> NativeDevelopmentCoreConfiguration {
            try .discover(bundle: bundle, environment: ["ORI_DEVELOPMENT_PYTHON": python.path, "ORI_DEVELOPMENT_CHECKOUT": checkout.path])
        }

        let selected = try discover()
        let development = NativeDevelopmentCoreConfiguration.defaultDataDirectory
        let archive = development.appendingPathComponent(namespace == nil ? "Rust" : "Rust-RFExplorerSurvey", isDirectory: true)
        let ready = root.appendingPathComponent("ready")
        #expect(selected.executable.resolvingSymlinksInPath() == rust.resolvingSymlinksInPath())
        #expect(selected.checkout == nil)
        #expect(selected.dataDirectory == archive)
        #expect(selected.arguments(readyFile: ready) == ["--mode", "live", "--allow-serial", "--data-dir", archive.path,
            "--host", "127.0.0.1", "--port", "0", "--ready-file", ready.path, "--stdin-lifeline"])

        try files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: rust.path)
        let packagedPython = try discover()
        #expect(packagedPython.executable.resolvingSymlinksInPath() == legacy.resolvingSymlinksInPath())
        #expect(packagedPython.dataDirectory == development)
        #expect(packagedPython.prefixArguments.isEmpty)
        try files.removeItem(at: legacy)
        let interpreter = try discover()
        #expect(interpreter.executable == python)
        #expect(interpreter.dataDirectory == development)
        #expect(interpreter.prefixArguments == ["-m", "radioinspector.launcher"])
    }

    @Test func archiveNamespaceRequiresASingleSafeDirectoryName() throws {
        let valid = try NativeDevelopmentCoreConfiguration.rustDataDirectory(namespace: "rf-survey_10kHz")
        #expect(valid.deletingLastPathComponent() == NativeDevelopmentCoreConfiguration.defaultDataDirectory)
        #expect(valid.lastPathComponent == "Rust-rf-survey_10kHz")
        let invalid: [Any] = ["", ".", "..", "../Rust", "/tmp/other", "one/two", "one\\two",
                              "%2e%2e", ".hidden", "-start", "end\n", "a\0b", String(repeating: "a", count: 65), 42]
        for value in invalid {
            #expect(throws: NativeDevelopmentCoreError.self) {
                try NativeDevelopmentCoreConfiguration.rustDataDirectory(namespace: value)
            }
        }
    }

    @Test func personalRCNeverResolvesOrLaunchesCompatibilityCore() async {
        var resolved = false
        let model = NativeDevelopmentCore(isPersonalRC: true, resolve: {
            resolved = true
            return configuration
        })
        await model.start()
        #expect(!resolved)
        #expect(model.phase == .failed)
        #expect(!model.ownsProcess)
    }

    @Test func startRequiresHealthAndStopOnlyClosesOwnedLifeline() async throws {
        let child = DevelopmentChildFixture()
        var readyPath: URL?
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, ready in
            readyPath = ready
            try Data("http://127.0.0.1:12345".utf8).write(to: ready)
            return child
        }, health: { url in #expect(url.port == 12345) })
        #expect(!model.ownsProcess)
        #expect(readyPath == nil)
        await model.start()
        #expect(model.phase == .ready)
        #expect(model.endpoint == "http://127.0.0.1:12345")
        #expect(model.ownsProcess)
        await model.stop()
        #expect(child.closed)
        #expect(!model.ownsProcess)
        #expect(model.endpoint == nil)
        let path = try #require(readyPath)
        #expect(!FileManager.default.fileExists(atPath: path.deletingLastPathComponent().path))
    }

    @Test func failedHealthStopsChildAndNeverPublishesEndpoint() async {
        let child = DevelopmentChildFixture()
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, ready in
            try Data("http://127.0.0.1:12345".utf8).write(to: ready)
            return child
        }, health: { _ in throw URLError(.badServerResponse) })
        await model.start()
        #expect(model.phase == .failed)
        #expect(model.endpoint == nil)
        #expect(child.closed)
        #expect(!model.ownsProcess)
    }

    @Test func childExitDuringHealthCannotLeaveStartupBusy() async {
        let child = DevelopmentChildFixture()
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, ready in
            try Data("http://127.0.0.1:12345".utf8).write(to: ready)
            return child
        }, health: { _ in await MainActor.run { child.isRunning = false } })
        await model.start()
        #expect(model.phase == .failed)
        #expect(!model.busy)
        #expect(!model.ownsProcess)
    }

    @Test func shutdownTimeoutRetainsOwnershipAndBlocksSecondLaunch() async {
        let child = DevelopmentChildFixture()
        child.exitsOnClose = false
        var launches = 0
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, ready in
            launches += 1
            try Data("http://127.0.0.1:12345".utf8).write(to: ready)
            return child
        }, health: { _ in }, stopTimeout: .zero)
        await model.start()
        await model.stop()
        #expect(model.phase == .failed)
        #expect(model.ownsProcess)
        await model.start()
        #expect(launches == 1)
        child.isRunning = false
        await model.stop()
        #expect(!model.ownsProcess)
    }

    @Test func missingInterpreterFailsWithoutClaimingForeignCore() async {
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: {
            throw NativeDevelopmentCoreError.unavailable("Missing environment")
        })
        await model.start()
        #expect(model.phase == .failed)
        #expect(!model.ownsProcess)
        #expect(model.endpoint == nil)
    }

    @Test func cancelledStartStillClosesLifeline() async {
        let child = DevelopmentChildFixture()
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, _ in child })
        let starting = Task { await model.start() }
        while !model.ownsProcess { await Task.yield() }
        starting.cancel()
        await starting.value
        #expect(child.closed)
        #expect(!model.ownsProcess)
        #expect(model.endpoint == nil)
    }

    @Test func concurrentStopCannotPublishLateReadiness() async {
        let child = DevelopmentChildFixture()
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { configuration }, launch: { _, _ in child })
        let starting = Task { await model.start() }
        while !model.ownsProcess { await Task.yield() }
        await model.stop()
        await starting.value
        #expect(model.phase == .stopped)
        #expect(!model.ownsProcess)
        #expect(model.endpoint == nil)
    }

    @Test func selectedEnvironmentPersistsOnlyAfterValidation() throws {
        let suite = "ori-development-test-" + UUID().uuidString
        let preferences = try #require(UserDefaults(suiteName: suite))
        defer { preferences.removePersistentDomain(forName: suite) }
        let model = NativeDevelopmentCore(isPersonalRC: false, preferences: preferences)
        let executable = URL(fileURLWithPath: "/usr/bin/true")
        try model.configurePython(executable: executable)
        #expect(preferences.string(forKey: NativeDevelopmentCoreConfiguration.pythonPreference) == executable.path)
        #expect(throws: (any Error).self) {
            try model.configurePython(executable: URL(fileURLWithPath: "/nonexistent/ori-python"))
        }
        #expect(preferences.string(forKey: NativeDevelopmentCoreConfiguration.pythonPreference) == executable.path)
        #expect(!model.ownsProcess)
    }

    @Test func realChildReceivesEOFOnOwnedPipe() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ori-lifeline-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let script = directory.appendingPathComponent("child.sh")
        try Data("""
        while [ "$#" -gt 0 ]; do
            if [ "$1" = "--ready-file" ]; then shift; ready=$1; fi
            shift
        done
        printf 'http://127.0.0.1:12345\\n' > "$ready"
        /bin/cat > /dev/null
        """.utf8).write(to: script)
        let config = NativeDevelopmentCoreConfiguration(executable: URL(fileURLWithPath: "/bin/sh"),
            prefixArguments: [script.path], checkout: nil, dataDirectory: directory)
        let model = NativeDevelopmentCore(isPersonalRC: false, resolve: { config }, health: { _ in },
                                          startupTimeout: .seconds(3), stopTimeout: .seconds(3))
        await model.start()
        #expect(model.phase == .ready)
        await model.stop()
        #expect(model.phase == .stopped)
        #expect(!model.ownsProcess)
    }
}
