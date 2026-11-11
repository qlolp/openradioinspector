import AppKit
import MetalKit
import SwiftUI
import SpektrKomandy

typealias NativeMetalVertex = PlatformTraceVertex
typealias NativeTraceRequest = PlatformTraceRequest
typealias NativeTraceMesh = PlatformTraceMesh

@MainActor
final class NativeMetalResources {
    static let shared: NativeMetalResources? = ProcessInfo.processInfo.environment["ORI_NATIVE_RENDERER"] == "canvas" ? nil : NativeMetalResources()
    let device: MTLDevice
    let queue: MTLCommandQueue
    let trace: MTLRenderPipelineState
    let waterfall: MTLRenderPipelineState
    private init?() {
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue(),
              let library = try? device.makeLibrary(source: Self.shaders, options: nil) else { return nil }
        func pipeline(vertex: String, fragment: String) -> MTLRenderPipelineState? {
            let descriptor = MTLRenderPipelineDescriptor()
            descriptor.vertexFunction = library.makeFunction(name: vertex); descriptor.fragmentFunction = library.makeFunction(name: fragment)
            descriptor.colorAttachments[0].pixelFormat = .bgra8Unorm
            return try? device.makeRenderPipelineState(descriptor: descriptor)
        }
        guard let trace = pipeline(vertex: "trace_vertex", fragment: "trace_fragment"),
              let waterfall = pipeline(vertex: "waterfall_vertex", fragment: "waterfall_fragment") else { return nil }
        self.device = device; self.queue = queue; self.trace = trace; self.waterfall = waterfall
    }
    private static let shaders = """
    #include <metal_stdlib>
    using namespace metal;
    struct Vertex { float2 position; float4 color; };
    struct TraceOut { float4 position [[position]]; float4 color; float pointSize [[point_size]]; };
    vertex TraceOut trace_vertex(uint id [[vertex_id]], constant Vertex *vertices [[buffer(0)]]) {
        TraceOut out; out.position = float4(vertices[id].position, 0, 1); out.color = vertices[id].color; out.pointSize = 5; return out;
    }
    fragment float4 trace_fragment(TraceOut in [[stage_in]]) { return in.color; }
    struct WaterfallOut { float4 position [[position]]; float2 uv; };
    vertex WaterfallOut waterfall_vertex(uint id [[vertex_id]]) {
        float2 p[4] = {float2(-1,-1), float2(-1,1), float2(1,-1), float2(1,1)};
        WaterfallOut out; out.position = float4(p[id],0,1); out.uv = float2((p[id].x+1)*0.5,(1-p[id].y)*0.5); return out;
    }
    fragment float4 waterfall_fragment(WaterfallOut in [[stage_in]], texture2d<float> pixels [[texture(0)]], constant uint4 &info [[buffer(0)]]) {
        uint x = min(uint(in.uv.x * info.w), info.w-1);
        uint logical = min(uint(in.uv.y * info.y), info.y-1);
        uint row = (info.x + info.z - logical) % info.z;
        return pixels.read(uint2(x,row));
    }
    """
}

struct NativeTracePlot: View {
    let current: [PlotPoint], minimum: [PlotPoint], maximum: [PlotPoint]
    let threshold: Double?
    let displayScale: PlatformSpectrumScale?
    @Environment(\.colorScheme) private var colorScheme
    @State private var mesh: NativeTraceMesh?
    var body: some View {
        GeometryReader { geometry in
            let input = NativeTraceRequest(current: current, minimum: minimum, maximum: maximum, threshold: threshold, scale: displayScale,
                                           dark: colorScheme == .dark, width: geometry.size.width, height: geometry.size.height)
            NativeMetalPlot(mesh: mesh, waterfall: nil)
                .task(id: input) {
                    let work = Task.detached(priority: .userInitiated) { try NativeTraceMesh(input) }
                    let value = try? await withTaskCancellationHandler(operation: { try await work.value }, onCancel: { work.cancel() })
                    guard !Task.isCancelled else { return }; mesh = value
                }
        }
    }
}

struct NativeWaterfallFrame: Sendable {
    let texture: PlatformWaterfallTexture
    let update: PlatformWaterfallTexture.Update
    let previousGeneration: Int
    let generation: Int
}

