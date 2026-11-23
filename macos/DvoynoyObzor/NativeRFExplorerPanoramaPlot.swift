import SwiftUI
import SpektrKomandy

struct NativeRFExplorerPanoramaPlot: View {
    let panorama: PlatformRFExplorerPanorama
    let showHold: Bool
    let selected: PlatformRFExplorerPoint?
    let markers: [PlatformFrequencyBookmarkEntry]
    let select: (Int64) -> Void

    var body: some View {
        let summary = panorama.summary
        let values = (panorama.columns + (showHold ? panorama.holdColumns ?? [] : [])).compactMap { $0?.levelDbm }
        let scale = PlatformSpectrumScale.fit(values + [selected?.levelDbm].compactMap { $0 })
        let lower = scale?.lower ?? -120, upper = scale?.upper ?? -10
        VStack(spacing: 2) {
            HStack(spacing: 5) {
                VStack(alignment: .trailing) {
                    Text(upper.formatted())
                    Spacer()
                    Text("дБм")
                    Spacer()
                    Text(lower.formatted())
                }.font(.caption2).frame(width: 38)
                GeometryReader { geometry in
                    Canvas { ctx, size in
                        let width = Double(summary.stopHz - summary.startHz)
                        func x(_ hz: Int64) -> CGFloat { CGFloat(Double(hz - summary.startHz) / width) * size.width }
                        func y(_ level: Double) -> CGFloat { size.height * CGFloat((upper - level) / max(1, upper - lower)) }
                        for gap in panorama.gaps {
                            let left = x(gap.lowerBound), right = x(gap.upperBound)
                            ctx.fill(Path(CGRect(x: left, y: 0, width: max(1, right - left), height: size.height)),
                                     with: .color(.gray.opacity(0.28)))
                        }
                        for part in 0...4 {
                            var grid = Path()
                            let height = size.height * CGFloat(part) / 4
                            grid.move(to: .init(x: 0, y: height)); grid.addLine(to: .init(x: size.width, y: height))
                            ctx.stroke(grid, with: .color(.white.opacity(0.15)))
                        }
                        func trace(_ points: [PlatformRFExplorerPoint?], _ spans: [PlatformRFExplorerCoverage], _ color: Color) {
                            var path = Path(), segment = 0, previous: Int?
                            for column in points {
                                guard let point = column else { previous = nil; continue }
                                while segment < spans.count && spans[segment].stopHz < point.frequencyHz { segment += 1 }
                                let position = CGPoint(x: x(point.frequencyHz), y: y(point.levelDbm))
                                if previous == segment { path.addLine(to: position) }
                                else {
                                    path.move(to: position)
                                    ctx.fill(Path(ellipseIn: CGRect(x: position.x - 1, y: position.y - 1, width: 2, height: 2)), with: .color(color))
                                }
                                previous = segment
                            }
                            ctx.stroke(path, with: .color(color), lineWidth: 1.4)
                        }
                        trace(panorama.columns, panorama.coverage, .cyan)
                        if showHold, let points = panorama.holdColumns, let spans = panorama.holdCoverage {
                            trace(points, spans, .orange)
                        }
                        for marker in markers where (summary.startHz...summary.stopHz).contains(marker.frequencyHz) {
                            let left = x(marker.frequencyHz)
                            var line = Path()
                            line.move(to: .init(x: left, y: 0)); line.addLine(to: .init(x: left, y: size.height))
                            ctx.stroke(line, with: .color(.yellow.opacity(0.7)), style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
                            ctx.draw(Text(marker.label).font(.caption2).foregroundStyle(.yellow),
                                     at: .init(x: left, y: 2), anchor: left < size.width / 2 ? .topLeading : .topTrailing)
                        }
                        if let selected {
                            let rect = CGRect(x: x(selected.frequencyHz) - 3, y: y(selected.levelDbm) - 3, width: 6, height: 6)
                            ctx.fill(Path(ellipseIn: rect), with: .color(.white))
                        }
                    }
                    .background(Color(red: 0.045, green: 0.065, blue: 0.09))
                    .contentShape(Rectangle())
                    .onTapGesture { point in
                        let fraction = min(1, max(0, point.x / max(1, geometry.size.width)))
                        let index = Int64((Double(summary.expectedPoints - 1) * Double(fraction)).rounded())
                        select(summary.startHz + index * summary.stepHz)
                    }
                    .accessibilityLabel("Панорама прохода, \(summary.uniquePoints) из \(summary.expectedPoints) частот")
                }
            }
            HStack {
                Text(FrequencyText.formatMHzRussian(fromHertz: summary.startHz))
                Spacer()
                Text("МГц")
                Spacer()
                Text(FrequencyText.formatMHzRussian(fromHertz: summary.stopHz))
            }.font(.caption2).padding(.leading, 43)
        }
    }
}
