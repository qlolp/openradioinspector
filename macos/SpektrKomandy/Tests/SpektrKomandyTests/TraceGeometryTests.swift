import XCTest
@testable import SpektrKomandy

final class TraceGeometryTests: XCTestCase {
    func testTraceMeshRetainsEveryPeakAndExplicitScale() throws {
        let input = PlatformTraceRequest(current: [.init(x: 100, y: -100), .init(x: 101, y: 0), .init(x: 102, y: -100)], minimum: [], maximum: [], threshold: nil,
                                         scale: PlatformSpectrumScale(lower: -100, upper: 0), dark: false, width: 216, height: 220)
        let mesh = try PlatformTraceMesh(input)
        XCTAssertEqual(mesh.traces.last?.vertices.count, 3)
        XCTAssertEqual(mesh.traces.last?.vertices.map { $0.position.y }, [-Float(204.0 / 220.0), Float(204.0 / 220.0), -Float(204.0 / 220.0)])
        XCTAssertEqual(mesh.traces.last?.vertices.map { $0.position.x }, [-Float(200.0 / 216.0), 0, Float(200.0 / 216.0)])
    }
    func testTraceMeshHandlesExtremeFiniteLevelsAndOnePointWithoutNaN() throws {
        let input = PlatformTraceRequest(current: [.init(x: 100, y: Double.greatestFiniteMagnitude)], minimum: [], maximum: [], threshold: -Double.greatestFiniteMagnitude,
                                         scale: nil, dark: true, width: 200, height: 220)
        let mesh = try PlatformTraceMesh(input)
        XCTAssertEqual(mesh.traces.last?.vertices.count, 1)
        XCTAssertTrue(mesh.traces.flatMap(\.vertices).allSatisfy { $0.position.x.isFinite && $0.position.y.isFinite })
        XCTAssertFalse(mesh.threshold.isEmpty)
    }
}
