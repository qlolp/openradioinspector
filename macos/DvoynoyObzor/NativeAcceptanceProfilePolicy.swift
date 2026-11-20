import Foundation

@MainActor
enum NativeAcceptanceProfilePolicy {
    static func origin(for profile: NativeAcceptanceTrace.Profile) throws -> URLComponents {
        guard profile.version == 1, !profile.runID.isEmpty, profile.runID.utf8.count <= 64,
              profile.runID.utf8.allSatisfy({ (48...57).contains($0) || (65...90).contains($0) || (97...122).contains($0) || $0 == 45 || $0 == 95 }),
              ["load", "hardware", "ui-test"].contains(profile.phase),
              profile.transport == nil || ["auto", "sse", "binary-v1"].contains(profile.transport!),
              profile.smokeSeconds == nil || (profile.phase == "load" && profile.smokeSeconds!.isFinite && (1...60).contains(profile.smokeSeconds!)),
              let origin = URLComponents(string: profile.coreURL), origin.scheme == "http", origin.host == "127.0.0.1",
              origin.port != nil, origin.user == nil, origin.password == nil, origin.query == nil,
              origin.fragment == nil, origin.path.isEmpty, profile.outputDirectory.hasPrefix("/") else {
            throw CocoaError(.coderInvalidValue)
        }
        if profile.phase == "hardware" {
            guard origin.port == 8774, let path = profile.coreDataDirectory, path.hasPrefix("/") else {
                throw CocoaError(.coderInvalidValue)
            }
        }
        if profile.phase == "ui-test", profile.coreDataDirectory != nil || profile.transport != nil {
            throw CocoaError(.coderInvalidValue)
        }
        return origin
    }
}
