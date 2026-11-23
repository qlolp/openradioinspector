import Foundation
import XCTest
@testable import SpektrKomandy

final class FieldDocumentTests: XCTestCase {
    private var fixture: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../contracts/fixtures/field-archive-v1.orifield").standardizedFileURL
    }

    func testSharedArchiveOpensOfflineAndNavigatesExactOriginals() throws {
        let document = try PlatformFieldDocument(opening: fixture)
        XCTAssertEqual(document.sessionID, "fixture-сессия")
        XCTAssertEqual(document.sweepCount, 1)
        XCTAssertEqual(document.observationCount, 1)
        XCTAssertEqual(document.rows.count, 5)
        let note = try XCTUnwrap(document.rows.first { $0.id.kind == .note })
        let sweepKey = try XCTUnwrap(note.link)
        guard case .sweep(let sweep, let raw) = try document.detail(for: sweepKey) else { return XCTFail("Missing sweep") }
        XCTAssertEqual(sweep.id, "fixture-sweep-α")
        XCTAssertEqual(sweep.frequenciesHz, [100000001, 100000002, 100000003, 100000004])
        XCTAssertEqual(sweep.levels[1].bitPattern, (-0.0 as Double).bitPattern)
        XCTAssertTrue(raw.contains(#""generator_output":false"#))
        let fix = try XCTUnwrap(document.row(sweepKey)?.link.flatMap { document.row($0) })
        XCTAssertEqual(fix.latitude, -0.25)
        XCTAssertEqual(fix.longitude, 0.125)
        XCTAssertEqual(document.linkedRows(fix.id).map(\.id), [sweepKey])
        let gap = try XCTUnwrap(document.rows.first { $0.id.kind == .gap })
        guard case .observation(let observation, _) = try document.detail(for: XCTUnwrap(gap.link)) else {
            return XCTFail("Missing observation")
        }
        XCTAssertEqual(observation.levelUnit, "s_meter_count")
        XCTAssertEqual(observation.level, 137)
    }

    func testCoordinatedCancellationStopsAnActiveReadBeforePublishingTheIndex() throws {
        var checks = 0
        XCTAssertThrowsError(try PlatformFieldDocument(opening: fixture) {
            checks += 1
            if checks == 4 { throw CancellationError() }
        }) { XCTAssertTrue($0 is CancellationError) }
        XCTAssertEqual(checks, 4)
        XCTAssertEqual(try PlatformFieldDocument(opening: fixture).rows.count, 5)
    }

    func testAndroidEmulatorExport() throws {
        // Android CI supplies a fresh export. Standalone SDK suites replay a saved export.
        let archive = ProcessInfo.processInfo.environment["ORI_FIELD_EMULATOR_ARCHIVE"]
            .map { URL(fileURLWithPath: $0) }
            ?? fixture.deletingLastPathComponent().appendingPathComponent("field-android-export-v1.orifield")
        let document = try PlatformFieldDocument(opening: archive)
        XCTAssertEqual(document.sweepCount, 1)
        XCTAssertEqual(document.observationCount, 0)
        XCTAssertEqual(document.rows.filter { $0.id.kind == .gap }.count, 1)
        let notes = document.rows.filter { $0.id.kind == .note }
        XCTAssertEqual(notes.count, 1)
        let key = try XCTUnwrap(notes.first?.link)
        guard case .sweep(let source, _) = try document.detail(for: key) else { return XCTFail("Missing original") }
        XCTAssertEqual(source.frequenciesHz, [100000001, 100000003])
        XCTAssertEqual(source.levels, [-100.125, -32.75])
        let fix = try XCTUnwrap(document.row(key)?.link.flatMap { document.row($0) })
        XCTAssertEqual(fix.latitude, -0.25)
        XCTAssertEqual(fix.longitude, 0.125)
        XCTAssertEqual(document.linkedRows(fix.id).map(\.id), [key])
    }

    func testAndroidPrecisionInventoryAndLargeSessions() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let tool = URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("Fixtures/field_documents.py")
        let generator = Process()
        generator.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        generator.arguments = ["python3", tool.path, fixture.path, directory.path]
        try generator.run()
        generator.waitUntilExit()
        XCTAssertEqual(generator.terminationStatus, 0)

        func open(_ name: String) throws -> PlatformFieldDocument {
            try .init(opening: directory.appendingPathComponent(name + ".orifield"))
        }
        XCTAssertEqual(try open("long-plan").sources.count, 2)
        XCTAssertEqual(try open("nanos").rows.first?.timeUTC, "2026-11-11T15:00:00.123456789+03:00")
        for invalid in ["missing-lf", "foreign-inventory", "reversed"] {
            XCTAssertThrowsError(try open(invalid), invalid)
        }
        let many = try open("long-session")
        XCTAssertEqual(many.sweepCount, 10001)
        let last = try XCTUnwrap(many.rows.last { $0.id.kind == .sweep })
        guard case .sweep(let source, _) = try many.detail(for: last.id) else { return XCTFail("Missing last sweep") }
        XCTAssertEqual(source.id, "sweep-10000")
        XCTAssertEqual(source.levels.last, -32.75)
    }

    func testDamageAndChangedSourceFailClosed() throws {
        let copy = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".orifield")
        defer { try? FileManager.default.removeItem(at: copy) }
        var bytes = try Data(contentsOf: fixture)
        try bytes.write(to: copy)
        let verified = try PlatformFieldDocument(opening: copy)
        let selected = try XCTUnwrap(verified.rows.first { $0.id.kind == .sweep })
        let match = try XCTUnwrap(bytes.range(of: Data("-100.125".utf8)))
        bytes[match.lowerBound + 1] = 50
        try bytes.write(to: copy)
        XCTAssertThrowsError(try verified.detail(for: selected.id))
        XCTAssertThrowsError(try PlatformFieldDocument(opening: copy))
        try bytes.dropLast().write(to: copy)
        XCTAssertThrowsError(try PlatformFieldDocument(opening: copy))
    }
}
