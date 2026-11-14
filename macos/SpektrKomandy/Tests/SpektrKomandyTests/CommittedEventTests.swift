import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
@testable import SpektrKomandy

final class CommittedEventTests: XCTestCase {
    private func frame(_ sequence: Int64, id: String = "sweep-α", session: String = "s", levels: String = "[-100,-0.0,-1.0000000000000002]", type: String = "sweep.received") -> String {
        "id: \(sequence)\r\nevent: \(type)\r\ndata: {\"sequence\":\(sequence),\"event_type\":\"\(type)\",\"occurred_at\":\"2026-11-11T12:00:00.123456Z\",\"session_id\":\"\(session)\",\"payload\":{\"sweep\":{\"id\":\"\(id)\",\"session_id\":\"\(session)\",\"stream_id\":\"a\",\"interval\":{\"started_at\":\"2026-11-11T12:00:00Z\",\"ended_at\":\"2026-11-11T12:00:01Z\"},\"frequencies_hz\":[1,2,3],\"levels\":\(levels),\"level_unit\":\"dBm\"}}}\r\n\r\n"
    }
    private func event(_ sequence: Int64, id: String = "sweep-α", session: String = "s") throws -> PlatformCommittedEvent {
        var parser = PlatformEventParser(); return try XCTUnwrap(parser.append(Data(frame(sequence, id: id, session: session).utf8)).first)
    }

    private func wrapped(_ frame: String, contract: String = "openradioinspector-platform", version: Int = 1) -> String {
        let pieces = frame.components(separatedBy: "data: ")
        let json = pieces[1].trimmingCharacters(in: .whitespacesAndNewlines)
        return pieces[0] + "data: {\"contract\":\"\(contract)\",\"contract_version\":\(version),\"data\":\(json)}\n\n"
    }

    func testRealAPIEnvelopeRetainsOriginalWireBytesAndFloatBits() throws {
        let wire = wrapped(frame(7))
        var parser = PlatformEventParser()
        let value = try XCTUnwrap(parser.append(Data(wire.utf8)).first)
        XCTAssertEqual(value.sequence, 7)
        XCTAssertEqual(value.payload.sweep?.levels.map(\.bitPattern), [0xc059000000000000, 0x8000000000000000, 0xbff0000000000001])
        XCTAssertEqual(value.sourceJSON, Data(wire.components(separatedBy: "data: ")[1].trimmingCharacters(in: .whitespacesAndNewlines).utf8))
    }

    func testStreamingDecoderDeliversBoundedPrefixBeforeMalformedTail() throws {
        var parser = PlatformEventParser(), received: [Int64] = []
        let frames = (1...70).map { wrapped(frame(Int64($0))) }.joined()
        XCTAssertThrowsError(try parser.append(Data(frames.utf8)) { event in
            guard received.count < 64 else { throw PlatformEventError.consumerOverrun }
            received.append(event.sequence)
        })
        XCTAssertEqual(received, Array(1...64).map(Int64.init))
        parser = PlatformEventParser(); received = []
        XCTAssertThrowsError(try parser.append(Data((wrapped(frame(1)) + "id: 2\nevent: future\ndata: invalid\n\n").utf8)) {
            received.append($0.sequence)
        })
        XCTAssertEqual(received, [1])
    }

    func testWrappedWrongContractOrVersionNeverFallsBackToBareEvent() throws {
        for wire in [wrapped(frame(7), contract: "other"), wrapped(frame(7), version: 2),
                     frame(7).replacingOccurrences(of: "\"sequence\":7", with: "\"contract\":\"other\",\"sequence\":7")] {
            var parser = PlatformEventParser()
            XCTAssertThrowsError(try parser.append(Data(wire.utf8))) { error in
                XCTAssertEqual(error as? PlatformEventError, .malformedEvent)
            }
        }
    }

    func testSSEChunksSplitUTF8CRLFAndHeartbeatWithoutLosingMeasurements() throws {
        var parser = PlatformEventParser(); var received: [PlatformCommittedEvent] = []
        let bytes = Data((": keep-alive\r\n\r\n" + frame(7) + frame(8, id: "next")).utf8)
        for byte in bytes { received += try parser.append(Data([byte])) }
        XCTAssertEqual(received.map(\.sequence), [7, 8])
        XCTAssertEqual(Array(try XCTUnwrap(received.first?.payload.sweep?.id).utf8), Array("sweep-α".utf8))
        XCTAssertEqual(received[0].payload.sweep?.levels.map(\.bitPattern), [0xc059000000000000, 0x8000000000000000, 0xbff0000000000001])
        XCTAssertEqual(parser.bufferedBytes, 0)
    }

    func testRejectsOversizedOrMismatchedEventInsteadOfUnboundedAccumulation() throws {
        var parser = PlatformEventParser(maximumEventBytes: 32)
        XCTAssertThrowsError(try parser.append(Data(repeating: 65, count: 33)))
        parser = PlatformEventParser()
        XCTAssertThrowsError(try parser.append(Data(frame(7).replacingOccurrences(of: "id: 7", with: "id: 8").utf8)))
        parser = PlatformEventParser()
        XCTAssertThrowsError(try parser.append(Data(frame(7).replacingOccurrences(of: "event: sweep.received", with: "event: other").utf8)))
    }

