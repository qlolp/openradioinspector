// Canonical standalone codec for contracts/protobuf/measurement_stream.proto.
// No socket, archive, authentication or reconnect ownership lives here.
import Foundation
import ORIContracts

public enum MeasurementStream {
    public static let protocolVersion: UInt32 = 1
    public static let maxFrameBytes = 16 * 1024 * 1024 + 64 * 1024
    public static let maxPoints = 1_000_000
    public static let path = "/api/v1/events/ws"
    public static let subprotocolName = "ori.measurements.v1.protobuf"

    public enum StreamError: Error, Equatable, Sendable {
        case invalid(String), frameTooLarge, pointLimit
    }
    public struct Limits: Sendable {
        public let maxFrameBytes: Int
        public let maxPoints: Int
        public init(maxFrameBytes: Int = MeasurementStream.maxFrameBytes, maxPoints: Int = MeasurementStream.maxPoints) {
            self.maxFrameBytes=maxFrameBytes; self.maxPoints=maxPoints
        }
        fileprivate func validate() throws {
            guard maxFrameBytes>0, maxFrameBytes<=MeasurementStream.maxFrameBytes,
                  maxPoints>=0, maxPoints<=MeasurementStream.maxPoints else {
                throw StreamError.invalid("Limits may only lower the protocol caps")
            }
        }
    }
    public struct SpectrumArrays: Sendable {
        public let frequenciesHz: [UInt64]
        public let levels: [Double]
    }
    public struct DecodedFrame: Sendable {
        public let protocolVersion: UInt32
        public let sequence: UInt64
        public let envelopeJSON: Data
        public let envelope: JSONValue
        public let spectrum: SpectrumArrays?

        /// Direct structured insertion, with no JSON serialization of arrays.
        public func intoEnvelope() throws -> JSONValue {
            guard let spectrum else {return envelope}
            var root=try envelope.requireObject()
            var data=try required(root,"data").requireObject()
            var payload=try required(data,"payload").requireObject()
            var sweep=try required(payload,"sweep").requireObject()
            sweep["frequencies_hz"] = .array(spectrum.frequenciesHz.map {JSONValue.integer(String($0))})
            sweep["levels"] = .array(spectrum.levels.map(JSONValue.number))
            payload["sweep"] = .object(sweep);data["payload"] = .object(payload);root["data"] = .object(data)
            return .object(root)
        }
    }

    /// Decode into opaque metadata and independent numeric arrays. Input Data
    /// is not retained; all returned buffers are owned values safe to transfer.
    public static func decode(_ bytes: Data, limits: Limits = Limits()) throws -> DecodedFrame {
        try limits.validate()
        guard bytes.count<=limits.maxFrameBytes else {throw StreamError.frameTooLarge}
        let source=Array(bytes)
        var reader=Reader(source[...]), seen=Set<Int>()
        var version:UInt64?,sequence:UInt64?,json:ArraySlice<UInt8>?,packed:ArraySlice<UInt8>?
        while !reader.done {
            let (field,wire)=try reader.tag()
            if field<=4 {
                guard seen.insert(field).inserted else {throw StreamError.invalid("Duplicate unique field")}
                guard (field<=2 && wire==0)||(field>=3 && wire==2) else {throw StreamError.invalid("Header field wire type")}
                switch field {
                case 1:version=try reader.varint()
                case 2:sequence=try reader.varint()
                case 3:json=try reader.blob()
                default:packed=try reader.blob()
                }
            } else {try reader.skip(wire)}
        }
        guard version==UInt64(protocolVersion),let sequence,sequence>0,let json,!json.isEmpty else {
            throw StreamError.invalid("Missing or unsupported header")
        }
        let arrays=try packed.map {try spectrum($0,maxPoints:limits.maxPoints)}
        let envelopeJSON=Data(json)
        let envelope=try StrictJSON.parse(envelopeJSON,maxBytes:limits.maxFrameBytes)
        try validate(envelope,sequence:sequence,typed:arrays != nil,maxPoints:limits.maxPoints)
        return DecodedFrame(protocolVersion:protocolVersion,sequence:sequence,envelopeJSON:envelopeJSON,envelope:envelope,spectrum:arrays)
    }

