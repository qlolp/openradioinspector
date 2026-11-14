import SwiftUI
import SpektrKomandy

/// Orthographic 3D projection of raw sweep ridges; no surface is invented across time gaps.
struct NativeWaterfall3D: View {
    let sweeps: [PlatformSweep]
    let range: ClosedRange<Double>
    let bounds: PlatformSpectrumScale?
    var window: ClosedRange<Date>? = nil
    @State private var azimuth = 0.35
    @State private var elevation = 0.55
    @State private var dragStart: CGPoint?

    var body: some View {
        // Capture UI state on MainActor; the drawing helpers use immutable values.
        let viewAzimuth = azimuth
        let viewElevation = elevation
        VStack(spacing: 3) {
            HStack {
                Text("3D · частота × UTC × дБм").font(.caption.bold())
                Spacer()
                Text("Перетаскивание — ракурс").font(.caption2).foregroundStyle(.secondary)
                Button("Сброс ракурса") { azimuth = 0.35; elevation = 0.55 }.controlSize(.mini)
            }
            Canvas { context, size in
                let timeline = NativeSpectrumTimeline(sweeps, window: window)
                guard let bounds, !timeline.rows.isEmpty else { return }
                func project(_ x: Double, _ time: Double, _ level: Double) -> CGPoint {
                    let horizontal = (x - 0.5) * cos(viewAzimuth) + (time - 0.5) * sin(viewAzimuth) * 0.55
                    let depth = -(x - 0.5) * sin(viewAzimuth) + (time - 0.5) * cos(viewAzimuth) * 0.55
                    return CGPoint(x: size.width * (0.5 + horizontal * 0.72),
                                   y: size.height * (0.70 - depth * sin(viewElevation) * 0.65 - level * cos(viewElevation) * 0.48))
                }
                func line(_ points: [CGPoint], color: Color, width: Double = 1) {
                    var path = Path()
                    for (i, p) in points.enumerated() { if i == 0 { path.move(to: p) } else { path.addLine(to: p) } }
                    context.stroke(path, with: .color(color), lineWidth: width)
                }
                func label(_ text: String, _ point: CGPoint) {
                    context.draw(Text(text).font(.system(size: 10, design: .monospaced)).foregroundStyle(.white), at: point)
                }
                for i in 0...4 {
                    let f = Double(i) / 4
                    line([project(f, 0, 0), project(f, 1, 0)], color: .gray.opacity(0.35))
                    line([project(0, f, 0), project(1, f, 0)], color: .gray.opacity(0.35))
                }
                // Older rows first; every measured bin is preserved, including narrow peaks.
                for row in timeline.rows {
                    let time = 1 - timeline.fraction(row.end)
                    var previous: CGPoint?
                    for bin in row.sweep.frequenciesHz.indices {
                        let hz = Double(row.sweep.frequenciesHz[bin])
                        guard range.contains(hz) else { previous = nil; continue }
                        let level = bounds.fraction(row.sweep.levels[bin])
                        let p = project((hz - range.lowerBound) / (range.upperBound - range.lowerBound), time, level)
                        if let previous {
                            let color = WaterfallPalette.color(level)
                            line([previous, p], color: Color(red: color.red, green: color.green, blue: color.blue))
                        }
                        previous = p
                    }
                }
                line([project(0, 0, 0), project(1, 0, 0)], color: .white)
                line([project(0, 0, 0), project(0, 1, 0)], color: .white)
                line([project(0, 0, 0), project(0, 0, 1)], color: .white)
                // Screen-space labels stay readable at every camera angle and window height.
                label(NativeSpectrumInspection.frequency(range.lowerBound), CGPoint(x: size.width * 0.28, y: size.height - 8))
                label(NativeSpectrumInspection.frequency(range.upperBound), CGPoint(x: size.width * 0.8, y: size.height - 8))
                label(String(format: "Верх %.1f дБм", bounds.upper), CGPoint(x: 70, y: 18))
                label(String(format: "Низ %.1f дБм", bounds.lower), CGPoint(x: 70, y: 35))
                if let first = timeline.rows.first, let last = timeline.rows.last {
                    label("UTC " + NativeSpectrumTimeline.utc(first.end) + " → " + NativeSpectrumTimeline.utc(last.end), CGPoint(x: size.width * 0.6, y: 18))
                }
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 3).onChanged { value in
                if dragStart == nil { dragStart = CGPoint(x: azimuth, y: elevation) }
                if let start = dragStart {
                    azimuth = min(0.9, max(-0.9, start.x + value.translation.width / 250))
                    elevation = min(1.1, max(0.15, start.y + value.translation.height / 250))
                }
            }.onEnded { _ in dragStart = nil })
            .accessibilityLabel("Трёхмерные линии свипов: частота, время UTC и уровень дБм. Для точных маркеров выберите 2D.")
        }
    }
}
