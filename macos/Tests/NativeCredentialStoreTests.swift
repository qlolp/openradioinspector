import Foundation
import LocalAuthentication
import Security
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeCredentialStoreTests: XCTestCase {
    final class Recorder: @unchecked Sendable {
        let lock = NSLock()
        var records: [(String, [String: Any])] = []
        func record(_ operation: String, _ query: CFDictionary) {
            lock.withLock { records.append((operation, query as! [String: Any])) }
        }
        var snapshot: [(String, [String: Any])] { lock.withLock { records } }
    }

    func testKeychainQueriesAreNodeScopedNonSynchronizingAndDisableAuthenticationInteraction() async throws {
        let recorder = Recorder()
        let operations = NativeKeychainOperations(
            read: { query in recorder.record("read", query); return (errSecSuccess, Data("synthetic-native-secret".utf8)) },
            update: { query, attributes in recorder.record("update", query); recorder.record("attributes", attributes); return errSecItemNotFound },
            add: { query in recorder.record("add", query); return errSecSuccess },
            delete: { query in recorder.record("delete", query); return errSecSuccess })
        let store = NativeKeychainCredentialStore(operations: operations)
        let server = try PlatformBaseURL("https://NODE.example:443/api/v1/")
        let loaded = try await store.load(for: server)
        let credential = try XCTUnwrap(loaded)
        XCTAssertEqual(credential.dataRepresentation, Data("synthetic-native-secret".utf8))
        try await store.save(credential, for: server)
        try await store.remove(for: server, matching: credential)
        let records = recorder.snapshot
        XCTAssertEqual(records.map(\.0), ["read", "update", "attributes", "add", "delete"])
        for (operation, query) in records where operation != "attributes" {
            XCTAssertEqual(query[kSecClass as String] as? String, kSecClassGenericPassword as String)
            XCTAssertEqual(query[kSecAttrAccount as String] as? String, "https://node.example")
            XCTAssertEqual(query[kSecAttrSynchronizable as String] as? Bool, false)
            XCTAssertEqual(query[kSecUseDataProtectionKeychain as String] as? Bool, false)
            XCTAssertEqual((query[kSecUseAuthenticationContext as String] as? LAContext)?.interactionNotAllowed, true)
            if operation != "add" { XCTAssertNil(query[kSecValueData as String]) }
        }
        let addition = try XCTUnwrap(records.first { $0.0 == "add" }?.1)
        XCTAssertEqual(addition[kSecValueData as String] as? Data, credential.dataRepresentation)
        let deletion = try XCTUnwrap(records.first { $0.0 == "delete" }?.1)
        let fingerprint = try XCTUnwrap(addition[kSecAttrGeneric as String] as? Data)
        XCTAssertEqual(fingerprint.count, 32)
        XCTAssertNotEqual(fingerprint, credential.dataRepresentation)
        XCTAssertEqual(deletion[kSecAttrGeneric as String] as? Data, fingerprint)
    }

    func testMissingEntryIsNotAnErrorButKeychainFailureAndCorruptDataCannotBecomeCredentials() async throws {
        let server = try PlatformBaseURL("https://node.example")
        func operations(_ status: OSStatus, _ bytes: Data?) -> NativeKeychainOperations {
            .init(read: { _ in (status, bytes) }, update: { _, _ in errSecAuthFailed },
                  add: { _ in errSecAuthFailed }, delete: { _ in errSecItemNotFound })
        }
        let missing = NativeKeychainCredentialStore(operations: operations(errSecItemNotFound, nil))
        let absent = try await missing.load(for: server); XCTAssertNil(absent)
        try await missing.remove(for: server)
        for (status, bytes) in [(errSecAuthFailed, nil), (errSecSuccess, Data([0xff])),
                                (errSecSuccess, Data("synthetic-secret\r\n".utf8))] {
            let store = NativeKeychainCredentialStore(operations: operations(status, bytes))
            do { _ = try await store.load(for: server); XCTFail("Invalid secure-store result was accepted") }
            catch { XCTAssertFalse(error.localizedDescription.contains("synthetic-secret")) }
        }
    }

    func testOwnedLoginKeychainFixtureRoundTripAndConditionalDelete() async throws {
        let store = NativeKeychainCredentialStore()
        let server = try PlatformBaseURL("https://ori-keychain-fixture-" + UUID().uuidString.lowercased() + ".invalid")
        let old = try PlatformBearerCredential(token: "owned-synthetic-keychain-old")
        let newer = try PlatformBearerCredential(token: "owned-synthetic-keychain-new")
        var created = false
        do {
            try await store.save(old, for: server); created = true
            let first = try await store.load(for: server)
            XCTAssertEqual(first?.dataRepresentation, old.dataRepresentation)
            try await store.save(newer, for: server)
            try await store.remove(for: server, matching: old)
            let retained = try await store.load(for: server)
            XCTAssertEqual(retained?.dataRepresentation, newer.dataRepresentation)
            try await store.remove(for: server, matching: newer)
            let removed = try await store.load(for: server); XCTAssertNil(removed)
        } catch {
            if created { try? await store.remove(for: server) }
            throw error
        }
        // Exact random fixture account only; never change Keychain search lists,
        // lock/unlock state, access controls or any existing user's credential.
        try await store.remove(for: server)
    }
}
