import Foundation
import CryptoKit
import Security
import CoreFoundation

/// Validation uses only bundled code and system frameworks. It never registers
/// an agent, opens a port or creates an archive.
enum NativeRCBundle {
    static var release: String { Bundle.main.object(forInfoDictionaryKey: "ORIReleaseVersion") as? String ?? "0.4.0-macos.rc.1" }
    static var nativeAudioPackage: Bool {
        release == "0.4.0-macos.rc.2" &&
        boolean(Bundle.main.object(forInfoDictionaryKey: "ORINativeAudioCapture")) == true &&
        !(Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String ?? "").isEmpty
    }
    private struct Metadata: Decodable {
        let schemaVersion: Int
        let releaseVersion, bundleIdentifier, sourceRevision, architecture: String
        let components: [String: Component]
        let packageProfile: String?
        enum CodingKeys: String, CodingKey {
            case architecture, components
            case schemaVersion = "schema_version", releaseVersion = "release_version"
            case bundleIdentifier = "bundle_identifier", sourceRevision = "source_revision"
            case packageProfile = "package_profile"
        }
        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            schemaVersion = try values.decode(Int.self, forKey: .schemaVersion)
            releaseVersion = try values.decode(String.self, forKey: .releaseVersion)
            bundleIdentifier = try values.decode(String.self, forKey: .bundleIdentifier)
            sourceRevision = try values.decode(String.self, forKey: .sourceRevision)
            architecture = try values.decode(String.self, forKey: .architecture)
            components = try values.decode([String: Component].self, forKey: .components)
            packageProfile = values.contains(.packageProfile) ? try values.decode(String.self, forKey: .packageProfile) : nil
        }
    }
    private struct Component: Decodable {
        let path, sha256, component, sourceRevision, targetArch, targetOS: String
        let features: [String: Bool]?
        enum CodingKeys: String, CodingKey {
            case path, sha256, component, features
            case sourceRevision = "source_revision", targetArch = "target_arch", targetOS = "target_os"
        }
    }
    private static func hexadecimal(_ digest: SHA256.Digest) -> String {
        digest.map { String(format: "%02x", $0) }.joined()
    }
    private static func boolean(_ value: Any?) -> Bool? {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
        return number.boolValue
    }
    private static func validateUIProfile(at root: URL, release: String, audio: Bool) throws {
        let path = root.appendingPathComponent("Contents/Info.plist")
        let attributes = try path.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              let size = attributes.fileSize, size > 0, size <= 65_536,
              let info = try PropertyListSerialization.propertyList(from: Data(contentsOf: path), options: [], format: nil) as? [String: Any],
              info["CFBundleIdentifier"] as? String == "org.openradioinspector.macos.rc",
              info["CFBundleExecutable"] as? String == "OpenRadioInspectorRC",
              info["ORIReleaseVersion"] as? String == release,
              boolean(info["ORIPersonalRC"]) == true else { throw invalid() }
        let declaredAudio = info["ORINativeAudioCapture"] == nil ? false : boolean(info["ORINativeAudioCapture"])
        guard declaredAudio == audio else { throw invalid() }
        if audio {
            guard let purpose = info["NSMicrophoneUsageDescription"] as? String,
                  !purpose.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                  purpose.utf8.count <= 4096 else { throw invalid() }
        } else if info["NSMicrophoneUsageDescription"] != nil { throw invalid() }
    }
    private static func hashFile(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hash = SHA256()
        while let data = try handle.read(upToCount: 1_048_576), !data.isEmpty { hash.update(data: data) }
        return hexadecimal(hash.finalize())
    }
    static func backendID(at bundle: URL) throws -> String {
        let root = bundle.resolvingSymlinksInPath()
        let path = root.appendingPathComponent("Contents/Resources/rc-build-info.json")
        let attributes = try path.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey, .isSymbolicLinkKey])
        guard attributes.isRegularFile == true, attributes.isSymbolicLink != true,
              let size = attributes.fileSize, size > 0, size <= 65_536 else { throw invalid() }
        let bytes = try Data(contentsOf: path)
        let metadata = try JSONDecoder().decode(Metadata.self, from: bytes)
        let prefix: String, audio: Bool, profile: String
        switch metadata.releaseVersion {
        case "0.4.0-macos.rc.1": prefix = "macos-rc1"; audio = false; profile = "rc1"
        case "0.4.0-macos.rc.2": prefix = "macos-rc2-audio"; audio = true; profile = "rc2-audio"
        default: throw invalid()
        }
        guard metadata.schemaVersion == 1, metadata.packageProfile == nil || metadata.packageProfile == profile,
              metadata.bundleIdentifier == "org.openradioinspector.macos.rc", metadata.architecture == "arm64",
              metadata.sourceRevision.count == 40,
              metadata.sourceRevision.allSatisfy({ "0123456789abcdef".contains($0) }) else { throw invalid() }
        try validateUIProfile(at: root, release: metadata.releaseVersion, audio: audio)
        for name in ["ori-core", "ori-supervisor"] {
            guard let item = metadata.components[name], item.component == name,
                  item.sourceRevision == metadata.sourceRevision, item.targetArch == "aarch64", item.targetOS == "macos",
                  item.path == "Contents/Helpers/" + name else { throw invalid() }
            if name == "ori-core", item.features != ["serial": true, "audio": audio] { throw invalid() }
            let helper = root.appendingPathComponent(item.path)
            let values = try helper.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true,
                  helper.resolvingSymlinksInPath() == helper.standardizedFileURL,
                  try hashFile(helper) == item.sha256 else { throw invalid() }
        }
        return prefix + "-" + hexadecimal(SHA256.hash(data: bytes))
    }
    static func validateCurrentInstallation(requireWritable: Bool = true) throws -> String {
        let root = Bundle.main.bundleURL
        if requireWritable, try root.resourceValues(forKeys: [.volumeIsReadOnlyKey]).volumeIsReadOnly != false {
            throw NativeServiceError.unavailable("Скопируйте RC в «Программы» и извлеките DMG перед запуском службы.")
        }
        guard Bundle.main.bundleIdentifier == "org.openradioinspector.macos.rc" else { throw invalid() }
        var code: SecStaticCode?
        guard SecStaticCodeCreateWithPath(root as CFURL, SecCSFlags(rawValue: 0), &code) == errSecSuccess,
              let code, SecStaticCodeCheckValidity(code, SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckNestedCode), nil) == errSecSuccess else {
            throw NativeServiceError.unavailable("Подпись или целостность RC повреждена. Установите проверенный пакет заново.")
        }
        return try backendID(at: root)
    }
    private static func invalid() -> NativeServiceError {
        .unavailable("Состав RC не совпадает с манифестом: требуются UI, ori-core и ori-supervisor одной сборки arm64.")
    }
}
