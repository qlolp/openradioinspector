import CryptoKit
import Foundation
import Security
import SpektrKomandy

struct ReaderProfile: Codable, Sendable {
    let origin: String
    let certificateDER: Data
    let bearer: Data

    func reader() throws -> PlatformFieldReader {
        guard let token = String(data: bearer, encoding: .utf8) else { throw CocoaError(.fileReadCorruptFile) }
        return try PlatformFieldReader(node: origin, certificateDER: certificateDER, credential: .init(token: token))
    }
}

/// Origin, trust anchor and bearer are replaced atomically as one device-only item.
struct ReaderKeychain {
    let service: String
    init(service: String = "org.openradioinspector.field.reader") { self.service = service }

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "local-node", kSecAttrSynchronizable as String: false]
    }

    func load() throws -> ReaderProfile? {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = result as? Data else { throw keychainError(status) }
        return try JSONDecoder().decode(ReaderProfile.self, from: data)
    }

    func save(_ profile: ReaderProfile) throws {
        let attributes: [String: Any] = [kSecValueData as String: try JSONEncoder().encode(profile),
                                        kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly]
        var status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            status = SecItemAdd(query.merging(attributes) { _, updated in updated } as CFDictionary, nil)
        }
        guard status == errSecSuccess else { throw keychainError(status) }
    }

    func clear() throws {
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw keychainError(status) }
    }

    private func keychainError(_ status: OSStatus) -> NSError {
        NSError(domain: NSOSStatusErrorDomain, code: Int(status),
                userInfo: [NSLocalizedDescriptionKey: "Связка ключей недоступна (\(status))."])
    }
}

struct ReaderCertificate {
    let der: Data
    var fingerprint: String {
        SHA256.hash(data: der).map { String(format: "%02X", $0) }.joined(separator: ":")
    }

    init(url: URL) throws {
        let access = url.startAccessingSecurityScopedResource()
        defer { if access { url.stopAccessingSecurityScopedResource() } }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        // Read one bounded file, including a possible final PEM line terminator.
        var bytes = Data()
        while bytes.count <= 65536 {
            guard let chunk = try handle.read(upToCount: 65537 - bytes.count), !chunk.isEmpty else { break }
            bytes.append(chunk)
        }
        guard bytes.count <= 65536 else { throw CocoaError(.fileReadTooLarge) }
        if let pem = String(data: bytes, encoding: .utf8) {
            let lines = pem.split(whereSeparator: \.isNewline)
            guard lines.first == "-----BEGIN CERTIFICATE-----", lines.last == "-----END CERTIFICATE-----",
                  let decoded = Data(base64Encoded: lines.dropFirst().dropLast().joined()) else {
                throw CocoaError(.fileReadCorruptFile)
            }
            bytes = decoded
        }
        guard SecCertificateCreateWithData(nil, bytes as CFData) != nil else { throw CocoaError(.fileReadCorruptFile) }
        der = bytes
    }
}
