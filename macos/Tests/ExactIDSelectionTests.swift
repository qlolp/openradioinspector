import XCTest
import SwiftUI
import SpektrKomandy
@testable import DvoynoyObzorSession

final class ExactIDSelectionTests: XCTestCase {
    @MainActor func testPickerBindingDistinguishesBothIDsAndPreservesRawStringOnWrite() {
        var raw = "é"
        let original = Binding<String>(get: { raw }, set: { raw = $0 })
        let selection = original.exactID
        XCTAssertNotEqual(selection.wrappedValue, PlatformExactID("e\u{301}"))
        selection.wrappedValue = PlatformExactID("e\u{301}")
        XCTAssertEqual(Array(raw.utf8), [101, 204, 129])
        XCTAssertEqual(selection.wrappedValue, PlatformExactID("e\u{301}"))
        XCTAssertNotEqual(selection.wrappedValue, PlatformExactID("é"))
    }
    @MainActor func testCandidateSelectionRetainsBothUnicodeIDsForCoreRequests() throws {
        let json = "[{\"id\":\"é\",\"session_id\":\"s\",\"source_stream_id\":\"a\",\"interval\":{\"started_at\":\"2026-11-11T00:00:00Z\",\"ended_at\":\"2026-11-11T00:00:01Z\"},\"center_frequency_hz\":100000000,\"level_unit\":\"dBm\"},{\"id\":\"e\u{301}\",\"session_id\":\"s\",\"source_stream_id\":\"a\",\"interval\":{\"started_at\":\"2026-11-11T00:00:00Z\",\"ended_at\":\"2026-11-11T00:00:01Z\"},\"center_frequency_hz\":100100000,\"level_unit\":\"dBm\"}]"
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let candidates = try decoder.decode([PlatformCandidate].self, from: Data(json.utf8))
        let model = FrequencyRasterModel()
        model.configure(client: try PlatformClient(baseURL: "https://example.test"), scope: .init(server: "server", session: "s"), candidates: candidates)
        model.toggle("é"); model.toggle("e\u{301}")
        XCTAssertEqual(model.chosenCandidates.map { Array($0.id.utf8) }, [[195, 169], [101, 204, 129]])
        model.toggle("é")
        XCTAssertEqual(model.chosenCandidates.map { Array($0.id.utf8) }, [[101, 204, 129]])
    }

}
