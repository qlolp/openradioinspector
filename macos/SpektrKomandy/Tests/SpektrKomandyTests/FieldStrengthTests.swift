import Foundation
import XCTest
@testable import SpektrKomandy

final class FieldStrengthTests: XCTestCase {
    private func fixture() throws -> MonitoringValue {
        let file = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../../../tests/fixtures/field-strength-rms-v2.json")
        return try JSONDecoder().decode(MonitoringValue.self, from: Data(contentsOf: file))
    }
    func testSharedPhysicalFixtureAndWrongSourceRejection() throws {
        let f = try fixture()
        let request = PlatformFieldStrengthRequest(sweepId: f["source"]["id"].text,
            frequencyHz: 200, calibration: f["request"]["calibration"])
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let result = try decoder.decode(PlatformFieldStrengthResult.self, from: JSONEncoder().encode(f["result"]))
        try result.validate(sessionID: f["source"]["session_id"].text,
                            streamID: f["source"]["stream_id"].text, request: request)
        XCTAssertEqual(result.valueDbuvPerM, 55, accuracy: 1e-9)
        XCTAssertEqual(result.value, 0.0005623413251903491, accuracy: 1e-12)
        XCTAssertThrowsError(try result.validate(sessionID: "wrong", streamID: "stream", request: request))
        XCTAssertThrowsError(try result.validate(sessionID: f["source"]["session_id"].text,
            streamID: f["source"]["stream_id"].text,
            request: .init(sweepId: request.sweepId, frequencyHz: 100, calibration: request.calibration)))
    }
    func testUnknownUncertaintyStaysUnknownAndInvalidNumbersAreRejected() throws {
        let f = try fixture()
        guard case .object(var raw) = f["result"] else { return XCTFail("Missing fixture") }
        raw["uncertainty_db"] = .null
        let decoder = JSONDecoder(); decoder.keyDecodingStrategy = .convertFromSnakeCase
        let unknown = try decoder.decode(PlatformFieldStrengthResult.self,
            from: JSONEncoder().encode(MonitoringValue.object(raw)))
        XCTAssertNil(unknown.uncertaintyDb)
        XCTAssertTrue(unknown.summary.contains("неизвестна"))
        raw["value"] = .number(-1)
        XCTAssertThrowsError(try decoder.decode(PlatformFieldStrengthResult.self,
            from: JSONEncoder().encode(MonitoringValue.object(raw))))
    }
}
