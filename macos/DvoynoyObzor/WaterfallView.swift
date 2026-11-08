import CoreGraphics
import Foundation
import SpektrKomandy
import SwiftUI

/// Картинка водопада из истории свипов: столбец — частота, строка — свип,
/// свежая строка сверху. Собирается один раз на свип, а не на каждый кадр:
/// модель поднимает revision после принятого свипа.
enum WaterfallBitmap {
    static func makeImage(rows: [[Double]], scale: WaterfallScale, displayScale: PlatformSpectrumScale? = nil) -> CGImage? {
        let height = rows.count
        guard height > 0, let width = rows.first?.count, width > 0 else { return nil }
        guard rows.allSatisfy({ $0.count == width }) else { return nil }
        let space = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
        // Память отводит CoreGraphics: тогда картинка не ссылается
        // на локальный массив, который освобождается при выходе.
        guard let context = CGContext(
            data: nil,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: width * 4,
            space: space,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ), let bytes = context.data?.assumingMemoryBound(to: UInt8.self) else { return nil }
        let stride = context.bytesPerRow
        for line in 0..<height {
            let row = rows[height - 1 - line]
            for column in 0..<width {
                let color = WaterfallPalette.color(displayScale?.fraction(row[column]) ?? scale.fraction(row[column]))
                let offset = line * stride + column * 4
                bytes[offset] = UInt8((color.red * 255).rounded())
                bytes[offset + 1] = UInt8((color.green * 255).rounded())
                bytes[offset + 2] = UInt8((color.blue * 255).rounded())
                bytes[offset + 3] = 255
            }
        }
        return context.makeImage()
    }
}

struct WaterfallView: View {
    var history: WaterfallHistory
    var revision: Int
    var unitTitle: String
    var displayScale: PlatformSpectrumScale? = nil

    @State private var image: CGImage?
    @State private var scale = WaterfallScale(low: 0, high: 1)

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let image {
                Image(decorative: image, scale: 1)
                    .resizable()
                    .interpolation(.none)
                    .frame(maxWidth: .infinity, minHeight: 150, maxHeight: 150)
                    .background(Color.primary.opacity(0.04))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                legend
            } else {
                Text("Водопад появится после первого законченного свипа")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 150)
                    .background(Color.primary.opacity(0.04))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
        }
        .onAppear(perform: rebuild)
        .onChange(of: revision) { _, _ in rebuild() }
        .onChange(of: displayScale) { _, _ in rebuild() }
    }

    private var legend: some View {
        HStack(spacing: 8) {
            Text(format(scale.low))
            gradientBar
            Text(format(scale.high))
            Spacer()
            Text("строк: \(history.rows.count) из \(history.capacity) · \(unitTitle)")
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private var gradientBar: some View {
        let colors = stride(from: 0.0, through: 1.0, by: 0.25).map { step -> Color in
            let rgb = WaterfallPalette.color(step)
            return Color(red: rgb.red, green: rgb.green, blue: rgb.blue)
        }
        return LinearGradient(colors: colors, startPoint: .leading, endPoint: .trailing)
            .frame(width: 120, height: 8)
            .clipShape(RoundedRectangle(cornerRadius: 2))
    }

    private func rebuild() {
        guard let auto = WaterfallScale.auto(rows: history.rows) else {
            image = nil
            return
        }
        scale = displayScale.map { WaterfallScale(low: $0.lower, high: $0.upper) } ?? auto
        image = WaterfallBitmap.makeImage(rows: history.rows, scale: scale, displayScale: displayScale)
    }

    private func format(_ value: Double) -> String {
        String(format: "%.1f", value)
    }
}