/// On-demand draw calls; no display-link timer, unbounded GPU queue or hardware access.
struct NativeMetalPlot: NSViewRepresentable {
    var mesh: NativeTraceMesh?
    var waterfall: NativeWaterfallFrame?
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> MTKView {
        let view = MTKView(frame: .zero, device: NativeMetalResources.shared?.device)
        view.colorPixelFormat = .bgra8Unorm; view.enableSetNeedsDisplay = true; view.isPaused = true
        view.clearColor = MTLClearColorMake(0, 0, 0, 0); view.layer?.isOpaque = false
        view.delegate = context.coordinator; return view
    }
    func updateNSView(_ view: MTKView, context: Context) {
        context.coordinator.set(mesh: mesh, waterfall: waterfall)
        view.needsDisplay = true
    }
    @MainActor
    final class Coordinator: NSObject, @preconcurrency MTKViewDelegate {
        private var traces: [(MTLBuffer, Int)] = []
        private var dashes: (MTLBuffer, Int)?
        private var texture: MTLTexture?
        private var info = SIMD4<UInt32>(repeating: 0)
        private var generation = -1
        private var inFlight = false
        private var inputRevision = 0
        private var pendingMesh: NativeTraceMesh?
        private var pendingWaterfall: NativeWaterfallFrame?
        private var appliedMesh: NativeTraceMesh?
        func set(mesh: NativeTraceMesh?, waterfall: NativeWaterfallFrame?) {
            if mesh != pendingMesh || waterfall?.generation != pendingWaterfall?.generation {
                pendingMesh = mesh; pendingWaterfall = waterfall; inputRevision += 1
            }
        }
        private func apply(mesh: NativeTraceMesh?, waterfall frame: NativeWaterfallFrame?) {
            guard let resources = NativeMetalResources.shared else { return }
            if let mesh, mesh != appliedMesh {
                appliedMesh = mesh
                func buffer(_ vertices: [NativeMetalVertex]) -> (MTLBuffer, Int)? {
                    guard !vertices.isEmpty else { return nil }
                    return vertices.withUnsafeBytes { bytes in
                        resources.device.makeBuffer(bytes: bytes.baseAddress!, length: bytes.count, options: .storageModeShared).map { ($0, vertices.count) }
                    }
                }
                traces = mesh.traces.compactMap { buffer($0.vertices) }; dashes = buffer(mesh.threshold)
            }
            guard let frame, generation != frame.generation, frame.texture.visibleRows > 0 else { return }
            let state = frame.texture
            let reset = texture == nil || texture?.width != state.width || texture?.height != state.height || frame.update.reset || generation != frame.previousGeneration
            if reset {
                let descriptor = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .rgba8Unorm, width: state.width, height: state.height, mipmapped: false)
                descriptor.usage = .shaderRead; descriptor.storageMode = .shared
                texture = resources.device.makeTexture(descriptor: descriptor)
            }
            let uploads = reset ? state.completeUploads() : frame.update.uploads
            for upload in uploads {
                upload.rgba.withUnsafeBytes { bytes in
                    texture?.replace(region: MTLRegionMake2D(0, upload.row, state.width, 1), mipmapLevel: 0, withBytes: bytes.baseAddress!, bytesPerRow: state.width * 4)
                }
            }
            generation = frame.generation
            info = SIMD4(UInt32(state.newestRow), UInt32(state.visibleRows), UInt32(state.height), UInt32(state.width))
        }
        func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}
        func draw(in view: MTKView) {
            guard !inFlight, let resources = NativeMetalResources.shared, let pass = view.currentRenderPassDescriptor, let drawable = view.currentDrawable,
                  let command = resources.queue.makeCommandBuffer() else { return }
            // The previous command has completed before shared texture storage is touched.
            apply(mesh: pendingMesh, waterfall: pendingWaterfall)
            guard let encoder = command.makeRenderCommandEncoder(descriptor: pass) else { return }
            inFlight = true
            let renderedRevision = inputRevision
            command.addCompletedHandler { [weak self, weak view] _ in
                Task { @MainActor [weak self, weak view] in
                    guard let self else { return }; self.inFlight = false
                    if self.inputRevision != renderedRevision { view?.needsDisplay = true }
                }
            }
            if let texture, info.y > 0 {
                encoder.setRenderPipelineState(resources.waterfall); encoder.setFragmentTexture(texture, index: 0)
                encoder.setFragmentBytes(&info, length: MemoryLayout.size(ofValue: info), index: 0)
                encoder.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
            } else {
                encoder.setRenderPipelineState(resources.trace)
                for (buffer, count) in traces {
                    encoder.setVertexBuffer(buffer, offset: 0, index: 0)
                    encoder.drawPrimitives(type: count == 1 ? .point : .lineStrip, vertexStart: 0, vertexCount: count)
                }
                if let (buffer, count) = dashes {
                    encoder.setVertexBuffer(buffer, offset: 0, index: 0); encoder.drawPrimitives(type: .line, vertexStart: 0, vertexCount: count)
                }
            }
            encoder.endEncoding(); command.present(drawable); command.commit()
        }
    }
}
