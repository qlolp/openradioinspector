import Foundation
import XCTest
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif
@testable import SpektrKomandy

final class BinaryMeasurementTransportTests: XCTestCase {
    private enum FixtureError: Error { case failed(String) }
    private final class Fixture {
        let process=Process(),input=Pipe(),output=Pipe()
        private var buffered=Data()
        private let label:String
        private var closed=false
        private(set) var ready:[String:Any]=[:]
        init(mode:String="replay",count:Int=5,tls:Bool=false) throws {
            label="binary-ws/\(mode)/\(tls ? "tls" : "plain")"
            let script=try XCTUnwrap(Bundle.module.url(forResource:"binary_event_fixture",withExtension:"py",subdirectory:"Fixtures"))
            process.executableURL=URL(fileURLWithPath:"/usr/bin/env")
            process.arguments=[ProcessInfo.processInfo.environment["ORI_NATIVE_BINARY_PYTHON"] ?? "python3","-u",script.path,"--mode",mode,"--count",String(count),"--reject-reverse-dns"]+(tls ? ["--tls"]:[])
            process.standardInput=input;process.standardOutput=output
            try process.run();try input.fileHandleForReading.close();try output.fileHandleForWriting.close()
            FixtureProcessLifecycle.note("started",process:process,label:label)
            ready=try Self.read(process:process,output:output,buffered:&buffered)
            FixtureProcessLifecycle.note("ready",process:process,label:label)
        }
        private static func read(process:Process,output:Pipe,buffered:inout Data) throws->[String:Any] {
            let deadline=Date().addingTimeInterval(5)
            while Date()<deadline {
                if let end=buffered.firstIndex(of:10){let line=buffered[..<end];buffered.removeSubrange(...end);return try XCTUnwrap(JSONSerialization.jsonObject(with:line)as?[String:Any])}
                var descriptor=pollfd(fd:output.fileHandleForReading.fileDescriptor,events:Int16(POLLIN),revents:0)
                if poll(&descriptor,1,50)>0 {let bytes=output.fileHandleForReading.availableData;if bytes.isEmpty{throw FixtureError.failed("owned WS fixture exited before reply")};buffered.append(bytes);if buffered.count>65536{throw FixtureError.failed("fixture reply exceeded budget")}}
                if !process.isRunning{throw FixtureError.failed("owned WS fixture terminated")}
            }
            throw FixtureError.failed("owned WS fixture reply deadline")
        }
        func command(_ command:String) throws->[String:Any] {try input.fileHandleForWriting.write(contentsOf:Data("{\"\(command)\":true}\n".utf8));return try Self.read(process:process,output:output,buffered:&buffered)}
        var url:String {ready["url"] as! String}
        func close() throws {
            try input.fileHandleForWriting.close()
            FixtureProcessLifecycle.note("stdin-eof",process:process,label:label)
            let final=try Self.read(process:process,output:output,buffered:&buffered)
            XCTAssertEqual(final["closed"]as?Bool,true);XCTAssertEqual(final["source_unchanged"]as?Bool,true)
            XCTAssertEqual((final["stats"]as?[String:Any])?["mutations"]as?Int,0)
            FixtureProcessLifecycle.note("final-json-read",process:process,label:label)
            let status=try FixtureProcessLifecycle.waitForExit(process,label:label)
            XCTAssertEqual(status,0)
            closed=true
        }
        deinit {
            try? input.fileHandleForWriting.close()
            if !closed && process.processIdentifier>0 {
                do {try FixtureProcessLifecycle.dispose(process,input:input.fileHandleForWriting,
                        output:output.fileHandleForReading,label:label)}
                catch {XCTFail("owned WS fixture cleanup failed: \(error)")}
            } else {try? output.fileHandleForReading.close()}
        }
    }
    private actor Recorder {
        var sequences:[UInt64]=[]
        var first:PlatformMeasurementEvent?
        var last:PlatformMeasurementEvent?
        var issues:[PlatformMeasurementDisplayIssue]=[]
        var entered=false
        var hold=false
        var gate:CheckedContinuation<Void,Never>?
        init(hold:Bool=false){self.hold=hold}
        func consume(_ event:PlatformMeasurementEvent)async throws {
            entered=true
            if hold {await withCheckedContinuation{gate=$0};hold=false}
            if first==nil{first=event};last=event;sequences.append(event.sequence)
            if let issue=event.displayIssue{issues.append(issue)}
        }
        func release(){gate?.resume();gate=nil}
    }
    private func wait(_ condition:()async throws->Bool)async throws {
        for _ in 0..<500{if try await condition(){return};try await Task.sleep(nanoseconds:10_000_000)}
        throw FixtureError.failed("consumer/ACK deadline")
    }
    private func completed(_ task:Task<Void,Error>)async throws {
        do{try await task.value;XCTFail("finite fixture did not surface socket closure")}
        catch PlatformBinaryStreamError.closed(code:1001){}
    }

