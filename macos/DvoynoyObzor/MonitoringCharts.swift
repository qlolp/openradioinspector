import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

struct MonitoringCurve {
    let title: String
    let color: Color
    let points: [PlotPoint]
    var connectsPoints = true
}

/// Display geometry only. All levels, peak choices, and baseline deltas come from the core.
struct MonitoringCurveChart: View {
    let curves: [MonitoringCurve]
    var timeAxis = false

    private var points: [PlotPoint] { curves.flatMap(\.points).filter { $0.x.isFinite && $0.y.isFinite } }
    private var xBounds: ClosedRange<Double> {
        let low = points.map(\.x).min() ?? 0, high = points.map(\.x).max() ?? 1
        return low...(high > low ? high : low + 1)
    }
    private var levelScale: WaterfallScale { WaterfallScale.auto(rows: curves.map { $0.points.map(\.y) }) ?? .init(low: -120, high: 0) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if points.isEmpty { Text("Нет точек для графика.").font(.caption) }
            else {
                HStack(alignment: .top, spacing: 6) {
                    VStack { Text(String(format: "%.1f", levelScale.high)); Spacer(); Text(String(format: "%.1f", levelScale.low)) }
                        .font(.caption.monospacedDigit()).frame(width: 48, height: 150)
                    Canvas { context, size in
                        let xBounds = self.xBounds, levelScale = self.levelScale
                        for curve in curves {
                            let valid = curve.points.filter { $0.x.isFinite && $0.y.isFinite }.sorted { $0.x < $1.x }
                            var path = Path()
                            for (index, point) in valid.enumerated() {
                                let location = CGPoint(x: (point.x - xBounds.lowerBound) / (xBounds.upperBound - xBounds.lowerBound) * size.width,
                                                       y: (1 - levelScale.fraction(point.y)) * size.height)
                                if index == 0 { path.move(to: location) } else { path.addLine(to: location) }
                                context.fill(Path(ellipseIn: CGRect(x: location.x - 2, y: location.y - 2, width: 4, height: 4)), with: .color(curve.color))
                            }
                            if curve.connectsPoints { context.stroke(path, with: .color(curve.color), lineWidth: 1.3) }
                        }
                    }.frame(height: 150).background(Color.primary.opacity(0.04)).clipped()
                }
                HStack {
                    Text(axis(xBounds.lowerBound)); Spacer(); Text(timeAxis ? "UTC" : "Гц"); Spacer(); Text(axis(xBounds.upperBound))
                }.font(.caption.monospacedDigit())
                HStack {
                    ForEach(Array(curves.enumerated()), id: \.offset) { _, curve in
                        Text("● " + curve.title).foregroundStyle(curve.color).font(.caption)
                    }
                }
            }
        }
    }

    private func axis(_ value: Double) -> String { timeAxis ? Self.utc(value) : String(format: "%.0f", value) }
    static func utc(_ value: Double) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: Date(timeIntervalSince1970: value))
    }
}

/// Actual UTC intervals on the vertical axis; each row retains its own core-selected grid.
struct MonitoringHistoryTimeline: View {
    let rows: [MonitoringHistoryRow]
    let selected: String
    let select: (String) -> Void
    private var lowerTime: Double { rows.map { $0.startedAt.timeIntervalSince1970 }.min() ?? 0 }
    private var upperTime: Double { max(rows.map { $0.endedAt.timeIntervalSince1970 }.max() ?? 1, lowerTime + 0.001) }
    private var lowerFrequency: Double { Double(rows.compactMap { $0.frequenciesHz.first }.min() ?? 0) }
    private var upperFrequency: Double { max(Double(rows.compactMap { $0.frequenciesHz.last }.max() ?? 1), lowerFrequency + 1) }
    private var scale: WaterfallScale { WaterfallScale.auto(rows: rows.map(\.levels)) ?? .init(low: -120, high: 0) }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("Частота: \(String(format: "%.0f", lowerFrequency)) Гц")
                Spacer()
                Text("\(String(format: "%.0f", upperFrequency)) Гц")
            }.font(.caption.monospacedDigit())
            HStack(spacing: 6) {
                VStack(alignment: .leading) {
                    Text(MonitoringCurveChart.utc(lowerTime)); Spacer(); Text("UTC ↓"); Spacer(); Text(MonitoringCurveChart.utc(upperTime))
                }.font(.caption.monospacedDigit()).frame(width: 180, height: 190)
                Canvas { context, size in
                    let lowerTime = self.lowerTime, upperTime = self.upperTime
                    let lowerFrequency = self.lowerFrequency, upperFrequency = self.upperFrequency
                    let scale = self.scale
                    for row in rows where !row.frequenciesHz.isEmpty {
                        let y = (row.startedAt.timeIntervalSince1970 - lowerTime) / (upperTime - lowerTime) * size.height
                        let bottom = (row.endedAt.timeIntervalSince1970 - lowerTime) / (upperTime - lowerTime) * size.height
                        for index in row.levels.indices {
                            let frequency = Double(row.frequenciesHz[index])
                            let left = index == 0 ? frequency : (Double(row.frequenciesHz[index - 1]) + frequency) / 2
                            let right = index == row.levels.count - 1 ? frequency : (frequency + Double(row.frequenciesHz[index + 1])) / 2
                            let x = (left - lowerFrequency) / (upperFrequency - lowerFrequency) * size.width
                            let endX = (right - lowerFrequency) / (upperFrequency - lowerFrequency) * size.width
                            let rgb = WaterfallPalette.color(scale.fraction(row.levels[index]))
                            context.fill(Path(CGRect(x: x, y: y, width: max(1, endX - x), height: max(1, bottom - y))),
                                         with: .color(Color(red: rgb.red, green: rgb.green, blue: rgb.blue)))
                        }
                        if row.id == selected {
                            let startX = (Double(row.frequenciesHz.first ?? 0) - lowerFrequency) / (upperFrequency - lowerFrequency) * size.width
                            let endX = (Double(row.frequenciesHz.last ?? 0) - lowerFrequency) / (upperFrequency - lowerFrequency) * size.width
                            context.stroke(Path(CGRect(x: startX, y: y, width: max(2, endX - startX), height: max(2, bottom - y))),
                                           with: .color(.yellow), lineWidth: 2)
                        }
                    }
                }
                .frame(height: 190).background(Color.primary.opacity(0.04)).clipped()
                .overlay {
                    GeometryReader { geometry in
                        Color.clear.contentShape(Rectangle()).gesture(SpatialTapGesture().onEnded { value in
                            let stamp = lowerTime + Double(value.location.y / max(1, geometry.size.height)) * (upperTime - lowerTime)
                            if let row = rows.last(where: { $0.startedAt.timeIntervalSince1970 <= stamp && $0.endedAt.timeIntervalSince1970 >= stamp }) {
                                select(row.id)
                            }
                        })
                    }
                }
                .accessibilityLabel("Водопад истории по UTC. Для выбора свипа также используйте список под графиком.")
            }
            Text(String(format: "Цвет: %.1f…%.1f dBm · нажмите интервал свипа", scale.low, scale.high)).font(.caption)
        }
    }
}
