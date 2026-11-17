import Foundation

struct NativeAcceptanceSchedule:Sendable {
    let duration:UInt64
    let panDuration:UInt64=10_000_000_000
    let panStarts:[UInt64]
    init(seconds:Double) throws {
        guard seconds.isFinite,(1...1800).contains(seconds) else{throw CocoaError(.coderInvalidValue)}
        duration=UInt64(seconds*1_000_000_000)
        panStarts=seconds>=60 ? [duration/10,duration*4/10,duration*7/10]:[]
    }
    func interactionDue(_ index:Int)->UInt64 {UInt64(index)*duration/100}
}
