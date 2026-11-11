import Foundation
import SpektrKomandy

/// Owns only a client's event subscription. It never sends device/session commands.
@MainActor
final class RemoteMeasurementStream {
    private var task: Task<Void, Never>?
    private var revision = 0
    private var sessionID = ""
    private var streamID = ""
    private(set) var measurements: PlatformLiveMeasurements?
    var isActive: Bool { task != nil && measurements != nil }

    func matches(sessionID: String, streamID: String) -> Bool {
        isActive && self.sessionID.utf8.elementsEqual(sessionID.utf8) && self.streamID.utf8.elementsEqual(streamID.utf8)
    }

    func stop() {
        revision += 1; task?.cancel(); task = nil; measurements = nil
    }

    func start(client: PlatformClient, sessionID: String, streamID: String, receiverStreamID: String? = nil, watermark: Int64,
               sweeps: [PlatformSweep], observation: PlatformObservation?,
               receive: @escaping @MainActor ([PlatformSweep], PlatformObservation?, Int64) async -> Void,
               recover: @escaping @MainActor (Error) async -> Void) {
        stop(); self.sessionID = sessionID; self.streamID = streamID
        let requestRevision = revision
        measurements = PlatformLiveMeasurements(sessionID: sessionID, streamID: streamID, after: watermark, sweeps: sweeps, observation: observation, receiverStreamID: receiverStreamID)
        task = Task { [weak self] in
            do {
                for try await event in try client.committedEvents(after: watermark) {
                    guard !Task.isCancelled, let self, requestRevision == self.revision,
                          var value = self.measurements else { return }
                    let change = try value.apply(event)
                    self.measurements = value
                    if change == .measurement { await receive(value.sweeps, value.observation, value.cursor) }
                }
                if Task.isCancelled { return }
                throw PlatformError.connection("Поток событий завершён")
            } catch {
                guard !Task.isCancelled, let self, requestRevision == self.revision else { return }
                self.task = nil; self.measurements = nil
                await recover(error)
            }
        }
    }
}