    private static func required(_ object:JSONObject,_ key:String) throws -> JSONValue {
        guard let value=object[key] else {throw StreamError.invalid("Missing required field")};return value
    }
    private static func text(_ object:JSONObject,_ key:String) throws -> String {try required(object,key).requireString()}
    private static func timestamp(_ object:JSONObject,_ key:String) throws {_ = try UtcTimestamp(text(object,key))}
    private static func validate(_ envelope:JSONValue,sequence:UInt64,typed:Bool,maxPoints:Int) throws {
        let root=try envelope.requireObject()
        guard try text(root,"contract")=="openradioinspector-platform",try required(root,"contract_version").requireUInt64()==1 else {
            throw StreamError.invalid("Unsupported contract")
        }
        let data=try required(root,"data").requireObject()
        guard try required(data,"sequence").requireUInt64()==sequence else {throw StreamError.invalid("Sequence mismatch")}
        let kind=try text(data,"event_type");try timestamp(data,"occurred_at")
        if let session=data["session_id"],session != .null {_ = try session.requireString()}
        let payload=try required(data,"payload").requireObject()
        guard !typed || kind=="sweep.received" else {throw StreamError.invalid("Spectrum requires sweep.received")}
        if kind=="sweep.received" {
            let sweep=try required(payload,"sweep").requireObject()
            for key in ["id","session_id","stream_id","level_unit"] {_ = try text(sweep,key)}
            let interval=try required(sweep,"interval").requireObject()
            try timestamp(interval,"started_at");try timestamp(interval,"ended_at")
            if let settings=sweep["settings"],settings != .null {_ = try settings.requireObject()}
            if typed {
                guard sweep["frequencies_hz"]==nil,sweep["levels"]==nil else {throw StreamError.invalid("Ambiguous metadata arrays")}
            } else {
                let frequencies=try required(sweep,"frequencies_hz").requireArray(),levels=try required(sweep,"levels").requireArray()
                guard !frequencies.isEmpty,frequencies.count==levels.count else {throw StreamError.invalid("Empty or unequal arrays")}
                guard frequencies.count<=maxPoints else {throw StreamError.pointLimit}
                for values in [frequencies,levels] {for value in values {
                    switch value {case .integer,.number:break;default:throw StreamError.invalid("Numeric sweep arrays required")}
                }}
            }
        }
    }

    private static func spectrum(_ bytes:ArraySlice<UInt8>,maxPoints:Int) throws -> SpectrumArrays {
        var frequencyCount=0,levelCount=0,previous:UInt64=0
        try walk(bytes,frequency:{value in
            frequencyCount+=1
            guard frequencyCount<=maxPoints else {throw StreamError.pointLimit}
            guard frequencyCount==1 || value>=previous else {throw StreamError.invalid("Unordered frequencies")}
            previous=value
        },level:{value in
            levelCount+=1
            guard levelCount<=maxPoints else {throw StreamError.pointLimit}
            guard value.isFinite else {throw StreamError.invalid("Nonfinite level")}
        })
        guard frequencyCount>0,frequencyCount==levelCount else {throw StreamError.invalid("Empty or unequal arrays")}
        var frequencies=[UInt64](repeating:0,count:frequencyCount),levels=[Double](repeating:0,count:levelCount),fi=0,li=0
        try walk(bytes,frequency:{frequencies[fi]=$0;fi+=1},level:{levels[li]=$0;li+=1})
        return SpectrumArrays(frequenciesHz:frequencies,levels:levels)
    }
    private static func walk(_ bytes:ArraySlice<UInt8>,frequency:(UInt64)throws->Void,level:(Double)throws->Void) throws {
        var reader=Reader(bytes)
        while !reader.done {
            let (field,wire)=try reader.tag()
            switch (field,wire) {
            case (1,0):try frequency(reader.varint())
            case (1,2):
                var packed=try Reader(reader.blob())
                while !packed.done {try frequency(packed.varint())}
            case (2,1):try level(reader.double())
            case (2,2):
                var packed=try Reader(reader.blob())
                guard packed.remaining%8==0 else {throw StreamError.invalid("Packed double length")}
                while !packed.done {try level(packed.double())}
            case (1,_),(2,_):throw StreamError.invalid("Spectrum field wire type")
            default:try reader.skip(wire)
            }
        }
    }
    private struct Reader {
        let bytes:ArraySlice<UInt8>
        var position:Int
        init(_ bytes:ArraySlice<UInt8>) {self.bytes=bytes;position=bytes.startIndex}
        var done:Bool {position==bytes.endIndex}
        var remaining:Int {bytes.endIndex-position}
        mutating func varint() throws -> UInt64 {
            var value:UInt64=0
            for index in 0..<10 {
                guard position<bytes.endIndex else {throw StreamError.invalid("Truncated varint")}
                let byte=bytes[position];position+=1
                guard index != 9 || byte<=1 else {throw StreamError.invalid("Overflowing varint")}
                value |= UInt64(byte&127) << (index*7)
                if byte<128 {return value}
            }
            throw StreamError.invalid("Unterminated varint")
        }
        mutating func tag() throws -> (Int,Int) {
            let key=try varint()
            guard key>=8,key<=UInt64(UInt32.max) else {throw StreamError.invalid("Invalid field tag")}
            return (Int(key>>3),Int(key&7))
        }
        mutating func take(_ count:Int) throws -> ArraySlice<UInt8> {
            guard count<=remaining else {throw StreamError.invalid("Truncated field")}
            let result=bytes[position..<position+count];position+=count;return result
        }
        mutating func blob() throws -> ArraySlice<UInt8> {
            let length=try varint()
            guard length<=UInt64(remaining) else {throw StreamError.invalid("Truncated length-delimited field")}
            return try take(Int(length))
        }
        mutating func double() throws -> Double {
            var bits:UInt64=0
            for (index,byte) in try take(8).enumerated() {bits |= UInt64(byte) << (index*8)}
            return Double(bitPattern:bits)
        }
        mutating func skip(_ wire:Int) throws {
            switch wire {
            case 0:_ = try varint()
            case 1:_ = try take(8)
            case 2:_ = try blob()
            case 5:_ = try take(4)
            default:throw StreamError.invalid("Unsupported protobuf wire type")
            }
        }
    }
}
