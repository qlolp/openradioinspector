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
        let metadata = root.appendingPathComponent("Contents/Resources/rc-build-info.json")
        func save() throws { try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]).write(to: metadata) }
        try save()
        let id = try NativeRCBundle.backendID(at: root)
        XCTAssertTrue(id.hasPrefix("macos-rc1-")); XCTAssertEqual(id.count, 74)
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
