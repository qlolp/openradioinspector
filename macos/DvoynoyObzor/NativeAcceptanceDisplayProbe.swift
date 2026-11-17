import AppKit
import CoreMedia
import CoreVideo
import ScreenCaptureKit

/// Observes only the small code drawn in the actual Canvas pass. No images are retained.
final class NativeAcceptanceDisplayProbe: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let tag:UInt32
    private let observed:@Sendable (UInt64,UInt64)->Void
    private let queue=DispatchQueue(label:"org.openradioinspector.acceptance.display")
    private var stream:SCStream?
    private init(tag:UInt32,observed:@escaping @Sendable (UInt64,UInt64)->Void) {
        self.tag=tag;self.observed=observed
    }
    @MainActor static func start(rect:NSRect,window:NSWindow,tag:UInt32,
        observed:@escaping @Sendable (UInt64,UInt64)->Void) async throws -> NativeAcceptanceDisplayProbe {
        guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
            throw NSError(domain:"ORI.Acceptance",code:1,userInfo:[NSLocalizedDescriptionKey:
                "Для измерения фактического показа разрешите запись экрана этому RC в настройках macOS и повторите тест. Снимки экрана не сохраняются."])
        }
        try await NativeAcceptanceDisplayReadiness.wait {window.isVisible && window.occlusionState.contains(.visible)}
        guard let primary=NSScreen.screens.first(where:{($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value==CGMainDisplayID()}) else {
            throw NSError(domain:"ORI.Acceptance",code:3,userInfo:[NSLocalizedDescriptionKey:"Экран графика недоступен; фактический показ не измерен."])
        }
        let global=CGRect(x:rect.minX,y:primary.frame.maxY-rect.maxY,width:rect.width,height:rect.height)
        let content=try await SCShareableContent.excludingDesktopWindows(false,onScreenWindowsOnly:true)
        guard let display=content.displays.first(where:{$0.frame.contains(global)}) else {
            throw NSError(domain:"ORI.Acceptance",code:4,userInfo:[NSLocalizedDescriptionKey:
                "Маркер графика находится вне видимой области экрана. Поместите всё окно на экран и повторите тест."])
        }
        let config=SCStreamConfiguration()
        config.sourceRect=global.offsetBy(dx:-display.frame.minX,dy:-display.frame.minY)
        config.width=128;config.height=64;config.pixelFormat=kCVPixelFormatType_32BGRA
        config.minimumFrameInterval=CMTime(value:1,timescale:60);config.queueDepth=3;config.showsCursor=false;config.capturesAudio=false
        let value=NativeAcceptanceDisplayProbe(tag:tag,observed:observed)
        let stream=SCStream(filter:SCContentFilter(display:display,excludingWindows:[]),configuration:config,delegate:value)
        try stream.addStreamOutput(value,type:.screen,sampleHandlerQueue:value.queue)
        value.stream=stream
        try await stream.startCapture()
        return value
    }
    func stream(_ stream:SCStream,didStopWithError error:any Error) {
        let message=error.localizedDescription
        Task { @MainActor in NativeAcceptanceTrace.shared?.record("native.error",["message":message,"presentation":"unmeasured"]) }
    }
    func stream(_ stream:SCStream,didOutputSampleBuffer sampleBuffer:CMSampleBuffer,of type:SCStreamOutputType) {
        guard type == .screen,CMSampleBufferIsValid(sampleBuffer),
              let attachments=CMSampleBufferGetSampleAttachmentsArray(sampleBuffer,createIfNecessary:false) as? [[SCStreamFrameInfo:Any]],
              let info=attachments.first,let status=info[.status] as? Int,SCFrameStatus(rawValue:status) == .complete,
              let ticks=info[.displayTime] as? NSNumber,let image=CMSampleBufferGetImageBuffer(sampleBuffer),
              CVPixelBufferGetPixelFormatType(image)==kCVPixelFormatType_32BGRA,
              CVPixelBufferGetWidth(image)==128,CVPixelBufferGetHeight(image)==64,
              CVPixelBufferLockBaseAddress(image,.readOnly)==kCVReturnSuccess else{return}
        defer {CVPixelBufferUnlockBaseAddress(image,.readOnly)}
        guard let base=CVPixelBufferGetBaseAddress(image) else{return}
        let stride=CVPixelBufferGetBytesPerRow(image)
        guard stride>=128*4 else{return}
        let pixels=base.assumingMemoryBound(to:UInt8.self)
        let bits=(0..<128).map {index -> Bool in
            let offset=(index/16*8+4)*stride+(index%16*8+4)*4
            return Int(pixels[offset])+Int(pixels[offset+1])+Int(pixels[offset+2])>384
        }
        guard let id=NativeAcceptanceMarker.decode(bits,tag:tag) else{return}
        observed(id,NativeAcceptanceClock.fromMachTicks(ticks.uint64Value))
    }
}