    func testRealURLSessionWebSocketWaitsForAppliedConsumerBeforeAckAndNextFrame()async throws {
        let fixture=try Fixture(mode:"mixed"),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder(hold:true)
        let transport=try await api.measurementTransport();XCTAssertEqual(transport,.binaryV1)
        let task=Task{try await api.consumeMeasurements(after:0,using:transport){event in try await recorder.consume(event)}}
        defer{task.cancel()}
        try await wait{await recorder.entered}
        var stats=try fixture.command("stats");XCTAssertEqual(stats["sent"]as?Int,1);XCTAssertEqual(stats["acks"]as?[Int],[])
        await recorder.release();try await completed(task)
        stats=try fixture.command("stats");XCTAssertEqual(stats["acks"]as?[Int],[1,2,3,4,5]);XCTAssertEqual(stats["bad_acks"]as?Int,0)
        let sequences=await recorder.sequences;XCTAssertEqual(sequences,[1,2,3,4,5])
        let issues=await recorder.issues;XCTAssertEqual(issues,[.unsupportedFrequency])
        let firstRecorded=await recorder.first
        let first=try XCTUnwrap(firstRecorded)
        XCTAssertEqual(first.committedEvent?.payload.sweep?.levels.map(\.bitPattern),[0x8000000000000000,0x3ff0000000000001,0xc059080000000000])
        XCTAssertEqual(first.committedEvent?.payload.sweep?.id.utf8.map{$0},Array("é".utf8))
        let lastRecorded=await recorder.last
        let last=try XCTUnwrap(lastRecorded);XCTAssertEqual(last.committedEvent?.payload.sweep?.id.utf8.map{$0},Array("e\u{301}".utf8))
        try fixture.close()
    }

    func testRealReplayPastThousandAndRestartResumesLastAppliedCursor()async throws {
        let fixture=try Fixture(count:1205),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder()
        let first=Task{try await api.consumeMeasurements(after:0,using:.binaryV1){event in try await recorder.consume(event)}}
        try await completed(first)
        let sequences=await recorder.sequences;XCTAssertEqual(sequences,Array(1...1205).map(UInt64.init))
        XCTAssertEqual(try fixture.command("restart")["restarted"]as?Bool,true)
        XCTAssertEqual(try fixture.command("append")["sequence"]as?Int,1206)
        let lastApplied=try XCTUnwrap(sequences.last)
        let resumed=Recorder();let next=Task{try await api.consumeMeasurements(after:lastApplied,using:.binaryV1){event in try await resumed.consume(event)}}
        try await completed(next)
        let replay=await resumed.sequences;XCTAssertEqual(replay,[1206])
        XCTAssertEqual(try fixture.command("stats")["cursors"]as?[String],["0","1205"])
        XCTAssertEqual(try fixture.command("stats")["mutations"]as?Int,0);try fixture.close()
    }

