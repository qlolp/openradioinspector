import XCTest
@testable import SpektrKomandy

final class TinySAControlTests: XCTestCase {
    func testContextUsesValidatedServerAndEncodedScope() throws {
        let value = try PlatformTinySAControlContext(server: "http://127.0.0.1:8770/prefix/api/v1", sessionID: "s/?&", streamID: "tiny/1")
        let url = try value.url()
        XCTAssertEqual(url.path, "/prefix/instrument")
        let parts = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        XCTAssertEqual(parts.queryItems?.first(where: { $0.name == "session_id" })?.value, "s/?&")
        XCTAssertEqual(parts.queryItems?.first(where: { $0.name == "stream_id" })?.value, "tiny/1")
    }
    func testChangingAnyContextProducesDifferentIdentity() throws {
        let a = try PlatformTinySAControlContext(server: "http://localhost:8770", sessionID: "s", streamID: "t")
        let b = try PlatformTinySAControlContext(server: "http://localhost:8770", sessionID: "s", streamID: "other")
        XCTAssertNotEqual(a, b)
        XCTAssertThrowsError(try PlatformTinySAControlContext(server: "http://external.example", sessionID: "s", streamID: "t"))
        XCTAssertThrowsError(try PlatformTinySAControlContext(server: "http://localhost", sessionID: "", streamID: "t"))
    }
}
