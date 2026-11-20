import SwiftUI
import CoreGraphics
import SpektrKomandy

struct NativeSpectrumPlot: View {
    let sweeps: [PlatformSweep]
    let selected: PlatformSweep?
    let bounds: PlatformSpectrumScale?
    let range: ClosedRange<Double>
    let threshold: Double?
    let cursor: NativeSpectrumSample?
    let markers: [NativeSpectrumSample]
    let layout: NativeSpectrumPlotLayout
    let timeline: NativeSpectrumTimeline
    var showMinimum = true
    var showMaximum = true
    var reference: PlatformSweep? = nil
    @State private var prepared = NativeSpectrumRenderCache()
    @State private var preparedRequest: Request?
    private struct Sources: Equatable {
        let ids: [PlatformExactID]
        let selected: PlatformExactID?
        let bounds: PlatformSpectrumScale?
        let threshold: Double?
        let reference: PlatformExactID?
    }
    private struct Request: Equatable {
        let sources:Sources
        let raster:NativeSpectrumRasterWindow?
    }

    var body: some View {
        let acceptance=NativeAcceptanceTrace.shared
        let fullLower=Double(selected?.frequenciesHz.first ?? 0)
        let fullUpper=max(fullLower+1,Double(selected?.frequenciesHz.last ?? 1))
        let selectedID=selected?.exactID
        let raster=NativeSpectrumRasterWindow.covering(range,within:fullLower...fullUpper,previous:preparedRequest?.raster)
        let request=Request(sources:Sources(ids:sweeps.map(\.exactID),selected:selectedID,bounds:bounds,threshold:threshold,reference:reference?.exactID),raster:raster)
        let ready=preparedRequest==request
        let geometryReady=preparedRequest?.sources==request.sources
        // A new marker may certify only geometry prepared from that exact source.
        let evidence=ready && raster != nil ? acceptance?.frame(for:selected?.id,viewport:range):nil
        let markerTag=acceptance?.tag ?? 0
        // Only captured, immutable paths/bitmaps enter the renderer. Composite
        // them asynchronously so drawing does not hold up UI input and delivery.
        Canvas(rendersAsynchronously: true) { context, _ in
            let plot = layout.spectrum, water = layout.waterfall
            func x(_ hz: Double) -> Double { plot.minX + (hz - range.lowerBound) / (range.upperBound - range.lowerBound) * plot.width }
            func label(_ text: String, _ point: CGPoint, color: Color = .white.opacity(0.82), anchor: UnitPoint = .center) {
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
            if geometryReady, let bounds, let selected {
                func y(_ level: Double) -> Double { plot.maxY - bounds.fraction(level) * plot.height }
                var clipped = context; clipped.clip(to: Path(plot))
                let span=range.upperBound-range.lowerBound
                var transform=CGAffineTransform(a:(prepared.upper-prepared.lower)/span*plot.width,b:0,c:0,d:plot.height,
                    tx:x(prepared.lower),ty:plot.maxY)
                func trace(_ path:CGPath?, color: Color, width: Double) {
                    guard let path=path?.copy(using:&transform) else{return}
                    clipped.stroke(Path(path), with: .color(color), lineWidth: width)
                }
                // Envelopes cover exactly the displayed history, never an unrelated grid.
                if showMinimum {trace(prepared.minimum,color:.cyan.opacity(0.6),width:1)}
                if showMaximum {trace(prepared.maximum,color:.orange.opacity(0.7),width:1)}
                trace(prepared.reference,color:.purple,width:1.5)
                for sample in prepared.thresholdPoints {
                    let point=sample.applying(transform)
                    guard plot.contains(point) else{continue}
                    clipped.fill(Path(ellipseIn:CGRect(x:point.x-2,y:point.y-2,width:4,height:4)),with:.color(.red))
                }
                trace(prepared.current, color: .white, width: 1.5)
                if let threshold, threshold >= bounds.lower, threshold <= bounds.upper {
                    line(CGPoint(x: plot.minX, y: y(threshold)), CGPoint(x: plot.maxX, y: y(threshold)), color: .red, dashed: true)
                }
                let labelCount = max(1, min(3, Int(plot.width / 240)))
                var peaks:[Int]=[]
                for i in prepared.peakCandidates where range.contains(Double(selected.frequenciesHz[i])) {
                    if peaks.allSatisfy({abs($0-i)>=3}) {peaks.append(i)}
                    if peaks.count==labelCount {break}
                }
                for (number, bin) in peaks.enumerated() {
                    let point = CGPoint(x: x(Double(selected.frequenciesHz[bin])), y: y(selected.levels[bin]))
                    let caption = CGPoint(x: plot.minX + plot.width * (Double(number) + 0.5) / Double(labelCount), y: 7)
                    // Match the legend by rank without leader lines crossing the entire trace.
                    let badge = CGPoint(x: min(plot.maxX - 12, max(plot.minX + 12, point.x)),
                                        y: max(plot.minY + 9, point.y - 12))
                    label("P\(number + 1)", badge, color: .yellow)
                    context.fill(Path(ellipseIn: CGRect(x: point.x - 3, y: point.y - 3, width: 6, height: 6)), with: .color(.yellow))
                    label("P\(number + 1) \(String(format: "%.3f", Double(selected.frequenciesHz[bin]) / 1_000_000)) МГц · \(String(format: "%.1f", selected.levels[bin])) дБм", caption, color: .yellow)
                }
            }
            for row in timeline.rows {
                guard let rect = timeline.waterfallRect(for: row, in: water) else { continue }
                let rowID=row.sweep.exactID
                var band = context
                band.clip(to: Path(rect))
                if geometryReady, let raster = prepared.rows.first(where: { $0.id == rowID }) {
                    let span=range.upperBound-range.lowerBound
                    let imageRect=CGRect(x:water.minX+(raster.lower-range.lowerBound)/span*water.width,y:rect.minY,
                        width:(raster.upper-raster.lower)/span*water.width,height:rect.height)
                    band.draw(Image(decorative: raster.image, scale: 1), in: imageRect)
                }
                if selectedID == rowID {
                    band.stroke(Path(rect), with: .color(.yellow.opacity(0.85)), lineWidth: 1)
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
                if let row = timeline.rows.first(where: { $0.sweep.exactID == cursor.sweep.exactID }) {
                    let py = water.minY + timeline.fraction(row.end.addingTimeInterval(-row.end.timeIntervalSince(row.start) / 2)) * water.height
                    line(CGPoint(x: water.minX, y: py), CGPoint(x: water.maxX, y: py), color: .white.opacity(0.9))
                }
            }
            NativeAcceptanceMarker.draw(&context,frame:evidence,tag:markerTag)
            if sweeps.isEmpty { label("Нет измерений — выберите сеанс и начните приём", CGPoint(x: plot.midX, y: plot.midY)) }
        }
        .background(Color(red:0.035,green:0.045,blue:0.065))
        .background {if evidence != nil, acceptance?.profile.phase=="load" {NativeAcceptanceGeometry()}}
        .accessibilityLabel("Панорама частот и история водопада")
        .task(id: request) {
            let source=sweeps,selected=selected,scale=bounds,threshold=threshold,reference=reference,previous=prepared,raster=request.raster
            let task=Task.detached(priority:.userInitiated) {
                try NativeSpectrumRenderCache.prepare(source,selected:selected,bounds:scale,
                    threshold:threshold,reference:reference,viewport:raster?.range,rasterWidth:raster?.pixelWidth,previous:previous)
            }
            do {
                let result=try await withTaskCancellationHandler(operation:{try await task.value},onCancel:{task.cancel()})
                guard !Task.isCancelled else{return}
                prepared=result;preparedRequest=request
            } catch is CancellationError {} catch {acceptance?.record("native.error",["message":error.localizedDescription])}
        }
    }
}
