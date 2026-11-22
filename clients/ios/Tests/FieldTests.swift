import Foundation
import SpektrKomandy
import UniformTypeIdentifiers
import XCTest
@testable import Field

final class FieldTests: XCTestCase {
    func testRegisteredViewerReopensBothPortableFixtures() throws {
        let types = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "CFBundleDocumentTypes") as? [[String: Any]])
        XCTAssertEqual(types.first?["CFBundleTypeRole"] as? String, "Viewer")
        XCTAssertEqual(UTType(filenameExtension: "orifield")?.identifier, UTType.oriField.identifier)
        for (name, source, frequencies) in [
            ("field-archive-v1", Bundle.main, [Int64(100000001), 100000002, 100000003, 100000004]),
            ("field-android-export-v1", Bundle(for: FieldTests.self), [Int64(100000001), 100000003])
        ] {
            let url = try XCTUnwrap(source.url(forResource: name, withExtension: "orifield"))
            let original = try Data(contentsOf: url)
            let file = try FieldFile(url: url)
            let note = try XCTUnwrap(file.document.rows.first { $0.id.kind == .note })
            let key = try XCTUnwrap(note.link)
            guard case .sweep(let sweep, _) = try file.document.detail(for: key) else { return XCTFail("Expected a sweep") }
            XCTAssertEqual(sweep.frequenciesHz, frequencies)
            XCTAssertEqual(sweep.levels.first, -100.125)
            guard case .event(let text, _) = try file.document.detail(for: note.id) else { return XCTFail("Expected a note") }
            XCTAssertFalse(text.isEmpty)
            let fix = try XCTUnwrap(file.document.row(key)?.link.flatMap(file.document.row))
            XCTAssertEqual(fix.latitude, -0.25)
            XCTAssertEqual(fix.longitude, 0.125)
            XCTAssertEqual(try Data(contentsOf: url), original)
        }
    }

    func testReaderProfileRoundTripsAndReplacesItsScopedKeychainItem() throws {
        let store = ReaderKeychain(service: "org.openradioinspector.test." + UUID().uuidString)
        defer { try? store.clear() }
        XCTAssertNil(try store.load())
        let first = ReaderProfile(origin: "https://fixture.local", certificateDER: Data([1]), bearer: Data("synthetic".utf8))
        let next = ReaderProfile(origin: "https://second.local", certificateDER: Data([2]), bearer: Data("replacement".utf8))
        try store.save(first)
        XCTAssertEqual(try store.load()?.bearer, first.bearer)
        try store.save(next)
        XCTAssertEqual(try store.load()?.origin, next.origin)
        XCTAssertEqual(try store.load()?.certificateDER, next.certificateDER)
        XCTAssertEqual(try store.load()?.bearer, next.bearer)
        try store.clear()
        XCTAssertNil(try store.load())
    }
}
