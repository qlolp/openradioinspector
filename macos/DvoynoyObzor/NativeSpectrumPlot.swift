import SwiftUI
import CoreGraphics
import SpektrKomandy

private struct NativeWaterfallRasterRow: @unchecked Sendable {
    let id: String
    let image: CGImage
}

struct NativeSpectrumPlot: View {
    let sweeps: [PlatformSweep]
    let selected: PlatformSweep?
    let bounds: PlatformSpectrumScale?
    let range: ClosedRange<Double>
    let threshold: Double?
    let cursor: NativeSpectrumSample?
    let markers: [NativeSpectrumSample]
    let layout: NativeSpectrumPlotLayout
    @State private var rasters: [NativeWaterfallRasterRow] = []
    private struct Request: Equatable {
        let sweeps: [PlatformSweep]
        let bounds: PlatformSpectrumScale?
        let range: ClosedRange<Double>
    }

    var body: some View {
        let timeline = NativeSpectrumTimeline(sweeps)
        Canvas { context, _ in
            let plot = layout.spectrum, water = layout.waterfall
            func x(_ hz: Double) -> Double { plot.minX + (hz - range.lowerBound) / (range.upperBound - range.lowerBound) * plot.width }
            func label(_ text: String, _ point: CGPoint, color: Color = .secondary, anchor: UnitPoint = .center) {
                context.draw(Text(text).font(.system(size: 10, design: .monospaced)).foregroundStyle(color), at: point, anchor: anchor)
            }
            func line(_ a: CGPoint, _ b: CGPoint, color: Color, dashed: Bool = false) {
                var path = Path(); path.move(to: a); path.addLine(to: b)
                context.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: 1, dash: dashed ? [4, 3] : []))
            }
            context.fill(Path(plot), with: .color(.white.opacity(0.035)))
            context.fill(Path(water), with: .color(.black))
            for i in 0...4 {
                let f = Double(i) / 4
                let px = plot.minX + f * plot.width
                line(CGPoint(x: px, y: plot.minY), CGPoint(x: px, y: plot.maxY), color: .white.opacity(0.14))
                let frequency = range.lowerBound + f * (range.upperBound - range.lowerBound)
                label(String(format: "%.3f", frequency / 1_000_000), CGPoint(x: px, y: plot.maxY + 12), anchor: i == 4 ? .trailing : .center)
                if let bounds {
                    let py = plot.maxY - f * plot.height
                    line(CGPoint(x: plot.minX, y: py), CGPoint(x: plot.maxX, y: py), color: .white.opacity(0.14))
                    label(String(format: "%.1f", bounds.lower + f * (bounds.upper - bounds.lower)), CGPoint(x: plot.minX - 7, y: py), anchor: .trailing)
                }
            }
            label("дБм", CGPoint(x: 8, y: 8), anchor: .leading)
            label("МГц", CGPoint(x: 8, y: plot.maxY + 12), anchor: .leading)
            label("UTC", CGPoint(x: 8, y: water.minY - 8), anchor: .leading)
            if let bounds, let selected {
                func y(_ level: Double) -> Double { plot.maxY - bounds.fraction(level) * plot.height }
                var clipped = context; clipped.clip(to: Path(plot))
                func trace(_ values: [Double], color: Color, width: Double) {
                    var path = Path()
                    for i in values.indices {
                        let point = CGPoint(x: x(Double(selected.frequenciesHz[i])), y: y(values[i]))
                        if i == 0 { path.move(to: point) } else { path.addLine(to: point) }
                    }
                    clipped.stroke(path, with: .color(color), lineWidth: width)
                }
                // Envelopes cover exactly the displayed history, never an unrelated grid.
                let sameGrid = sweeps.filter { $0.frequenciesHz == selected.frequenciesHz }
                if !sameGrid.isEmpty {
                    trace(selected.levels.indices.map { i in sameGrid.map { $0.levels[i] }.min() ?? selected.levels[i] }, color: .cyan.opacity(0.6), width: 1)
                    trace(selected.levels.indices.map { i in sameGrid.map { $0.levels[i] }.max() ?? selected.levels[i] }, color: .orange.opacity(0.7), width: 1)
                }
                trace(selected.levels, color: .white, width: 1.5)
                if let threshold, threshold >= bounds.lower, threshold <= bounds.upper {
                    line(CGPoint(x: plot.minX, y: y(threshold)), CGPoint(x: plot.maxX, y: y(threshold)), color: .red, dashed: true)
                }
                let labelCount = max(1, min(3, Int(plot.width / 240)))
                let peaks = NativeSpectrumInspection.peaks(in: selected, range: range, limit: labelCount)
                for (number, bin) in peaks.enumerated() {
                    let point = CGPoint(x: x(Double(selected.frequenciesHz[bin])), y: y(selected.levels[bin]))
                    let caption = CGPoint(x: plot.minX + plot.width * (Double(number) + 0.5) / Double(labelCount), y: 7)
                    line(CGPoint(x: caption.x, y: 14), point, color: .yellow.opacity(0.5))
                    context.fill(Path(ellipseIn: CGRect(x: point.x - 3, y: point.y - 3, width: 6, height: 6)), with: .color(.yellow))
                    label("P\(number + 1) \(String(format: "%.3f", Double(selected.frequenciesHz[bin]) / 1_000_000)) МГц · \(String(format: "%.1f", selected.levels[bin])) дБм", caption, color: .yellow)
                }
            }
            for row in timeline.rows {
                let top = water.minY + timeline.fraction(row.end) * water.height
                let bottom = water.minY + timeline.fraction(row.start) * water.height
                let rect = CGRect(x: water.minX, y: top, width: water.width, height: max(1, bottom - top))
                if let raster = rasters.first(where: { $0.id == row.sweep.id }) {
                    var imageContext = context; imageContext.clip(to: Path(water))
                    imageContext.draw(Image(decorative: raster.image, scale: 1), in: rect)
                }
                if selected?.id == row.sweep.id {
                    context.stroke(Path(rect), with: .color(.yellow.opacity(0.85)), lineWidth: 1)
                }
            }
            if !timeline.rows.isEmpty {
                for i in 0...2 {
                    let f = Double(i) / 2
                    let time = timeline.end.addingTimeInterval(-f * timeline.duration)
                    label(String(NativeSpectrumTimeline.utc(time).prefix(8)), CGPoint(x: water.minX - 7, y: water.minY + f * water.height), anchor: .trailing)
                }
            }
            for (index, marker) in markers.enumerated() where range.contains(Double(marker.frequency)) {
                let px = x(Double(marker.frequency))
                line(CGPoint(x: px, y: plot.minY), CGPoint(x: px, y: water.maxY), color: .yellow, dashed: true)
                label("M\(index + 1)", CGPoint(x: px + 3, y: water.minY + 8), color: .yellow, anchor: .leading)
            }
            if let cursor, range.contains(Double(cursor.frequency)) {
                let px = x(Double(cursor.frequency))
                line(CGPoint(x: px, y: plot.minY), CGPoint(x: px, y: water.maxY), color: .white.opacity(0.9))
                if let row = timeline.rows.first(where: { $0.sweep.id == cursor.sweep.id }) {
                    let py = water.minY + timeline.fraction(row.end.addingTimeInterval(-row.end.timeIntervalSince(row.start) / 2)) * water.height
                    line(CGPoint(x: water.minX, y: py), CGPoint(x: water.maxX, y: py), color: .white.opacity(0.9))
                }
            }
            if sweeps.isEmpty { label("Нет измерений — выберите сеанс и начните приём", CGPoint(x: plot.midX, y: plot.midY)) }
        }
        .task(id: Request(sweeps: sweeps, bounds: bounds, range: range)) {
            let source = sweeps, scale = bounds, visible = range
            let task = Task.detached(priority: .userInitiated) { Self.makeRasters(source, bounds: scale, range: visible) }
            let images = await withTaskCancellationHandler(operation: { await task.value }, onCancel: { task.cancel() })
            guard !Task.isCancelled else { return }
            rasters = images
        }
    }

    nonisolated private static func makeRasters(_ sweeps: [PlatformSweep], bounds: PlatformSpectrumScale?, range: ClosedRange<Double>) -> [NativeWaterfallRasterRow] {
        guard let bounds else { return [] }
        return sweeps.compactMap { sweep in
            guard !Task.isCancelled,
                  let space = CGColorSpace(name: CGColorSpace.sRGB),
                  let context = CGContext(data: nil, width: 1024, height: 1, bitsPerComponent: 8, bytesPerRow: 4096,
                    space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
            context.setAllowsAntialiasing(false)
            for i in sweep.levels.indices {
                let frequencies = sweep.frequenciesHz
                let f = Double(frequencies[i])
                let low = i == 0 ? f : (Double(frequencies[i - 1]) + f) / 2
                let high = i == frequencies.count - 1 ? f : (Double(frequencies[i + 1]) + f) / 2
                let x = (low - range.lowerBound) / (range.upperBound - range.lowerBound) * 1024
                let width = (high - low) / (range.upperBound - range.lowerBound) * 1024
                let color = WaterfallPalette.color(bounds.fraction(sweep.levels[i]))
                context.setFillColor(red: color.red, green: color.green, blue: color.blue, alpha: 1)
                context.fill(CGRect(x: x, y: 0, width: max(1, width), height: 1))
            }
            return context.makeImage().map { NativeWaterfallRasterRow(id: sweep.id, image: $0) }
        }
    }
}
