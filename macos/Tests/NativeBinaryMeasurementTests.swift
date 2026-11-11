import Foundation
import XCTest
import SpektrKomandy
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif
@testable import DvoynoyObzorSession

/// Runs on the real native target in Xcode. Its server is an owned loopback
/// protocol fixture; no RemoteDualModel/device enumeration is instantiated.
final class NativeBinaryMeasurementTests: XCTestCase {
    private final class Fixture {
        let process=Process(),input=Pipe(),output=Pipe()
        private var buffered=Data()
        var url=""
        init(mode:String="mixed")throws {
            let root=URL(fileURLWithPath:#filePath).deletingLastPathComponent().deletingLastPathComponent()
            process.executableURL=URL(fileURLWithPath:"/usr/bin/env")
            process.arguments=["python3","-u",root.appendingPathComponent("SpektrKomandy/Tests/SpektrKomandyTests/Fixtures/binary_event_fixture.py").path,"--mode",mode]
            process.standardInput=input;process.standardOutput=output;try process.run()
            try input.fileHandleForReading.close();try output.fileHandleForWriting.close()
            url=try XCTUnwrap(reply()["url"]as?String)
        }
        func reply()throws->[String:Any] {
            let deadline=Date().addingTimeInterval(5)
            while Date()<deadline {
                if let end=buffered.firstIndex(of:10){let line=buffered[..<end];buffered.removeSubrange(...end);return try XCTUnwrap(JSONSerialization.jsonObject(with:line)as?[String:Any])}
                var descriptor=pollfd(fd:output.fileHandleForReading.fileDescriptor,events:Int16(POLLIN),revents:0)
                if poll(&descriptor,1,50)>0{let bytes=output.fileHandleForReading.availableData;guard !bytes.isEmpty else{throw CocoaError(.fileReadUnknown)};buffered.append(bytes)}
                guard buffered.count<=65536,process.isRunning else{throw CocoaError(.fileReadUnknown)}
            }
            throw CocoaError(.fileReadUnknown)
        }
        func stats()throws->[String:Any]{try input.fileHandleForWriting.write(contentsOf:Data("{\"stats\":true}\n".utf8));return try reply()}
        func close()throws{try input.fileHandleForWriting.close();let result=try reply();XCTAssertEqual(result["source_unchanged"]as?Bool,true);XCTAssertEqual((result["stats"]as?[String:Any])?["mutations"]as?Int,0);let deadline=Date().addingTimeInterval(5);while process.isRunning&&Date()<deadline{Thread.sleep(forTimeInterval:0.01)};guard !process.isRunning else{throw CocoaError(.fileReadUnknown)};process.waitUntilExit();XCTAssertEqual(process.terminationStatus,0)}
        deinit{try?input.fileHandleForWriting.close();if process.isRunning{process.terminate()};if process.processIdentifier>0{process.waitUntilExit()};try?output.fileHandleForReading.close()}
    }
    @MainActor private func wait(_ condition:()->Bool)async throws{
        for _ in 0..<500{if condition(){return};try await Task.sleep(nanoseconds:10_000_000)}
        XCTFail("owned native binary consumer deadline")
    }
    @MainActor func testNativeConsumerCommitsCacheAndSourceCursorBeforeAcknowledgement()async throws {
        let fixture=try Fixture(),client=try PlatformClient(baseURL:fixture.url),stream=RemoteMeasurementStream()
        var entered=false,recovered=false,gate:CheckedContinuation<Void,Never>?
        var sources:[UInt64]=[],issues:[PlatformMeasurementDisplayIssue]=[]
        stream.start(client:client,sessionID:"synthetic-é",streamID:"a",receiverStreamID:"r",watermark:0,
            transport:.binaryV1,sweeps:[],observation:nil,receive:{sweeps,observation,event,change in
                if event.sequence==1{entered=true;await withCheckedContinuation{gate=$0}}
                try Task.checkCancellation()
                sources.append(event.sequence);if let issue=event.displayIssue{issues.append(issue)}
                if event.sequence==5{XCTAssertEqual(sweeps.map{$0.id.utf8.map{$0}},[Array("é".utf8),Array("e\u{301}".utf8)]);XCTAssertEqual(observation?.level,137);XCTAssertEqual(observation?.levelUnit,"s_meter_count")}
            },recover:{error in
                if case PlatformBinaryStreamError.closed(code:1001)=error{}else{XCTFail("unexpected native WS result: \(error)")};recovered=true
            })
        defer{gate?.resume();stream.stop()}
        try await wait{entered}
        XCTAssertEqual(stream.appliedSourceCursor,0);XCTAssertEqual(stream.measurements?.cursor,0)
        XCTAssertEqual(try fixture.stats()["acks"]as?[Int],[])
        gate?.resume();gate=nil;try await wait{recovered}
        XCTAssertEqual(sources,[1,2,3,4,5]);XCTAssertEqual(issues,[.unsupportedFrequency])
        XCTAssertEqual(stream.appliedSourceCursor,5);XCTAssertEqual(stream.measurements?.cursor,5)
        XCTAssertEqual(try fixture.stats()["acks"]as?[Int],[1,2,3,4,5]);stream.stop();try fixture.close()
    }
    @MainActor func testSupersededNativeConsumerNeverAcknowledgesLateCallback()async throws {
        let fixture=try Fixture(),client=try PlatformClient(baseURL:fixture.url),stream=RemoteMeasurementStream()
        var entered=false,returned=false,gate:CheckedContinuation<Void,Never>?,recoveries=0
        stream.start(client:client,sessionID:"synthetic-é",streamID:"a",receiverStreamID:"r",watermark:0,
            transport:.binaryV1,sweeps:[],observation:nil,receive:{_,_,_,_ in
                entered=true;await withCheckedContinuation{gate=$0};returned=true
            },recover:{_ in recoveries+=1})
        defer{gate?.resume();stream.stop()}
        try await wait{entered};stream.stop();gate?.resume();gate=nil;try await wait{returned}
        XCTAssertNil(stream.measurements);XCTAssertEqual(stream.appliedSourceCursor,0)
        XCTAssertEqual(try fixture.stats()["acks"]as?[Int],[]);XCTAssertEqual(recoveries,0);try fixture.close()
    }
    @MainActor func testReplayBeyondInt64UsesAppliedSourceCursorWithoutTruncationOrFreshHealth()async throws {
        let fixture=try Fixture(mode:"cursor-boundary"),client=try PlatformClient(baseURL:fixture.url),stream=RemoteMeasurementStream()
        var recoveries=0,issues:[PlatformMeasurementDisplayIssue]=[]
        let receive:@MainActor([PlatformSweep],PlatformObservation?,PlatformMeasurementEvent,PlatformLiveMeasurements.Change)async throws->Void={_,_,event,_ in if let issue=event.displayIssue{issues.append(issue)}}
        let recover:@MainActor(Error)async->Void={_ in recoveries+=1}
        stream.start(client:client,sessionID:"synthetic-é",streamID:"a",watermark:Int64.max-1,transport:.binaryV1,
            sweeps:[],observation:nil,receive:receive,recover:recover)
        defer{stream.stop()};try await wait{recoveries==1}
        XCTAssertEqual(stream.measurements?.cursor,Int64.max);XCTAssertEqual(stream.appliedSourceCursor,UInt64(Int64.max)+1);XCTAssertEqual(issues,[.unsupportedCursor])
        stream.start(client:client,sessionID:"synthetic-é",streamID:"a",watermark:Int64.max,transport:.binaryV1,
            sweeps:stream.measurements?.sweeps ?? [],observation:nil,receive:receive,recover:recover)
        try await wait{recoveries==2}
        XCTAssertEqual(try fixture.stats()["cursors"]as?[String],[String(Int64.max-1),String(UInt64(Int64.max)+1)])
        stream.stop();try fixture.close()
    }
}