    func testMalformedOrTextFramesNeverAckOrAdvanceConsumer()async throws {
        for mode in ["malformed","text","oversized"] {
            let fixture=try Fixture(mode:mode,count:1),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder()
            do{try await api.consumeMeasurements(after:0,using:.binaryV1){event in try await recorder.consume(event)};XCTFail("bad frame accepted: \(mode)")}
            catch{}
            let events=await recorder.sequences;XCTAssertTrue(events.isEmpty,mode)
            XCTAssertEqual(try fixture.command("stats")["acks"]as?[Int],[],mode);try fixture.close()
        }
    }

    func testFailedUpgradeRedirectAndTLSRemainVisibleWithoutSSEOrCrossOriginRetry()async throws {
        for (mode,tls) in [("auth",false),("redirect",false),("wrong-protocol",false),("replay",true)] {
            let fixture=try Fixture(mode:mode,count:1,tls:tls),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder()
            do{try await api.consumeMeasurements(after:0,using:.binaryV1){event in try await recorder.consume(event)};XCTFail("unsafe upgrade accepted: \(mode)/TLS\(tls)")}
            catch{}
            let stats=try fixture.command("stats");XCTAssertEqual(stats["traps"]as?Int,0)
            XCTAssertFalse((stats["requests"]as?[String] ?? []).contains("/api/v1/events"))
            let events=await recorder.sequences;XCTAssertTrue(events.isEmpty);try fixture.close()
        }
    }

    func testWideDisplaySettingsAndUInt64MaxAreConsumedAsExactOriginalsAndAcknowledged()async throws {
        for (mode,cursor,issue) in [("wide-int64",UInt64(0),PlatformMeasurementDisplayIssue.unsupportedFrequency),
                                    ("opaque-settings",UInt64(0),.unsupportedSettings),
                                    ("max-cursor",UInt64.max-1,.unsupportedCursor)] {
            let fixture=try Fixture(mode:mode,count:1),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder()
            let task=Task{try await api.consumeMeasurements(after:cursor,using:.binaryV1){event in try await recorder.consume(event)}}
            try await completed(task)
            let events=await recorder.sequences,issues=await recorder.issues
            XCTAssertEqual(events,[cursor+1],mode);XCTAssertEqual(issues,[issue],mode)
            XCTAssertEqual(try fixture.command("stats")["ack_texts"]as?[String],[String(cursor+1)],mode)
            let captured=await recorder.first;let source=try XCTUnwrap(captured).source
            XCTAssertFalse(source.originalData.isEmpty)
            if mode=="opaque-settings" {
                let data=try source.metadata["data"]!.requireObject(),payload=try data["payload"]!.requireObject(),sweep=try payload["sweep"]!.requireObject(),settings=try sweep["settings"]!.requireObject()
                XCTAssertEqual(settings.count,3);XCTAssertEqual(try settings["é"]?.requireString(),"NFC");XCTAssertEqual(try settings["e\u{301}"]?.requireString(),"NFD")
            }
            try fixture.close()
        }
    }
    func testCanonicalSourceUtcSpellingRemainsCompatibleWithScientificSourceEquality()async throws {
        let fixture=try Fixture(mode:"whole-second",count:1),api=try PlatformClient(baseURL:fixture.url),recorder=Recorder()
        let task=Task{try await api.consumeMeasurements(after:0,using:.binaryV1){event in try await recorder.consume(event)}}
        try await completed(task);let captured=await recorder.first,event=try XCTUnwrap(captured)
        // Existing scientific validators compare the returned source interval
        // with the selected immutable sweep; adding .000000 would break it.
        XCTAssertEqual(event.committedEvent?.payload.sweep?.interval,PlatformInterval(startedAt:"2026-11-11T12:00:00Z",endedAt:"2026-11-11T12:00:00Z"))
        try fixture.close()
    }
}
