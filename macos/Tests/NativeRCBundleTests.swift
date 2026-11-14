import Foundation
import CryptoKit
import XCTest
@testable import DvoynoyObzorSession

final class NativeRCBundleTests: XCTestCase {
    func testMetadataBindsHelpersAndRefusesChangedMixedAndEscapingComponents() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".app")
        try FileManager.default.createDirectory(at: root.appendingPathComponent("Contents/Helpers"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("Contents/Resources"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        var components: [String: Any] = [:]
        for name in ["ori-core", "ori-supervisor"] {
            let bytes = Data(name.utf8)
            let relative = "Contents/Helpers/" + name
            try bytes.write(to: root.appendingPathComponent(relative))
            var entry: [String: Any] = ["component": name, "path": relative,
                "sha256": SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined(),
                "source_revision": String(repeating: "a", count: 40), "target_arch": "aarch64", "target_os": "macos"]
            if name == "ori-core" { entry["features"] = ["serial": true, "audio": false] }
            components[name] = entry
        }
        var value: [String: Any] = ["schema_version": 1, "release_version": "0.4.0-macos.rc.1",
            "bundle_identifier": "org.openradioinspector.macos.rc", "source_revision": String(repeating: "a", count: 40),
            "architecture": "arm64", "components": components]
        var info: [String: Any] = ["CFBundleIdentifier": "org.openradioinspector.macos.rc",
            "CFBundleExecutable": "OpenRadioInspectorRC", "ORIPersonalRC": true,
            "ORIReleaseVersion": "0.4.0-macos.rc.1"]
        let plist = root.appendingPathComponent("Contents/Info.plist")
        func saveInfo() throws { try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0).write(to: plist) }
        try saveInfo()
        let metadata = root.appendingPathComponent("Contents/Resources/rc-build-info.json")
        func save() throws { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]).write(to: metadata) }
        try save()
        let id = try NativeRCBundle.backendID(at: root)
        XCTAssertTrue(id.hasPrefix("macos-rc1-")); XCTAssertEqual(id.count, 74)
        var audio = components["ori-core"] as! [String: Any]
        audio["features"] = ["serial": true, "audio": true]
        components["ori-core"] = audio; value["components"] = components
        value["release_version"] = "0.4.0-macos.rc.2"; try save()
        info["ORIReleaseVersion"] = "0.4.0-macos.rc.2"
        info["ORINativeAudioCapture"] = true; info["NSMicrophoneUsageDescription"] = "Selected analogue input"
        try saveInfo()
        XCTAssertTrue(try NativeRCBundle.backendID(at: root).hasPrefix("macos-rc2-audio-"))
        for broken in [["ORINativeAudioCapture": false], ["NSMicrophoneUsageDescription": ""],
            ["ORIReleaseVersion": "0.4.0-macos.rc.1"], ["ORIPersonalRC": false]] as [[String: Any]] {
            let saved = info
            for (key, entry) in broken { info[key] = entry }
            try saveInfo(); XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
            info = saved; try saveInfo()
        }
        for profile in [NSNull(), "unknown", "rc1"] as [Any] {
            value["package_profile"] = profile; try save()
            XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
        }
        value.removeValue(forKey: "package_profile"); try save()
        value["release_version"] = "0.4.0-macos.rc.1"; try save()
        XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
        audio["features"] = ["serial": true, "audio": false]
        components["ori-core"] = audio; value["components"] = components; try save()
        info["ORIReleaseVersion"] = "0.4.0-macos.rc.1"
        info.removeValue(forKey: "ORINativeAudioCapture"); info.removeValue(forKey: "NSMicrophoneUsageDescription")
        try saveInfo()
        var mixed = components["ori-core"] as! [String: Any]
        mixed["source_revision"] = String(repeating: "b", count: 40)
        components["ori-core"] = mixed; value["components"] = components; try save()
        XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
        mixed["source_revision"] = String(repeating: "a", count: 40)
        components["ori-core"] = mixed; value["components"] = components; try save()
        try Data("modified".utf8).write(to: root.appendingPathComponent("Contents/Helpers/ori-core"))
        XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
        mixed["path"] = "../outside"; components["ori-core"] = mixed; value["components"] = components; try save()
        XCTAssertThrowsError(try NativeRCBundle.backendID(at: root))
    }
    @MainActor
    func testDifferentInstalledBuildCannotAttachOrStartButCanExplicitlyStop() async {
        let reply = Data("{\"ok\":true,\"service_state\":\"stopped\",\"backend_id\":\"old-build\"}".utf8)
        var unregistered = false
        let model = NativeLocalService(exchange: { command, _ in
            if command == "stop" { return Data("{\"ok\":true}".utf8) }
            XCTAssertEqual(command, "status"); return reply
        }, registration: { .enabled }, unregister: { unregistered = true }, expectedBackendID: { "current-build" })
        await model.start()
        XCTAssertEqual(model.phase, .failed)
        XCTAssertNil(model.status)
        XCTAssertTrue(model.message.contains("другой сборки"))
        await model.stop()
        XCTAssertTrue(unregistered)
    }
}
