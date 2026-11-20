import Foundation

struct NativeAcceptanceScenarioLifecycle {
    private(set) var owner:UInt64?
    private var next:UInt64=0
    mutating func attach(session:String,stream:String,sourceSession:String?,sourceStream:String?,expected:String)->UInt64? {
        guard session==expected,!stream.isEmpty,sourceSession==session,sourceStream==stream,next<UInt64.max else{return nil}
        next+=1;owner=next;return next
    }
    mutating func detach(_ token:UInt64)->Bool {
        guard owner==token else{return false};owner=nil;return true
    }
}

struct NativeAcceptanceFrame: Sendable {
    let id: UInt64
    let sourceID: String
    let sequence: UInt64
    let requests: [String]
    let viewport:[Double]?
}
struct NativeAcceptanceFrames {
    let capacity: Int
    private var next: UInt64 = 0
    private var values: [NativeAcceptanceFrame] = []
    mutating func issue(sourceID: String, sequence: UInt64, requests: [String], viewport:[Double]?=nil) throws -> NativeAcceptanceFrame {
        guard capacity > 0, !sourceID.isEmpty, sequence > 0, next < UInt64.max else { throw CocoaError(.coderInvalidValue) }
        next += 1
        let value=NativeAcceptanceFrame(id:next,sourceID:sourceID,sequence:sequence,requests:requests,viewport:viewport)
        values.append(value)
        if values.count>capacity {values.removeFirst(values.count-capacity)}
        return value
    }
    func frame(_ id:UInt64)->NativeAcceptanceFrame? {values.first{$0.id==id}}
}

enum NativeAcceptanceMarker {
    private static func checksum(_ bytes:[UInt8])->UInt32 {
        bytes.reduce(UInt32(2166136261)){($0 ^ UInt32($1)) &* 16777619}
    }
    static func bits(id:UInt64,tag:UInt32)->[Bool] {
        var bytes=(0..<4).map{UInt8(truncatingIfNeeded:tag >> (24-$0*8))}
        bytes += (0..<8).map{UInt8(truncatingIfNeeded:id >> (56-$0*8))}
        let sum=checksum(bytes)
        bytes += (0..<4).map{UInt8(truncatingIfNeeded:sum >> (24-$0*8))}
        return bytes.flatMap{byte in (0..<8).map{byte & (0x80 >> $0) != 0}}
    }
    static func decode(_ bits:[Bool],tag:UInt32)->UInt64? {
        guard bits.count==128 else{return nil}
        let bytes=(0..<16).map{index in bits[(index*8)..<(index*8+8)].reduce(UInt8(0)){($0<<1)|($1 ? 1:0)}}
        let actual=bytes.prefix(4).reduce(UInt32(0)){($0<<8)|UInt32($1)}
        let sum=bytes.suffix(4).reduce(UInt32(0)){($0<<8)|UInt32($1)}
        guard actual==tag,checksum(Array(bytes.prefix(12)))==sum else{return nil}
        return bytes[4..<12].reduce(UInt64(0)){($0<<8)|UInt64($1)}
    }
}
