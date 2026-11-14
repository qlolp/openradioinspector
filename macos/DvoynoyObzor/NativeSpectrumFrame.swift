import Foundation
import SpektrKomandy

/// Local rendering adapter; scientific analyses continue to use the core APIs.
/// The existing envelope/legacy threshold display is kept without changing raw bins.
struct NativeSpectrumFrame: Sendable {
    let current: [PlotPoint]
    let minimum: [PlotPoint]
    let maximum: [PlotPoint]
    let rows: [Exceedance]
    let waterfall: WaterfallHistory

    init(sweeps: [PlatformSweep], threshold: Double) throws {
        var envelope = TraceEnvelope(), found: [Exceedance] = []
        var history = WaterfallHistory(capacity: PlatformSpectrumPresentation.capacity)
        for sweep in sweeps {
            try Task.checkCancellation()
            let bins = zip(sweep.frequenciesHz, sweep.levels).map { LevelBin(frequency: $0, level: $1) }
            envelope.absorb(bins)
            found = ExceedanceList.update(existing: found, bins: bins, threshold: threshold)
            history.absorb(bins)
        }
        func plot(_ bins: [LevelBin]) -> [PlotPoint] { bins.map { PlotPoint(x: Double($0.frequency) / 1_000_000, y: $0.level) } }
        current = sweeps.last.map { sweep in zip(sweep.frequenciesHz, sweep.levels).map { PlotPoint(x: Double($0) / 1_000_000, y: $1) } } ?? []
        minimum = plot(envelope.minimumSeries()); maximum = plot(envelope.maximumSeries())
        rows = found; waterfall = history
    }
}
