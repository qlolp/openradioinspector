import Foundation
import ORIContracts

private func hasFieldText(_ value: String) -> Bool {
    !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
}

/// Preserve sub-microsecond precision used by Android's Instant and compare
/// offsets using the common timestamp parser. Trailing fractional zeros are equal.
struct FieldInstant: Comparable {
    let timestamp: PlatformTimestamp
    let remainder: String
    init(_ value: String) throws {
        let bytes = Array(value.utf8)
        if bytes.count > 26, bytes[19] == 46 {
            var end = 20
            while end < bytes.count, (48...57).contains(bytes[end]) { end += 1 }
            if end > 26 {
                let extra = bytes[26..<end]
                let last = extra.lastIndex(where: { $0 != 48 })
                remainder = last.map { String(decoding: bytes[26...$0], as: UTF8.self) } ?? ""
                timestamp = try PlatformTimestamp(String(decoding: bytes[..<26] + bytes[end...], as: UTF8.self))
                return
            }
        }
        timestamp = try PlatformTimestamp(value)
        remainder = ""
    }
    static func < (a: Self, b: Self) -> Bool {
        a.timestamp == b.timestamp ? a.remainder < b.remainder : a.timestamp < b.timestamp
    }
}

public struct FieldSourceDescriptor: Decodable, Sendable {
    public let id, device, profile: String
    let settings: [String: PlatformComparisonSetting]
}

private struct FieldManifest: Decodable {
    struct Entry: Decodable { let bytes, records: Int; let sha256: String }
    let format: String, format_version: Int, session_id: String, stream_ids: [String]
    let streams: [FieldSourceDescriptor]
    let sweep_count, observation_count: Int
    let files: [String: Entry]
}

private struct FieldHeader: Decodable { let format: String, format_version: Int, session_id: String }

public enum FieldRecordKind: String, Decodable, Sendable {
    case sweep, observation, gps, note, gap
}

public struct FieldRecordKey: Hashable, Sendable {
    public let kind: FieldRecordKind
    public let stream, identifier: PlatformExactID
    init(_ kind: FieldRecordKind, _ stream: String, _ id: String) {
        self.kind = kind
        self.stream = PlatformExactID(stream)
        self.identifier = PlatformExactID(id)
    }
}

public struct FieldRecordRow: Identifiable, Sendable {
    public let id: FieldRecordKey
    public let timeUTC, summary: String
    public let longitude, latitude: Double?
    public let link: FieldRecordKey?
    fileprivate let offset, size: Int
    fileprivate let hash: Data
}

private struct FieldEvent: Decodable {
    let kind: FieldRecordKind, id, session_id, stream_id, time_utc: String
    let latitude, longitude, accuracy_m, altitude_m: Double?
    let sweep_id, observation_id, reason, text: String?
}

private struct FieldMeasurement: Decodable {
    let kind: FieldRecordKind
    let sweep: PlatformSweep?
    let observation: PlatformObservation?
    let fix: String?
    enum Keys: String, CodingKey { case kind, measurement, settings, metadata, fieldFixId }
    init(from decoder: Decoder) throws {
        let row = try decoder.container(keyedBy: Keys.self)
        kind = try row.decode(FieldRecordKind.self, forKey: .kind)
        let details = try row.nestedContainer(keyedBy: Keys.self, forKey: .measurement)
        let locationKey: Keys
        switch kind {
        case .sweep:
            sweep = try row.decode(PlatformSweep.self, forKey: .measurement)
            observation = nil
            locationKey = .settings
        case .observation:
            observation = try row.decode(PlatformObservation.self, forKey: .measurement)
            sweep = nil
            locationKey = .metadata
        default: throw FieldReadError.invalid("неизвестный тип измерения")
        }
        if details.contains(locationKey), try !details.decodeNil(forKey: locationKey) {
            let provenance = try details.nestedContainer(keyedBy: Keys.self, forKey: locationKey)
            fix = try provenance.decodeIfPresent(String.self, forKey: .fieldFixId)
        } else { fix = nil }
    }
}

public enum FieldRecordDetail: Sendable {
    case sweep(PlatformSweep, String)
    case observation(PlatformObservation, String)
    case event(String)
}

/// An immutable index of verified source offsets. Raw sample arrays live only
/// while verifying one line or displaying the selected original measurement.
public struct PlatformFieldDocument: Sendable {
    public let sessionID: String
    public let sources: [FieldSourceDescriptor]
    public let rows: [FieldRecordRow]
    public let sweepCount, observationCount: Int
    private let url: URL
    private let indices: [FieldRecordKey: Int]
    private let reverseLinks: [FieldRecordKey: [Int]]

    public func row(_ key: FieldRecordKey) -> FieldRecordRow? { indices[key].map { rows[$0] } }