    func testCursorDetectsGlobalSequenceGapsDeduplicatesAndBoundsLocalHistory() throws {
        var live = PlatformLiveMeasurements(sessionID: "s", streamID: "a", after: 10, capacity: 2)
        XCTAssertEqual(try live.apply(event(11)), .measurement)
        XCTAssertEqual(try live.apply(event(11)), .duplicate)
        XCTAssertEqual(try live.apply(event(12, id: "foreign", session: "foreign")), .metadata)
        XCTAssertEqual(try live.apply(event(13, id: "sweep-β")), .measurement)
        XCTAssertEqual(try live.apply(event(14, id: "sweep-γ")), .measurement)
        XCTAssertEqual(live.sweeps.map(\.id), ["sweep-β", "sweep-γ"])
        XCTAssertEqual(live.cursor, 14)
        XCTAssertThrowsError(try live.apply(event(16))) { error in
            XCTAssertEqual(error as? PlatformEventError, .sequenceGap(expected: 15, received: 16))
        }
        XCTAssertEqual(live.cursor, 14)
    }

    func testOneLargeNetworkChunkCannotCreateAnUnboundedEventBatch() throws {
        var parser = PlatformEventParser()
        let batch = (1...65).map { frame(Int64($0)) }.joined()
        XCTAssertThrowsError(try parser.append(Data(batch.utf8))) { error in
            XCTAssertEqual(error as? PlatformEventError, .consumerOverrun)
        }
    }

    func testMeasurementPayloadCannotClaimAnotherEventSession() throws {
        var parser = PlatformEventParser()
        let bad = frame(1).replacingOccurrences(of: "\"session_id\":\"s\",\"payload\"", with: "\"session_id\":\"other\",\"payload\"")
        XCTAssertThrowsError(try parser.append(Data(bad.utf8)))
    }

    func testLateCommittedSweepKeepsArchiveTimeThenUTF8IDOrder() throws {
        func at(_ sequence: Int64, _ id: String, _ microseconds: String) throws -> PlatformCommittedEvent {
            var parser = PlatformEventParser()
            let text = frame(sequence, id: id).replacingOccurrences(of: "2026-11-11T12:00:00Z", with: "2026-11-11T12:00:00." + microseconds + "Z")
            return try XCTUnwrap(parser.append(Data(text.utf8)).first)
        }
        var live = PlatformLiveMeasurements(sessionID: "s", streamID: "a", after: 0, capacity: 2)
        _ = try live.apply(at(1, "new", "000002"))
        _ = try live.apply(at(2, "old", "000001"))
        XCTAssertEqual(live.sweeps.map(\.id), ["old", "new"])
        _ = try live.apply(at(3, "earlier", "000000"))
        XCTAssertEqual(live.sweeps.map(\.id), ["old", "new"])
        _ = try live.apply(at(4, "z", "000002"))
        XCTAssertEqual(live.sweeps.map(\.id), ["new", "z"])
    }

    func testReceiverStreamFilterAndLateObservationPreserveLatestRawSMeter() throws {
        func observation(_ sequence: Int, _ stream: String, _ second: Int, _ level: Int) throws -> PlatformCommittedEvent {
            var parser = PlatformEventParser()
            let json = "{\"sequence\":\(sequence),\"event_type\":\"observation.received\",\"occurred_at\":\"2026-11-11T00:00:03Z\",\"session_id\":\"s\",\"payload\":{\"observation\":{\"id\":\"o\(sequence)\",\"session_id\":\"s\",\"stream_id\":\"\(stream)\",\"interval\":{\"started_at\":\"2026-11-11T00:00:0\(second)Z\",\"ended_at\":\"2026-11-11T00:00:04Z\"},\"frequency_hz\":100000001,\"mode\":\"FM\",\"level_unit\":\"s_meter_count\",\"level\":\(level)}}}"
            return try XCTUnwrap(parser.append(Data("id: \(sequence)\nevent: observation.received\ndata: \(json)\n\n".utf8)).first)
        }
        var live = PlatformLiveMeasurements(sessionID: "s", streamID: "a", after: 0, receiverStreamID: "receiver")
        _ = try live.apply(observation(1, "receiver", 2, 137))
        _ = try live.apply(observation(2, "foreign", 3, 777))
        _ = try live.apply(observation(3, "receiver", 1, 10))
        XCTAssertEqual(live.observation?.level, 137)
        XCTAssertEqual(live.observation?.levelUnit, "s_meter_count")
        XCTAssertEqual(live.observation?.frequencyHz, 100000001)
        XCTAssertEqual(live.cursor, 3)
    }

    func testBoundedReplayRetainsDistinctUnicodeIDsThroughDeduplicationAndEviction() throws {
        var live = PlatformLiveMeasurements(sessionID: "s", streamID: "a", after: 0, capacity: 2)
        _ = try live.apply(event(1, id: "é"))
        _ = try live.apply(event(2, id: "e\u{301}"))
        XCTAssertEqual(live.sweeps.map { Array($0.id.utf8) }, [[101, 204, 129], [195, 169]])
        XCTAssertEqual(try live.apply(event(3, id: "e\u{301}")), .duplicate)
        _ = try live.apply(event(4, id: "z"))
        XCTAssertEqual(live.sweeps.map { Array($0.id.utf8) }, [[122], [195, 169]])
        XCTAssertEqual(live.sweeps.count, 2)
        XCTAssertEqual(live.cursor, 4)
    }

    func testSnapshotReplayUsesByteExactIDsAndDoesNotAppendDuplicateSweep() throws {
        let first = try event(1, id: "é").payload.sweep!
        var live = PlatformLiveMeasurements(sessionID: "s", streamID: "a", after: 1, sweeps: [first])
        _ = try live.apply(event(2, id: "é"))
        _ = try live.apply(event(3, id: "é"))
        XCTAssertEqual(live.sweeps.count, 2)
        XCTAssertEqual(live.sweeps.map { Array($0.id.utf8) }, [Array("é".utf8), Array("é".utf8)])
    }
}
