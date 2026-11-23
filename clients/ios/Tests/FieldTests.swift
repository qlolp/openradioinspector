import Foundation
import SpektrKomandy
import UniformTypeIdentifiers
import XCTest
@testable import Field

final class FieldTests: XCTestCase {
    func testRegisteredViewerReopensBothPortableFixtures() async throws {
        let types = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "CFBundleDocumentTypes") as? [[String: Any]])
        XCTAssertEqual(types.first?["CFBundleTypeRole"] as? String, "Viewer")
        XCTAssertEqual(UTType(filenameExtension: "orifield")?.identifier, UTType.oriField.identifier)
        for (name, source, frequencies) in [
            ("field-archive-v1", Bundle.main, [Int64(100000001), 100000002, 100000003, 100000004]),
            ("field-android-export-v1", Bundle(for: FieldTests.self), [Int64(100000001), 100000003])
        ] {
            let url = try XCTUnwrap(source.url(forResource: name, withExtension: "orifield"))
            let original = try Data(contentsOf: url)
            let file = try await FieldFile.open(url)
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

    func testCancelledFilesRequestCanBeReplacedByAWorkingArchive() async throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "field-archive-v1", withExtension: "orifield"))
        let discarded = Task {
            while !Task.isCancelled { await Task.yield() }
            return try await FieldFile.open(url)
        }
        discarded.cancel()
        do {
            _ = try await discarded.value
            XCTFail("A superseded file request returned a document")
        } catch is CancellationError {}
        let replacement = try await FieldFile.open(url)
        XCTAssertEqual(replacement.document.sweepCount, 1)
    }

    func testReaderAgainstLocalRustTLSArchive() async throws {
        struct Fixture: Decodable { let origin, code: String; let ca, otherCA: Data }
        guard let input = ProcessInfo.processInfo.environment["ORI_READER_FIXTURE"] else {
            throw XCTSkip("Set ORI_FIELD_CORE when running the simulator script for the disposable Rust TLS fixture.")
        }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(input.utf8))
        XCTAssertEqual(try PlatformFieldReader.origin(fixture.origin).host, "127.0.0.1")
        for (host, ca) in [(fixture.origin, fixture.otherCA),
                           (fixture.origin.replacingOccurrences(of: "127.0.0.1", with: "localhost"), fixture.ca)] {
            let wrong = try PlatformFieldReader(node: host, certificateDER: ca)
            do { _ = try await wrong.pair(code: "unused"); XCTFail("Accepted an untrusted node") }
            catch PlatformError.http { XCTFail("HTTP request reached a node with an untrusted chain or name") }
            catch {}
        }
        let origin = try PlatformFieldReader(node: fixture.origin, certificateDER: fixture.ca)
        let token = try await origin.pair(code: fixture.code)
        let first = try PlatformFieldReader(node: fixture.origin, certificateDER: fixture.ca, credential: token)
        try await first.verify()
        let sessions = try await first.sessions()
        let session = try XCTUnwrap(sessions.first)
        let stream = try XCTUnwrap(session.streams.first { $0.role == "spectrum_analyzer" })
        let receiver = try XCTUnwrap(session.streams.first { $0.isReceiver })
        let page = try await first.history(session: session.id, stream: stream.id)
        let row = try XCTUnwrap(page.items.first)
        let sweep = try await first.sweep(session: session.id, stream: stream.id, id: row.id)
        XCTAssertEqual(sweep.frequenciesHz, [100000001, 100000002, 100000003, 100000004])
        XCTAssertEqual(sweep.levels[1].bitPattern, (-0.0 as Double).bitPattern)
        let observations = try await first.observations(session: session.id, stream: receiver.id)
        let observation = try XCTUnwrap(observations.first)
        XCTAssertEqual(observation.frequencyHz, 100000003)
        XCTAssertEqual(observation.level, 137)
        XCTAssertEqual(observation.levelUnit, "s_meter_count")

        var journal = PlatformFieldJournal(watermark: 0)
        try journal.append(await first.events(after: journal.cursor, limit: 1))
        let saved = journal.cursor
        let resumed = try PlatformFieldReader(node: fixture.origin, certificateDER: fixture.ca, credential: token)
        try await resumed.verify()
        let watermark = try await resumed.watermark()
        while journal.cursor < watermark {
            let page = try await resumed.events(after: journal.cursor)
            guard !page.isEmpty else { return XCTFail("Incomplete replay after reconnect") }
            try journal.append(page)
        }
        XCTAssertGreaterThan(journal.cursor, saved)
        XCTAssertTrue(journal.recent.contains { $0.type == "observation.received" })
        try await resumed.revoke()
        do { try await first.verify(); XCTFail("Revoked reader remained usable") } catch is PlatformError {}
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
