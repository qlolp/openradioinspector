import SwiftUI
import SpektrKomandy

typealias PlotPoint = PlatformPlotPoint
typealias TraceChartVerticalScale = PlatformTraceVerticalScale

struct TraceChart: View {
    var current: [PlotPoint]
    var minimum: [PlotPoint]
    var maximum: [PlotPoint]
    var threshold: Double?
    var yTitle: String
    var currentTitle = "текущая"
    var thresholdTitle = "порог"
    var displayScale: PlatformSpectrumScale? = nil
    var plotHeight: CGFloat = 220
    var showsGrid = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Group {
                if NativeMetalResources.shared != nil {
                    NativeTracePlot(current: current, minimum: minimum, maximum: maximum, threshold: threshold, displayScale: displayScale)
                } else {
                    Canvas { context, size in draw(context: context, size: size) }
                }
            }
            .frame(height: plotHeight)
            .background {
                if showsGrid {
                    Canvas { context, size in
                        let rect = CGRect(x: 8, y: 8, width: max(size.width - 16, 1), height: max(size.height - 16, 1))
                        var grid = Path()
                        for index in 0...4 {
                            let fraction = Double(index) / 4
                            let y = rect.minY + rect.height * fraction
                            grid.move(to: CGPoint(x: rect.minX, y: y))
                            grid.addLine(to: CGPoint(x: rect.maxX, y: y))
                            let x = rect.minX + rect.width * fraction
                            grid.move(to: CGPoint(x: x, y: rect.minY))
                            grid.addLine(to: CGPoint(x: x, y: rect.maxY))
                        }
                        context.stroke(grid, with: .color(.primary.opacity(0.12)), lineWidth: 0.5)
                    }
                }
            }
            .background(Color.primary.opacity(0.04))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            HStack(spacing: 14) {
                swatch(Color.primary, currentTitle)
                if !minimum.isEmpty { swatch(Color.blue, "минимум") }
                if !maximum.isEmpty { swatch(Color.orange, "максимум") }
                if threshold != nil { swatch(Color.red, thresholdTitle) }
                Spacer()
                Text(yTitle)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private func swatch(_ color: Color, _ title: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 2)
                .fill(color)
                .frame(width: 12, height: 3)
            Text(title)
        }
    }

    private func draw(context: GraphicsContext, size: CGSize) {
        let plot = CGRect(x: 8, y: 8, width: max(size.width - 16, 1), height: max(size.height - 16, 1))
        let samples = current + minimum + maximum
        guard !samples.isEmpty else {
            return
        }
        var xMin = samples.map(\.x).min() ?? 0
        var xMax = samples.map(\.x).max() ?? 1
        var yMin = samples.map(\.y).min() ?? 0
        var yMax = samples.map(\.y).max() ?? 1
        if let threshold {
            yMin = min(yMin, threshold)
            yMax = max(yMax, threshold)
        }
        let verticalScale = TraceChartVerticalScale(minimum: yMin, maximum: yMax)
        if xMax <= xMin {
            xMin -= 1
            xMax += 1
        }
        func map(_ point: PlotPoint) -> CGPoint {
            let xRatio = (point.x - xMin) / (xMax - xMin)
            let yRatio = displayScale?.fraction(point.y) ?? verticalScale.fraction(point.y)
            return CGPoint(
                x: plot.minX + xRatio * plot.width,
                y: plot.maxY - yRatio * plot.height
            )
        }

        stroke(samples: minimum, color: .blue, width: 1, context: context, map: map)
        stroke(samples: maximum, color: .orange, width: 1, context: context, map: map)
        stroke(samples: current, color: .primary, width: 1.6, context: context, map: map)
        if let threshold {
            var line = Path()
            let left = map(PlotPoint(x: xMin, y: threshold))
            let right = map(PlotPoint(x: xMax, y: threshold))
            line.move(to: left)
            line.addLine(to: right)
            context.stroke(
                line,
                with: .color(.red),
                style: StrokeStyle(lineWidth: 1, dash: [5, 4])
            )
        }
    }

    private func stroke(
        samples: [PlotPoint],
        color: Color,
        width: CGFloat,
        context: GraphicsContext,
        map: (PlotPoint) -> CGPoint
    ) {
        guard let first = samples.first else { return }
        if samples.count == 1 {
            let center = map(first)
            let dot = Path(ellipseIn: CGRect(x: center.x - 2.5, y: center.y - 2.5, width: 5, height: 5))
            context.fill(dot, with: .color(color))
            return
        }
        var path = Path()
        path.move(to: map(first))
        for point in samples.dropFirst() {
            path.addLine(to: map(point))
        }
        context.stroke(path, with: .color(color), lineWidth: width)
    }
}
