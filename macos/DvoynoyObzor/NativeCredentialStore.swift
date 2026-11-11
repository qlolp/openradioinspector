import Foundation
import LocalAuthentication
import Security
import CryptoKit
import SpektrKomandy

protocol NativeCredentialStoring: Sendable {
    func load(for server: PlatformBaseURL) async throws -> PlatformBearerCredential?
    func save(_ credential: PlatformBearerCredential, for server: PlatformBaseURL) async throws
    func remove(for server: PlatformBaseURL, matching credential: PlatformBearerCredential?) async throws
}
extension NativeCredentialStoring {
    func remove(for server: PlatformBaseURL) async throws { try await remove(for: server, matching: nil) }
}

/// Injectable Security framework calls; tests never touch a developer's Keychain.
struct NativeKeychainOperations: Sendable {
    let read: @Sendable (CFDictionary) -> (OSStatus, Data?)
    let update: @Sendable (CFDictionary, CFDictionary) -> OSStatus
    let add: @Sendable (CFDictionary) -> OSStatus
    let delete: @Sendable (CFDictionary) -> OSStatus
    static let live = NativeKeychainOperations(read: { query in
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query, &item)
        return (status, item as? Data)
    }, update: { SecItemUpdate($0, $1) }, add: { SecItemAdd($0, nil) }, delete: { SecItemDelete($0) })
}

/// The macOS login Keychain supports the shipped unsigned compatibility app.
/// Its default application ACL protects the generic password. No cloud sync,
/// entitlement-dependent data-protection fallback, or preferences copy is used.
/// A separate actor keeps Security daemon work away from SwiftUI's main actor.
actor NativeKeychainCredentialStore: NativeCredentialStoring {
    private let operations: NativeKeychainOperations
    init(operations: NativeKeychainOperations = .live) { self.operations = operations }

    func load(for server: PlatformBaseURL) throws -> PlatformBearerCredential? {
        guard server.root.scheme == "https" else { return nil }
        var query = query(for: server)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        let (status, bytes) = operations.read(query as CFDictionary)
        if status == errSecItemNotFound { return nil }
        try check(status)
        guard let bytes, let token = String(data: bytes, encoding: .utf8),
              let credential = try? PlatformBearerCredential(token: token) else { throw NativeCredentialStoreError.corrupt }
        return credential
    }

    func save(_ credential: PlatformBearerCredential, for server: PlatformBaseURL) throws {
        guard server.root.scheme == "https" else { throw NativeCredentialStoreError.insecureNode }
        var query = query(for: server)
        let fingerprint = Data(SHA256.hash(data: credential.dataRepresentation))
        let attributes = [kSecValueData as String: credential.dataRepresentation, kSecAttrGeneric as String: fingerprint] as CFDictionary
        let updated = operations.update(query as CFDictionary, attributes)
        if updated == errSecItemNotFound {
            query[kSecValueData as String] = credential.dataRepresentation
            query[kSecAttrGeneric as String] = fingerprint
            query[kSecAttrLabel as String] = "OpenRadioInspector native core access"
            try check(operations.add(query as CFDictionary))
        } else { try check(updated) }
    }

    func remove(for server: PlatformBaseURL, matching credential: PlatformBearerCredential?) throws {
        var query = query(for: server)
        if let credential { query[kSecAttrGeneric as String] = Data(SHA256.hash(data: credential.dataRepresentation)) }
        // An old window must not erase a newer credential saved by another window.
        let status = operations.delete(query as CFDictionary)
        if status != errSecItemNotFound { try check(status) }
    }

    private func query(for server: PlatformBaseURL) -> [String: Any] {
        var origin = URLComponents(url: server.root, resolvingAgainstBaseURL: false)!
        origin.scheme = origin.scheme?.lowercased(); origin.host = origin.host?.lowercased()
        if origin.port == 443 { origin.port = nil }
        let context = LAContext(); context.interactionNotAllowed = true
        return [kSecClass as String: kSecClassGenericPassword,
                kSecAttrService as String: "org.openradioinspector.native-core-access.v1",
                kSecAttrAccount as String: origin.url!.absoluteString,
                kSecAttrSynchronizable as String: false,
                kSecUseDataProtectionKeychain as String: false,
                kSecUseAuthenticationContext as String: context]
    }
    private func check(_ status: OSStatus) throws {
        guard status == errSecSuccess else { throw NativeCredentialStoreError.status(status) }
    }
}

enum NativeCredentialStoreError: Error, LocalizedError {
    case status(OSStatus), corrupt, insecureNode
    var errorDescription: String? {
        switch self {
        case .status(let status): return "Связка ключей недоступна (код \(status)). Разблокируйте login Keychain и проверьте доступ приложения."
        case .corrupt: return "Данные сопряжения в связке ключей повреждены. Забудьте их и выполните сопряжение заново."
        case .insecureNode: return "Данные сопряжения сохраняются только для HTTPS-узла."
        }
    }
}
