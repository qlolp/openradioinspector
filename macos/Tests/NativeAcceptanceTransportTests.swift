import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeAcceptanceTransportTests:XCTestCase {
    func testForcedBinaryRequiresAnAdvertisedSupportedProtocol() throws {
        XCTAssertThrowsError(try NativeAcceptanceTransport.resolve("binary-v1",offered:.serverSentEvents))
        XCTAssertEqual(try NativeAcceptanceTransport.resolve("binary-v1",offered:.binaryV1),.binaryV1)
        XCTAssertThrowsError(try NativeAcceptanceTransport.resolve("future",offered:.binaryV1))
    }
    func testSSECanBeForcedWhileDefaultFollowsCapabilities() throws {
        XCTAssertEqual(try NativeAcceptanceTransport.resolve("sse",offered:.binaryV1),.serverSentEvents)
        XCTAssertEqual(try NativeAcceptanceTransport.resolve(nil,offered:.binaryV1),.binaryV1)
        XCTAssertEqual(try NativeAcceptanceTransport.resolve("auto",offered:.serverSentEvents),.serverSentEvents)
    }
}
