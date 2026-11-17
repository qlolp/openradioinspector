import CoreGraphics
import Foundation
import SpektrKomandy

/// Immutable drawing inputs, prepared outside MainActor. Panning transforms the
/// paths immediately; detail for the new viewport is rasterized off MainActor.
struct NativeSpectrumRenderCache: @unchecked Sendable {
    struct Row: @unchecked Sendable {
        let id:PlatformExactID
        let lower:Double
        let upper:Double
        let image:CGImage
    }
    var rows:[Row]=[]
    var bounds:PlatformSpectrumScale?
    var selectedID:PlatformExactID?
    var lower=0.0,upper=1.0
    var current:CGPath?,minimum:CGPath?,maximum:CGPath?,reference:CGPath?
    var minimumLevels:[Double]=[],maximumLevels:[Double]=[]
    var thresholdPoints:[CGPoint]=[]
    var peakCandidates:[Int]=[]
    var rasterizedRows=0
    var viewport:ClosedRange<Double>?
    private var sourceIDs:[PlatformExactID]=[]
    private var referenceID:PlatformExactID?
    private var threshold:Double?

    nonisolated static func prepare(_ sweeps:[PlatformSweep],selected:PlatformSweep?,bounds:PlatformSpectrumScale?,
        threshold:Double?=nil,reference:PlatformSweep?=nil,viewport:ClosedRange<Double>?=nil,previous:Self=Self()) throws -> Self {
        let ids=sweeps.map(\.exactID)
        let sameGeometry=previous.bounds==bounds && previous.sourceIDs==ids && previous.selectedID==selected?.exactID
            && previous.referenceID==reference?.exactID && previous.threshold==threshold
        var result=sameGeometry ? previous:Self()
        result.rows=[];result.rasterizedRows=0;result.bounds=bounds;result.viewport=viewport
        result.sourceIDs=ids;result.referenceID=reference?.exactID;result.threshold=threshold
        guard let bounds else{return result}
        let reusable=previous.bounds==bounds && previous.viewport==viewport ? Dictionary(previous.rows.map{($0.id,$0)},uniquingKeysWith:{$1}):[:]
        for sweep in sweeps {
            try Task.checkCancellation()
            if let old=reusable[sweep.exactID] {result.rows.append(old)}
            else if let row=try raster(sweep,bounds:bounds,viewport:viewport) {result.rows.append(row);result.rasterizedRows+=1}
        }
        if sameGeometry {return result}
        guard let selected,let first=selected.frequenciesHz.first,let last=selected.frequenciesHz.last else{return result}
        result.selectedID=selected.exactID;result.lower=Double(first);result.upper=max(Double(first)+1,Double(last))
        var low=selected.levels,high=selected.levels
        for sweep in sweeps where sweep.frequenciesHz==selected.frequenciesHz {
            try Task.checkCancellation()
            for i in low.indices {low[i]=min(low[i],sweep.levels[i]);high[i]=max(high[i],sweep.levels[i])}
        }
        result.minimumLevels=low;result.maximumLevels=high
        func point(_ i:Int,_ value:Double)->CGPoint {
            CGPoint(x:(Double(selected.frequenciesHz[i])-result.lower)/(result.upper-result.lower),y:-bounds.fraction(value))
        }
        func path(_ values:[Double])->CGPath {
            let p=CGMutablePath()
            for i in values.indices {
                let value=point(i,values[i])
                if i==0 {p.move(to:value)} else {p.addLine(to:value)}
            }
            return p.copy()!
        }
        result.current=path(selected.levels);result.minimum=path(low);result.maximum=path(high)
        if let reference,reference.frequenciesHz==selected.frequenciesHz,reference.levelUnit==selected.levelUnit {
            result.reference=path(reference.levels)
        }
        if let threshold {
            result.thresholdPoints=selected.levels.indices.filter{selected.levels[$0]>threshold}.map{point($0,selected.levels[$0])}
        }
        let levels=selected.levels
        result.peakCandidates=levels.indices.filter { i in
            (i==0 || levels[i]>levels[i-1]) && (i==levels.count-1 || levels[i]>=levels[i+1])
        }.sorted{levels[$0]==levels[$1] ? $0<$1:levels[$0]>levels[$1]}
        return result
    }
    nonisolated private static func raster(_ sweep:PlatformSweep,bounds:PlatformSpectrumScale,viewport:ClosedRange<Double>?) throws -> Row? {
        guard let first=sweep.frequenciesHz.first,let last=sweep.frequenciesHz.last else{return nil}
        let lower=viewport?.lowerBound ?? Double(first),upper=viewport?.upperBound ?? max(Double(first)+1,Double(last))
        guard lower.isFinite,upper.isFinite,upper>lower else{return nil}
        let width=min(4096,max(1024,sweep.levels.count))
        var maxima=[Double](repeating:-Double.infinity,count:width)
        func lowerBound(_ frequency:Double)->Int {
            var low=0,high=sweep.frequenciesHz.count
            while low<high {
                let middle=low+(high-low)/2
                if Double(sweep.frequenciesHz[middle])<frequency {low=middle+1}else{high=middle}
            }
            return low
        }
        // Include the adjacent centers: their bins can overlap the viewport.
        let start=max(0,lowerBound(lower)-1),end=min(sweep.levels.count,lowerBound(upper)+1)
        for i in start..<end {
            if i%256==0 {try Task.checkCancellation()}
            let f=Double(sweep.frequenciesHz[i])
            let low=i==0 ? f:(Double(sweep.frequenciesHz[i-1])+f)/2
            let high=sweep.levels.count==1 ? f+1:(i==sweep.levels.count-1 ? f:(Double(sweep.frequenciesHz[i+1])+f)/2)
            let clippedLow=max(low,lower),clippedHigh=min(high,upper)
            guard clippedHigh>clippedLow else{continue}
            let a=max(0,min(width-1,Int(floor((clippedLow-lower)/(upper-lower)*Double(width)))))
            let b=max(a,min(width-1,Int(ceil((clippedHigh-lower)/(upper-lower)*Double(width)))-1))
            for x in a...b {maxima[x]=max(maxima[x],sweep.levels[i])}
        }
        var bytes=[UInt8]();bytes.reserveCapacity(width*4)
        for value in maxima {
            if !value.isFinite {bytes += [0,0,0,0];continue}
            let color=WaterfallPalette.color(bounds.fraction(value))
            bytes += [UInt8((color.red*255).rounded()),UInt8((color.green*255).rounded()),UInt8((color.blue*255).rounded()),255]
        }
        guard let provider=CGDataProvider(data:Data(bytes) as CFData),let space=CGColorSpace(name:CGColorSpace.sRGB),
            let image=CGImage(width:width,height:1,bitsPerComponent:8,bitsPerPixel:32,bytesPerRow:width*4,
                space:space,bitmapInfo:CGBitmapInfo(rawValue:CGImageAlphaInfo.premultipliedLast.rawValue),
                provider:provider,decode:nil,shouldInterpolate:false,intent:.defaultIntent) else{return nil}
        return Row(id:sweep.exactID,lower:lower,upper:upper,image:image)
    }
}
