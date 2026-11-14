import Foundation

/// CPU-side row cache shared by the native Metal renderer and bitmap fallback.
/// It keeps original levels; palette/scale changes only invalidate display pixels.
public struct PlatformWaterfallTexture: Sendable {
    public struct Upload: Equatable, Sendable { public let row: Int; public let rgba: [UInt8] }
    public struct Update: Equatable, Sendable { public let reset: Bool; public let uploads: [Upload] }
    public private(set) var width = 0
    public private(set) var height = 0
    public private(set) var visibleRows = 0
    public private(set) var newestRow = 0
    private var grid: [Int64] = []
    private var rows: [[Double]] = []
    private var pixels: [[UInt8]] = []
    private var scale: PlatformSpectrumScale?
    public init() {}

    public mutating func update(_ history: WaterfallHistory, scale: PlatformSpectrumScale) throws -> Update {
        guard history.grid.count <= 16_384, history.rows.count <= 240,
              history.rows.allSatisfy({ $0.count == history.grid.count && $0.allSatisfy(\.isFinite) }) else {
            throw PlatformError.invalidResponse("Водопад превышает экранный бюджет или содержит неверную сетку")
        }
        guard !history.rows.isEmpty, !history.grid.isEmpty else {
            let changed = !rows.isEmpty; self = Self(); return Update(reset: changed, uploads: [])
        }
        let targetHeight = min(240, history.capacity)
        let reset = grid != history.grid || self.scale != scale || height != targetHeight
        var overlap = 0
        if !reset {
            for count in stride(from: min(rows.count, history.rows.count), through: 1, by: -1) {
                if rows.suffix(count).elementsEqual(history.rows.prefix(count)) { overlap = count; break }
            }
        }
        if reset || overlap == 0 {
            width = history.grid.count; height = targetHeight; grid = history.grid
            pixels = Array(repeating: [], count: height); newestRow = height - 1
        }
        var uploads: [Upload] = []
        for row in history.rows.dropFirst(overlap) {
            try Task.checkCancellation()
            newestRow = (newestRow + 1) % height
            var rgba: [UInt8] = []; rgba.reserveCapacity(width * 4)
            for level in row {
                let rgb = WaterfallPalette.color(scale.fraction(level))
                rgba += [UInt8((rgb.red * 255).rounded()), UInt8((rgb.green * 255).rounded()), UInt8((rgb.blue * 255).rounded()), 255]
            }
            pixels[newestRow] = rgba; uploads.append(Upload(row: newestRow, rgba: rgba))
        }
        rows = history.rows; visibleRows = rows.count; self.scale = scale
        return Update(reset: reset || overlap == 0, uploads: uploads)
    }

    public func completeUploads() -> [Upload] { pixels.enumerated().filter { !$0.element.isEmpty }.map { Upload(row: $0.offset, rgba: $0.element) } }

    /// RGBA with newest row first, matching the established native bitmap orientation.
    public func orderedRGBA() -> [UInt8] {
        guard height > 0 else { return [] }
        return (0..<visibleRows).flatMap { pixels[(newestRow + height - $0) % height] }
    }
}
