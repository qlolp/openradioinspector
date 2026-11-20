import AppKit
import CoreGraphics
import CryptoKit
import Foundation

/// Read-only probe executed by the candidate itself, so its TCC identity is
/// measured. It never requests a permission, registers a service or opens USB.
@MainActor
enum NativeAcceptanceHostProbe {
    static func run(directory:URL) throws -> [String:Any] {
        _=try NativeRCBundle.validateCurrentInstallation()
        let attributes=try FileManager.default.attributesOfItem(atPath:directory.path)
        guard attributes[.type] as? FileAttributeType == .typeDirectory,
            (attributes[.ownerAccountID] as? NSNumber)?.uint32Value==geteuid(),
            ((attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0)&0o077==0,
            directory.standardizedFileURL==directory.resolvingSymlinksInPath(),
            let executable=Bundle.main.executableURL else{throw CocoaError(.fileReadNoPermission)}
        let volume=try directory.resourceValues(forKeys:[.volumeIsLocalKey,.volumeIsReadOnlyKey])
        let screen=NSScreen.main?.visibleFrame ?? .zero
        let session=CGSessionCopyCurrentDictionary() as? [String:Any]
        let console=session?[kCGSessionOnConsoleKey as String] as? Bool == true
        let locked=session?["CGSSessionScreenIsLocked"] as? Bool == true
        let hash=SHA256.hash(data:try Data(contentsOf:executable,options:.mappedIfSafe)).map{String(format:"%02x",$0)}.joined()
        return ["schema_version":1,"native_sha256":hash,"screen_capture_allowed":CGPreflightScreenCaptureAccess(),
            "screen_locked":!console || locked,"screen_width":Double(screen.width),"screen_height":Double(screen.height),
            "volume_is_local":volume.volumeIsLocal==true,"volume_is_read_only":volume.volumeIsReadOnly != false,
            "service_started":false,"hardware_opened":false]
    }
}
