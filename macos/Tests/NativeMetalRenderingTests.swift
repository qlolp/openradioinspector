import Foundation
import MetalKit
import XCTest
import SpektrKomandy
@testable import DvoynoyObzorSession

final class NativeMetalRenderingTests: XCTestCase {
    @MainActor func testMetalWaterfallMatchesFallbackPaletteAndNewestFirstOrientation() throws {
        if MTLCreateSystemDefaultDevice() == nil || ProcessInfo.processInfo.environment["ORI_NATIVE_RENDERER"] == "canvas" {
            XCTAssertNil(NativeMetalResources.shared)
            let image = try XCTUnwrap(WaterfallBitmap.makeImage(rows: [[-100, -50, 0], [0, -100, -50]], scale: WaterfallScale(low: -100, high: 0)))
            XCTAssertEqual(image.width, 3); XCTAssertEqual(image.height, 2)
            let bytes = try XCTUnwrap(image.dataProvider?.data) as Data
            XCTAssertEqual(Array(bytes.prefix(12)), [252, 255, 163, 255, 0, 0, 5, 255, 130, 23, 110, 255])
            print("Native renderer: verified bitmap fallback")
            return
        }
        let resources = try XCTUnwrap(NativeMetalResources.shared, "Native shader pipelines must compile on a Metal host")
        var history = WaterfallHistory(capacity: 3)
        history.absorb([.init(frequency: 1, level: -100), .init(frequency: 2, level: -50), .init(frequency: 3, level: 0)])
        history.absorb([.init(frequency: 1, level: 0), .init(frequency: 2, level: -100), .init(frequency: 3, level: -50)])
        var state = PlatformWaterfallTexture()
        let update = try state.update(history, scale: PlatformSpectrumScale(lower: -100, upper: 0)!)
        let sourceDescriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: 3, height: 3, mipmapped: false)
        sourceDescriptor.storageMode = .shared; sourceDescriptor.usage = .shaderRead
        let source = try XCTUnwrap(resources.device.makeTexture(descriptor: sourceDescriptor))
        for row in update.uploads {
            row.rgba.withUnsafeBytes { source.replace(region: MTLRegionMake2D(0, row.row, 3, 1), mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: 12) }
        }
        let targetDescriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: 3, height: 2, mipmapped: false)
        targetDescriptor.storageMode = .shared; targetDescriptor.usage = .renderTarget
        let target = try XCTUnwrap(resources.device.makeTexture(descriptor: targetDescriptor))
        let pass = MTLRenderPassDescriptor(); pass.colorAttachments[0].texture = target; pass.colorAttachments[0].loadAction = .clear; pass.colorAttachments[0].storeAction = .store
        let command = try XCTUnwrap(resources.queue.makeCommandBuffer())
        let encoder = try XCTUnwrap(command.makeRenderCommandEncoder(descriptor: pass))
        encoder.setRenderPipelineState(resources.waterfall); encoder.setFragmentTexture(source, index: 0)
        var info = SIMD4<UInt32>(UInt32(state.newestRow), UInt32(state.visibleRows), UInt32(state.height), UInt32(state.width))
        encoder.setFragmentBytes(&info, length: 16, index: 0); encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        encoder.endEncoding(); command.commit(); command.waitUntilCompleted()
        XCTAssertEqual(command.status, .completed, command.error?.localizedDescription ?? "GPU command failed")
        var actual = [UInt8](repeating: 0, count: 24)
        actual.withUnsafeMutableBytes { target.getBytes($0.baseAddress!, bytesPerRow: 12, from: MTLRegionMake2D(0, 0, 3, 2), mipmapLevel: 0) }
        let rgba = state.orderedRGBA()
        let expected = stride(from: 0, to: rgba.count, by: 4).flatMap { [rgba[$0 + 2], rgba[$0 + 1], rgba[$0], rgba[$0 + 3]] }
        XCTAssertEqual(actual, expected)
    }
}
