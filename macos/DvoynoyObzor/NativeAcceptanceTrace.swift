import AppKit
import CryptoKit
import Foundation
import SwiftUI
import SpektrKomandy

nonisolated enum NativeAcceptanceClock {
    static func fromMachTicks(_ ticks:UInt64)->UInt64 {
        var info=mach_timebase_info_data_t()
        guard mach_timebase_info(&info)==KERN_SUCCESS,info.denom>0 else{return 0}
        let value=ticks.multipliedFullWidth(by:UInt64(info.numer))
        guard value.high<UInt64(info.denom) else{return 0}
        return UInt64(info.denom).dividingFullWidth(value).quotient
    }
    static func now()->UInt64 {fromMachTicks(mach_absolute_time())}
}

private final class NativeAcceptanceSink: @unchecked Sendable {
    private let queue=DispatchQueue(label:"org.openradioinspector.acceptance.trace")
    private let file:FileHandle
    private var bytes=0
    private var failed=false
    private var sealed=false
    init(_ url:URL) throws {
        let descriptor=Darwin.open(url.path,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0o600)
        guard descriptor>=0 else{throw POSIXError(.EEXIST)}
        file=FileHandle(fileDescriptor:descriptor,closeOnDealloc:true)
    }
    func append(_ data:Data) {
        queue.async { [self] in
            guard !sealed else{return}
            guard !failed,data.count<=65_536,bytes+data.count<=256*1024*1024 else {failed=true;return}
            do {try file.write(contentsOf:data);bytes+=data.count}
            catch {failed=true;FileHandle.standardError.write(Data("Acceptance trace incomplete\n".utf8))}
        }
    }
    func fail() {queue.async {[self] in failed=true}}
    func flush()->Bool {queue.sync {sealed=true;do {try file.synchronize()}catch{failed=true};return !failed}}
}

@MainActor
final class NativeAcceptanceTrace {
    struct Profile:Decodable {
        let version:Int
        let runID:String
        let outputDirectory:String
        let expectedUI:String
        let coreURL:String
        let sessionID:String
        let warmupSessionID:String?
        let phase:String
        let coreDataDirectory:String?
        enum CodingKeys:String,CodingKey {
            case version,phase
            case runID="run_id",outputDirectory="output_dir",expectedUI="expected_ui_sha256"
            case coreURL="core_url",sessionID="session_id",coreDataDirectory="data_dir",warmupSessionID="warmup_session_id"
        }
    }
    static var shared:NativeAcceptanceTrace?
    let profile:Profile
    let tag:UInt32
    private let sink:NativeAcceptanceSink
    private var sources:[String:UInt64]=[:]
    private var sourceOrder:[String]=[]
    private var registry=NativeAcceptanceFrames(capacity:1024)
    private var requests:[String]=[]
    private var requestNumber=0
    private var delivered:Set<UInt64>=[]
    private var deliveredOrder:[UInt64]=[]
    private(set) var displayProbe:NativeAcceptanceDisplayProbe?
    private var probeStarting=false
    private var readyPublished=false
    private var observer:NSObjectProtocol?