    public func linkedRows(_ key: FieldRecordKey) -> [FieldRecordRow] {
        (reverseLinks[key] ?? []).map { rows[$0] }
    }

    private static func measurementDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    public func detail(for key: FieldRecordKey) throws -> FieldRecordDetail {
        guard let row = row(key) else { throw FieldReadError.invalid("исходная запись отсутствует") }
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        try handle.seek(toOffset: UInt64(row.offset))
        let bytes = try handle.fieldRead(row.size)
        guard FieldDigest.hash(bytes) == row.hash, let raw = String(data: bytes, encoding: .utf8) else {
            throw FieldReadError.invalid("файл изменён после открытия")
        }
        if key.kind == .sweep || key.kind == .observation {
            let decoded = try Self.measurementDecoder().decode(FieldMeasurement.self, from: bytes)
            if let sweep = decoded.sweep { return .sweep(sweep, raw) }
            if let observation = decoded.observation { return .observation(observation, raw) }
        }
        return .event(raw)
    }

    public init(opening url: URL) throws {
        let zip = try FieldStoredZIP(url: url)
        defer { try? zip.file.close() }
        let decoder = JSONDecoder()
        let manifestBytes = try zip.manifest()
        _ = try StrictJSON.parse(manifestBytes)
        let manifest = try decoder.decode(FieldManifest.self, from: manifestBytes)
        let streams = Set(manifest.stream_ids.map { PlatformExactID($0) })
        guard manifest.format == "openradioinspector-field", manifest.format_version == 1,
              hasFieldText(manifest.session_id), !streams.isEmpty, streams.count <= 256,
              streams.count == manifest.stream_ids.count, manifest.stream_ids.allSatisfy(hasFieldText),
              manifest.streams.count == streams.count,
              Set(manifest.streams.map { PlatformExactID($0.id) }) == streams,
              manifest.streams.allSatisfy({ hasFieldText($0.device) && hasFieldText($0.profile) }),
              Set(manifest.files.keys) == Set(FieldStoredZIP.names.dropLast()) else {
            throw FieldReadError.invalid("неподдерживаемый манифест")
        }
        let measurementDecoder = Self.measurementDecoder()
        var rows: [FieldRecordRow] = [], index: [FieldRecordKey: Int] = [:], reverse: [FieldRecordKey: [Int]] = [:]
        var gpsFeatures: [Data] = []
        var sweepCount = 0, observationCount = 0
        func append(_ key: FieldRecordKey, session: String, time: String, summary: String,
                    location: (Double?, Double?) = (nil, nil), link: FieldRecordKey?, line: Data, offset: Int) throws {
            guard PlatformExactID.same(session, manifest.session_id), streams.contains(key.stream),
                  hasFieldText(key.identifier.rawValue),
                  index.updateValue(rows.count, forKey: key) == nil else {
                throw FieldReadError.invalid("чужой поток, неверное время или повторный ID")
            }
            if let link { reverse[link, default: []].append(rows.count) }
            rows.append(FieldRecordRow(id: key, timeUTC: time, summary: summary, longitude: location.0,
                latitude: location.1, link: link, offset: offset, size: line.count, hash: FieldDigest.hash(line)))
        }

        let measurements = manifest.files["measurements.ndjson"]!
        var hasHeader = false
        try zip.lines("measurements.ndjson", bytes: measurements.bytes, sha: measurements.sha256) { line, offset in
            if !hasHeader {
                _ = try StrictJSON.parse(line)
                let header = try decoder.decode(FieldHeader.self, from: line)
                guard header.format == "openradioinspector-archive-ndjson", header.format_version == 1,
                      PlatformExactID.same(header.session_id, manifest.session_id) else { throw FieldReadError.invalid("заголовок измерений") }
                hasHeader = true
                return
            }
            _ = try StrictJSON.parse(line, maxBytes: 16 * 1024 * 1024 + 1024)
            let record = try measurementDecoder.decode(FieldMeasurement.self, from: line)
            let id: String, session: String, stream: String, interval: PlatformInterval, summary: String
            if let sweep = record.sweep {
                guard hasFieldText(sweep.levelUnit) else { throw FieldReadError.invalid("единица измерения") }
                (id, session, stream, interval) = (sweep.id, sweep.sessionId, sweep.streamId, sweep.interval)
                summary = "\(sweep.frequenciesHz.first!)–\(sweep.frequenciesHz.last!) Гц · \(sweep.levels.count) точек · \(sweep.levelUnit)"
                sweepCount += 1
            } else if let observation = record.observation {
                guard observation.frequencyHz > 0, hasFieldText(observation.mode), hasFieldText(observation.levelUnit) else { throw FieldReadError.invalid("частота наблюдения") }
                (id, session, stream, interval) = (observation.id, observation.sessionId, observation.streamId, observation.interval)
                summary = "\(observation.frequencyHz) Гц · \(observation.mode) · \(observation.level.map { String($0) } ?? "—") \(observation.levelUnit)"
                observationCount += 1
            } else { throw FieldReadError.invalid("измерение отсутствует") }
            guard try FieldInstant(interval.startedAt) <= FieldInstant(interval.endedAt) else { throw FieldReadError.invalid("интервал измерения") }
            try append(.init(record.kind, stream, id), session: session, time: interval.startedAt, summary: summary,
                       link: record.fix.map { .init(.gps, stream, $0) }, line: line, offset: offset)
        }
        guard hasHeader, rows.count == measurements.records, sweepCount == manifest.sweep_count,
              observationCount == manifest.observation_count else { throw FieldReadError.invalid("число измерений") }

        let events = manifest.files["events.ndjson"]!
        try zip.lines("events.ndjson", bytes: events.bytes, sha: events.sha256) { line, offset in
            _ = try StrictJSON.parse(line, maxBytes: 16 * 1024 * 1024 + 1024)
            let event = try decoder.decode(FieldEvent.self, from: line)
            _ = try FieldInstant(event.time_utc)
            let summary: String
            switch event.kind {
            case .gps:
                guard let lon = event.longitude, let lat = event.latitude, (-180...180).contains(lon),
                      (-90...90).contains(lat), event.accuracy_m.map({ $0 >= 0 }) ?? true,
                      var properties = try JSONSerialization.jsonObject(with: line) as? [String: Any] else {
                    throw FieldReadError.invalid("координаты GPS")
                }
                var coordinates = [lon, lat]
                if let altitude = event.altitude_m { coordinates.append(altitude) }
                for key in ["kind", "id", "longitude", "latitude", "altitude_m"] { properties.removeValue(forKey: key) }
                let feature: [String: Any] = ["type": "Feature", "id": event.id, "properties": properties,
                    "geometry": ["type": "Point", "coordinates": coordinates]]
                gpsFeatures.append(try Self.featureHash(feature))
                summary = "\(lat), \(lon)"
            case .note:
                guard let text = event.text, hasFieldText(text) else { throw FieldReadError.invalid("текст заметки") }
                summary = String(text.prefix(160))
            case .gap:
                guard let reason = event.reason, hasFieldText(reason) else { throw FieldReadError.invalid("причина разрыва") }
                summary = String(reason.prefix(160))
            default: throw FieldReadError.invalid("неизвестный тип события")
            }
            guard event.observation_id == nil || event.sweep_id == nil else { throw FieldReadError.invalid("двойная ссылка события") }
            let link = event.sweep_id.map { FieldRecordKey(.sweep, event.stream_id, $0) }
                ?? event.observation_id.map { FieldRecordKey(.observation, event.stream_id, $0) }
            try append(.init(event.kind, event.stream_id, event.id), session: event.session_id, time: event.time_utc,
                       summary: summary, location: (event.longitude, event.latitude), link: link, line: line, offset: offset)
        }
        guard rows.count - measurements.records == events.records, reverse.keys.allSatisfy({ index[$0] != nil }) else {
            throw FieldReadError.invalid("число событий или ссылка на отсутствующий источник")
        }

        let track = manifest.files["track.geojson"]!
        var lineNumber = 0, trackEnded = false, features = 0
        try zip.lines("track.geojson", bytes: track.bytes, sha: track.sha256) { line, _ in
            defer { lineNumber += 1 }
            if lineNumber == 0 {
                guard line == Data(#"{"type":"FeatureCollection","features":["#.utf8) else { throw FieldReadError.invalid("заголовок трека") }
            } else if !trackEnded, line == Data("]}".utf8) {
                trackEnded = true
            } else {
                guard !trackEnded, features < gpsFeatures.count, features == 0 || line.first == 44 else { throw FieldReadError.invalid("структура трека") }
                let data = features == 0 ? line : Data(line.dropFirst())
                _ = try StrictJSON.parse(data, maxBytes: 16 * 1024 * 1024 + 1024)
                let feature = try JSONSerialization.jsonObject(with: data)
                guard try Self.featureHash(feature) == gpsFeatures[features] else { throw FieldReadError.invalid("GPS и трек не совпадают") }
                features += 1
            }
        }
        guard trackEnded, features == gpsFeatures.count, features == track.records else { throw FieldReadError.invalid("число точек трека") }
        self.url = url
        self.rows = rows
        self.indices = index
        self.reverseLinks = reverse
        self.sessionID = manifest.session_id
        self.sources = manifest.streams
        self.sweepCount = sweepCount
        self.observationCount = observationCount
    }

    private static func featureHash(_ object: Any) throws -> Data {
        guard JSONSerialization.isValidJSONObject(object) else { throw FieldReadError.invalid("геометрия трека") }
        return FieldDigest.hash(try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys]))
    }
}
