import CFieldDigest
import Foundation

enum FieldReadError: LocalizedError {
    case invalid(String)
    var errorDescription: String? {
        switch self { case .invalid(let detail): return "Полевой архив: " + detail }
    }
}

struct FieldDigest {
    private var state = FieldSHA256()
    private(set) var crc: UInt32 = 0
    init() { field_digest_init(&state) }
    mutating func update(_ data: Data) {
        data.withUnsafeBytes { raw in
            guard raw.count > 0 else { return }
            let count = UInt32(raw.count)
            field_digest_update(&state, raw.baseAddress, count)
            crc = field_crc(crc, raw.baseAddress, count)
        }
    }
    mutating func finish() -> Data {
        var bytes = [UInt8](repeating: 0, count: 32)
        field_digest_final(&state, &bytes)
        return Data(bytes)
    }
    static func hash(_ data: Data) -> Data {
        var digest = Self()
        digest.update(data)
        return digest.finish()
    }
}

private extension Data {
    func u16(_ i: Int) -> Int { Int(self[startIndex + i]) | (Int(self[startIndex + i + 1]) << 8) }
    func u32(_ i: Int) -> Int { u16(i) | (u16(i + 2) << 16) }
}

extension FileHandle {
    func fieldRead(_ count: Int) throws -> Data {
        var result = Data()
        while result.count < count {
            guard let bytes = try read(upToCount: count - result.count), !bytes.isEmpty else {
                throw FieldReadError.invalid("неожиданный конец файла")
            }
            result.append(bytes)
        }
        return result
    }
}

struct FieldStoredZIP {
    static let names = ["measurements.ndjson", "events.ndjson", "track.geojson", "manifest.json"]
    struct Member { let offset, size: Int; let crc: UInt32 }
    let file: FileHandle
    let members: [String: Member]

    init(url: URL) throws {
        guard url.isFileURL, try url.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw FieldReadError.invalid("выберите обычный файл")
        }
        file = try FileHandle(forReadingFrom: url)
        let length = try file.seekToEnd()
        let invalid = FieldReadError.invalid("нарушена структура ZIP")
        guard (22...0xfffffffe).contains(length) else { throw invalid }
        try file.seek(toOffset: length - 22)
        let end = try file.fieldRead(22)
        let directory = end.u32(16)
        guard end.u32(0) == 0x06054b50, end.u32(4) == 0, end.u16(8) == 4, end.u16(10) == 4,
              end.u16(20) == 0, end.u32(12) <= 262144, directory + end.u32(12) == Int(length) - 22 else { throw invalid }
        var position = directory
        var found: [String: Member] = [:]
        var ranges: [Range<Int>] = []
        for entry in 0..<4 {
            try file.seek(toOffset: UInt64(position))
            let central = try file.fieldRead(46)
            let flags = central.u16(8), nameLength = central.u16(28), size = central.u32(24)
            let crc = UInt32(central.u32(16))
            let fileType = (central.u32(38) >> 16) & 0o170000
            guard central.u32(0) == 0x02014b50, flags & ~0x800 == 0, central.u16(10) == 0,
                  size == central.u32(20), central.u16(34) == 0, (1...32).contains(nameLength),
                  fileType == 0 || fileType == 0o100000, central.u32(38) & 0x10 == 0 else { throw invalid }
            let rawName = try file.fieldRead(nameLength)
            guard let name = String(data: rawName, encoding: .ascii), Self.names.contains(name), found[name] == nil, (entry == 3) == (name == "manifest.json") else { throw invalid }
            position += 46 + nameLength + central.u16(30) + central.u16(32)
            let start = central.u32(42)
            try file.seek(toOffset: UInt64(start))
            let local = try file.fieldRead(30)
            guard local.u32(0) == 0x04034b50, local.u16(6) == flags, local.u16(8) == 0,
                  local.u32(14) == Int(crc), local.u32(18) == size, local.u32(22) == size,
                  local.u16(26) == nameLength, try file.fieldRead(nameLength) == rawName else { throw invalid }
            let dataStart = start + 30 + nameLength + local.u16(28)
            guard dataStart + size <= directory else { throw invalid }
            ranges.append(start..<(dataStart + size))
            found[name] = Member(offset: dataStart, size: size, crc: crc)
        }
        let ordered = ranges.sorted { $0.lowerBound < $1.lowerBound }
        guard position == Int(length) - 22, ordered.first?.lowerBound == 0, ordered.last?.upperBound == directory,
              zip(ordered, ordered.dropFirst()).allSatisfy({ $0.upperBound == $1.lowerBound }),
              let manifest = found["manifest.json"], manifest.size <= 262144,
              manifest.offset + manifest.size == directory else { throw invalid }
        members = found
    }

    func manifest() throws -> Data {
        let member = members["manifest.json"]!
        try file.seek(toOffset: UInt64(member.offset))
        let data = try file.fieldRead(member.size)
        var digest = FieldDigest()
        digest.update(data)
        guard digest.crc == member.crc else { throw FieldReadError.invalid("CRC манифеста не совпадает") }
        return data
    }

    /// Delivers one bounded line at a time, along with its absolute source offset.
    /// Hashes the same bytes passed to the parser, including all line terminators.
    func lines(_ name: String, bytes: Int, sha: String, body: (Data, Int) throws -> Void) throws {
        let member = members[name]!
        guard bytes == member.size else { throw FieldReadError.invalid("размер " + name) }
        let maxLine = 16 * 1024 * 1024 + 1024
        try file.seek(toOffset: UInt64(member.offset))
        var remaining = bytes, offset = member.offset
        var line = Data(), digest = FieldDigest()
        while remaining > 0 {
            try Task.checkCancellation()
            let chunk = try file.fieldRead(min(remaining, 128 * 1024))
            remaining -= chunk.count
            digest.update(chunk)
            var start = chunk.startIndex
            while start < chunk.endIndex {
                let newline = chunk[start...].firstIndex(of: 10)
                let end = newline ?? chunk.endIndex
                guard line.count + end - start < maxLine else { throw FieldReadError.invalid("слишком длинная запись") }
                line.append(chunk[start..<end])
                if newline != nil {
                    try body(line, offset)
                    offset += line.count + 1
                    line.removeAll(keepingCapacity: true)
                }
                start = end + 1
            }
        }
        guard line.isEmpty else { throw FieldReadError.invalid("строка без LF") }
        let hex = digest.finish().map { String(format: "%02x", $0) }.joined()
        guard digest.crc == member.crc, hex == sha else { throw FieldReadError.invalid("контрольная сумма " + name) }
    }
}
