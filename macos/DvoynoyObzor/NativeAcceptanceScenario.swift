import Foundation

@MainActor
enum NativeAcceptanceScenario {
    private struct Handlers {let pan:@MainActor (Double)->Void;let zoom:@MainActor (Double)->Void}
    private static var lifecycle=NativeAcceptanceScenarioLifecycle()
    private static var handlers:Handlers?
    private static var driver:Task<Void,Never>?
    static func attach(session:String,stream:String,sourceSession:String?,sourceStream:String?,pan:@escaping @MainActor (Double)->Void,zoom:@escaping @MainActor (Double)->Void)->UInt64? {
        guard let trace=NativeAcceptanceTrace.shared,trace.profile.phase=="load",
              let token=lifecycle.attach(session:session,stream:stream,sourceSession:sourceSession,sourceStream:sourceStream,expected:trace.profile.sessionID) else{return nil}
        handlers=Handlers(pan:pan,zoom:zoom)
        if driver==nil {driver=Task{await run(trace)}}
        else {_=trace.beginInteraction();zoom(0.5)}
        return token
    }
    static func detach(_ token:UInt64) {if lifecycle.detach(token) {handlers=nil}}
    private static func current() async -> Handlers? {
        for _ in 0..<100 {
            if let handlers{return handlers}
            do {try await Task.sleep(for:.milliseconds(20))}catch{return nil}
        }
        NativeAcceptanceTrace.shared?.record("native.error",["message":"Native scenario lost its visible workload inspector"])
        return nil
    }
    private static func run(_ trace:NativeAcceptanceTrace) async {
        let anchor=NativeAcceptanceClock.now()
        guard let schedule=try? NativeAcceptanceSchedule(seconds:trace.profile.smokeSeconds ?? 1800) else {
            trace.record("native.error",["message":"Invalid native scenario duration"]);return
        }
        trace.record("scenario.begin",["input":"scheduled_native_actions","duration_seconds":Double(schedule.duration)/1e9])
        // Independent schedules prevent a ten-second pan from delaying queued
        // input probes in the compressed smoke. The full-run times are unchanged.
        async let inputs:Void=runInputs(trace,anchor:anchor,schedule:schedule)
        async let pans:Void=runPans(trace,anchor:anchor,schedule:schedule)
        _=await (inputs,pans)
        trace.record("scenario.completed")
    }
    private static func runInputs(_ trace:NativeAcceptanceTrace,anchor:UInt64,schedule:NativeAcceptanceSchedule) async {
        for index in 0..<100 {
            let due=anchor+schedule.interactionDue(index)
            guard await wait(until:due) else{return}
            _=trace.beginInteraction(at:due)
            guard let view=await current() else{return}
            if index==0 {view.zoom(0.5)}else{view.pan(index%2==0 ? 0.02:-0.02)}
        }
    }
    private static func runPans(_ trace:NativeAcceptanceTrace,anchor:UInt64,schedule:NativeAcceptanceSchedule) async {
        for (index,offset) in schedule.panStarts.enumerated() {
            guard await wait(until:anchor+offset) else{return}
            let id="pan-\(index)",start=NativeAcceptanceClock.now()
            trace.record("pan_zoom.begin",["request_id":id],at:start)
            for tick in 1...600 {
                let next=start+UInt64(tick)*16_666_667
                guard await wait(until:next) else{return}
                _=trace.beginInteraction(at:next)
                guard let view=await current() else{return}
                view.pan(tick%2==0 ? 0.002:-0.002)
            }
            trace.record("pan_zoom.end",["request_id":id])
        }
    }
    private static func wait(until target:UInt64) async -> Bool {
        let now=NativeAcceptanceClock.now()
        if target>now {do {try await Task.sleep(nanoseconds:target-now)}catch{return false}}
        return !Task.isCancelled
    }
}