    static func activate(arguments:[String]=CommandLine.arguments) throws {
        guard let index=arguments.firstIndex(of:"--acceptance-profile") else{return}
        guard arguments.indices.contains(index+1),shared==nil else{throw CocoaError(.coderInvalidValue)}
        let url=URL(fileURLWithPath:arguments[index+1])
        let info=try FileManager.default.attributesOfItem(atPath:url.path)
        guard info[.type] as? FileAttributeType == .typeRegular,
              (info[.ownerAccountID] as? NSNumber)?.uint32Value==geteuid(),
              ((info[.posixPermissions] as? NSNumber)?.intValue ?? 0)&0o077==0,
              ((info[.size] as? NSNumber)?.intValue ?? Int.max) <= 65_536 else{throw CocoaError(.fileReadNoPermission)}
        let profile=try JSONDecoder().decode(Profile.self,from:Data(contentsOf:url))
        shared=try NativeAcceptanceTrace(profile)
    }
    private init(_ profile:Profile) throws {
        guard profile.version==1,!profile.runID.isEmpty,profile.runID.utf8.count<=64,
              profile.runID.utf8.allSatisfy({(48...57).contains($0)||(65...90).contains($0)||(97...122).contains($0)||$0==45||$0==95}),
              ["load","hardware"].contains(profile.phase),
              let origin=URLComponents(string:profile.coreURL),origin.scheme=="http",origin.host=="127.0.0.1",
              origin.port != nil,origin.user==nil,origin.password==nil,origin.query==nil,origin.fragment==nil,
              origin.path.isEmpty,profile.outputDirectory.hasPrefix("/"),
              let executable=Bundle.main.executableURL else{throw CocoaError(.coderInvalidValue)}
        let hash=try SHA256.hash(data:Data(contentsOf:executable,options:.mappedIfSafe)).map{String(format:"%02x",$0)}.joined()
        guard hash==profile.expectedUI else{throw CocoaError(.fileReadCorruptFile)}
        let directory=URL(fileURLWithPath:profile.outputDirectory,isDirectory:true)
        let info=try FileManager.default.attributesOfItem(atPath:directory.path)
        guard info[.type] as? FileAttributeType == .typeDirectory,
              (info[.ownerAccountID] as? NSNumber)?.uint32Value==geteuid(),
              ((info[.posixPermissions] as? NSNumber)?.intValue ?? 0)&0o077==0 else{throw CocoaError(.fileReadNoPermission)}
        if profile.phase=="hardware" {
            guard origin.port==8774,let path=profile.coreDataDirectory,path.hasPrefix("/") else{throw CocoaError(.coderInvalidValue)}
            let info=try FileManager.default.attributesOfItem(atPath:path)
            guard info[.type] as? FileAttributeType == .typeDirectory,
                  (info[.ownerAccountID] as? NSNumber)?.uint32Value==geteuid(),
                  ((info[.posixPermissions] as? NSNumber)?.intValue ?? 0)&0o077==0 else{throw CocoaError(.fileReadNoPermission)}
        }
        if profile.phase=="hardware",let path=profile.coreDataDirectory {NativeLocalServicePaths.configureAcceptanceData(URL(fileURLWithPath:path,isDirectory:true))}
        self.profile=profile
        let digest=SHA256.hash(data:Data(profile.runID.utf8))
        tag=digest.prefix(4).reduce(UInt32(0)){($0<<8)|UInt32($1)}
        sink=try NativeAcceptanceSink(directory.appendingPathComponent("native-\(getpid()).jsonl"))
        record("native_identity",["component_sha256":hash,"phase":profile.phase,"clock":"mach_absolute_time","diagnostics_version":1])
        observer=NotificationCenter.default.addObserver(forName:NSApplication.willTerminateNotification,object:nil,queue:.main){[weak self] _ in
            MainActor.assumeIsolated {self?.finish()}
        }
    }
    func record(_ stage:String,_ fields:[String:Any]=[:],at:UInt64?=nil) {
        var value=fields;value["v"]=1;value["stage"]=stage;value["run_id"]=profile.runID
        value["at_ns"]=at ?? NativeAcceptanceClock.now()
        do {var bytes=try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]);bytes.append(10);sink.append(bytes)}
        catch {sink.fail();FileHandle.standardError.write(Data("Acceptance event could not be encoded\n".utf8))}
    }
    /// The SDK invokes this on its transport executor, before MainActor delivery.
    /// Capture only the immutable run ID and serialized sink, never actor state.
    func transportObserver()->@Sendable (PlatformMeasurementTiming)->Void {
        let runID=profile.runID,sink=sink
        return { timing in
            var fields:[String:Any] = ["v":1,"run_id":runID,"at_ns":timing.atNanoseconds,
                "sequence":timing.sequence,"transport":timing.transport == .binaryV1 ? "binary-v1":"sse",
                "payload_bytes":timing.payloadBytes]
            switch timing.stage {
            case .received,.decoded:
                guard timing.eventType=="sweep.received",let source=timing.sourceID else{return}
                fields["stage"]=timing.stage == .received ? "transport.received":"decoded"
                fields["source_id"]=source
            case .queued:
                fields["stage"]="transport.queue";fields["queued_events"]=timing.queuedEvents
            }
            do {
                var bytes=try JSONSerialization.data(withJSONObject:fields,options:[.sortedKeys]);bytes.append(10)
                sink.append(bytes)
            } catch {sink.fail()}
        }
    }
    private func finish() {
        record("native.finished")
        let successful=sink.flush()
        let target=URL(fileURLWithPath:profile.outputDirectory).appendingPathComponent("native-finished-\(getpid()).json")
        if let data=try? JSONSerialization.data(withJSONObject:["run_id":profile.runID,"successful":successful,"pid":getpid()]) {
            _=FileManager.default.createFile(atPath:target.path,contents:data,attributes:[.posixPermissions:0o600])
        }
    }
    func serviceStopped(successful:Bool,message:String) {
        let target=URL(fileURLWithPath:profile.outputDirectory).appendingPathComponent("native-service-stopped.json")
        record("service.cleanup",["successful":successful,"registration_removed":successful])
        do {
            let data=try JSONSerialization.data(withJSONObject:["run_id":profile.runID,"successful":successful,
                "resources_released":successful,"registration_removed":successful,"message":message])
            try data.write(to:target,options:.atomic)
            try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:target.path)
        } catch {sink.fail();record("native.error",["message":error.localizedDescription])}
    }
    private func key(_ id:String)->String {Data(id.utf8).base64EncodedString()}
    func received(_ id:String,sequence:UInt64) {
        let exact=key(id)
        if sources[exact]==nil {sourceOrder.append(exact)}
        sources[exact]=sequence
        if sourceOrder.count>1024 {sources.removeValue(forKey:sourceOrder.removeFirst())}
        record("received",["source_id":id,"sequence":sequence])
    }
    func applied(_ id:String,sequence:UInt64) {record("applied",["source_id":id,"sequence":sequence])}
    func sourceStage(_ stage:String,id:String,at:UInt64?=nil) {
        guard let sequence=sources[key(id)] else{return}
        record(stage,["source_id":id,"sequence":sequence],at:at)
    }
    func frame(for sourceID:String?,viewport:ClosedRange<Double>?=nil)->NativeAcceptanceFrame? {
        guard let sourceID,let sequence=sources[key(sourceID)] else{return nil}
        applied(sourceID,sequence:sequence)
        return try? registry.issue(sourceID:sourceID,sequence:sequence,requests:requests,viewport:viewport.map{[$0.lowerBound,$0.upperBound]})
    }
    func presented(_ id:UInt64,at:UInt64,method:String) {
        guard !delivered.contains(id),let value=registry.frame(id) else{return}
        delivered.insert(id);deliveredOrder.append(id)
        if deliveredOrder.count>1024 {delivered.remove(deliveredOrder.removeFirst())}
        var fields:[String:Any]=["source_id":value.sourceID,"sequence":value.sequence,"render_id":id,"method":method]
        if let viewport=value.viewport,viewport.count==2 {fields["viewport_lower_hz"]=viewport[0];fields["viewport_upper_hz"]=viewport[1]}
        record("presented",fields,at:at)
        if !readyPublished,method=="screen_capture_display_time" {
            readyPublished=true
            let ready=URL(fileURLWithPath:profile.outputDirectory).appendingPathComponent("native-ready.json")
            if let data=try? JSONSerialization.data(withJSONObject:["run_id":profile.runID,"pid":getpid(),"display_probe":true,"source_id":value.sourceID]) {
                _=FileManager.default.createFile(atPath:ready.path,contents:data,attributes:[.posixPermissions:0o600])
            }
        }
        for request in value.requests {
            guard requests.contains(request) else{continue}
            record("interaction.presented",["request_id":request],at:at)
            requests.removeAll{$0==request}
        }
    }
    func beginInteraction(at:UInt64?=nil)->String {
        requestNumber+=1;let id="ui-\(requestNumber)"
        if requests.count<1024 {requests.append(id)}else{record("native.error",["message":"interaction evidence buffer full"])}
        record("interaction.begin",["request_id":id],at:at);return id
    }
    func streamReady(sessionID:String) {
        record("stream.ready",["session_id":sessionID])
        let url=URL(fileURLWithPath:profile.outputDirectory).appendingPathComponent("native-stream-ready.json")
        do {
            let data=try JSONSerialization.data(withJSONObject:["run_id":profile.runID,"session_id":sessionID])
            try data.write(to:url,options:.atomic)
            try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:url.path)
        } catch {record("native.error",["message":error.localizedDescription])}
    }
    func observeMarker(rect:NSRect,window:NSWindow?) {
        guard profile.phase=="load",!probeStarting,displayProbe==nil,let window,
              window.isVisible,rect.width>0 else{return}
        probeStarting=true
        Task { [weak self,weak window] in
            guard let self,let window else{return}
            do {
                displayProbe=try await NativeAcceptanceDisplayProbe.start(rect:rect,window:window,tag:tag){[weak self] id,time in
                    Task { @MainActor in self?.presented(id,at:time,method:"screen_capture_display_time") }
                }
                record("display_probe.ready")

            } catch {record("native.error",["message":error.localizedDescription,"presentation":"unmeasured"])}
        }
    }
}

struct NativeAcceptanceGeometry:NSViewRepresentable {
    func makeNSView(context:Context)->MarkerGeometryView {MarkerGeometryView()}
    func updateNSView(_ view:MarkerGeometryView,context:Context) {view.needsLayout=true}
    final class MarkerGeometryView:NSView {
        override func hitTest(_ point:NSPoint)->NSView? {nil}
        override func layout() {
            super.layout()
            guard let window else{return}
            let whole=window.convertToScreen(convert(bounds,to:nil))
            let marker=NSRect(x:whole.minX+8,y:whole.maxY-8-32,width:64,height:32)
            NativeAcceptanceTrace.shared?.observeMarker(rect:marker,window:window)
        }
    }
}

extension NativeAcceptanceMarker {
    static func draw(_ context:inout GraphicsContext,frame:NativeAcceptanceFrame?,tag:UInt32) {
        guard let frame else{return}
        let code=bits(id:frame.id,tag:tag)
        for index in code.indices {
            context.fill(Path(CGRect(x:8+CGFloat(index%16)*4,y:8+CGFloat(index/16)*4,width:4,height:4)),with:.color(code[index] ? .white:.black))
        }
    }
}
