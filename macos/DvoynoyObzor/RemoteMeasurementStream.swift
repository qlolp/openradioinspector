import Foundation
import SpektrKomandy

/// Owns only a client's event subscription. It never sends device/session commands.
@MainActor
final class RemoteMeasurementStream {
    private var task: Task<Void, Never>?
    private var revision = 0
    private var sessionID = ""
    private var streamID = ""
    private var receiverStreamID: String?
    private(set) var measurements: PlatformLiveMeasurements?
    private(set) var appliedSourceCursor: UInt64 = 0
    var isActive: Bool { task != nil && measurements != nil }

    func matches(sessionID: String, streamID: String, receiverStreamID: String? = nil) -> Bool {
        measurements != nil && PlatformExactID.same(self.sessionID, sessionID)
            && PlatformExactID.same(self.streamID, streamID)
            && PlatformExactID.same(self.receiverStreamID, receiverStreamID)
    }

    func stop() {
        revision += 1; task?.cancel(); task = nil; measurements = nil; appliedSourceCursor = 0
    }

    func start(client: PlatformClient, sessionID: String, streamID: String, receiverStreamID: String? = nil, watermark: Int64,
               transport: PlatformMeasurementTransport = .serverSentEvents,
               sweeps: [PlatformSweep], observation: PlatformObservation?,
               receive: @escaping @MainActor ([PlatformSweep], PlatformObservation?, PlatformMeasurementEvent, PlatformLiveMeasurements.Change) async throws -> Void,
               recover: @escaping @MainActor (Error) async -> Void) {
        guard watermark >= 0 else {
            stop(); Task { await recover(PlatformError.invalidCommand("Курсор должен быть неотрицательным")) }; return
        }
        let sourceCursor = matches(sessionID: sessionID, streamID: streamID, receiverStreamID: receiverStreamID)
            ? appliedSourceCursor : UInt64(watermark)
        stop(); self.sessionID = sessionID; self.streamID = streamID; self.receiverStreamID = receiverStreamID
        let requestRevision = revision
        appliedSourceCursor = sourceCursor
        measurements = PlatformLiveMeasurements(sessionID: sessionID, streamID: streamID, after: watermark, sweeps: sweeps, observation: observation, receiverStreamID: receiverStreamID)
        task = Task { [weak self] in
            do {
                try await client.consumeMeasurements(after: sourceCursor, using: transport) { [weak self] event in
                    guard let self else { throw CancellationError() }
                    try await self.apply(event, revision: requestRevision, receive: receive)
                }
                if Task.isCancelled { return }
                throw PlatformError.connection("Поток событий завершён")
            } catch {
                guard !Task.isCancelled, let self, requestRevision == self.revision else { return }
                // Retain the last applied cursor and bounded cache. A health
                // watermark on reconnect would skip committed but unseen rows.
                self.task = nil
                await recover(error)
            }
        }
    }

    private func apply(_ event: PlatformMeasurementEvent, revision expected: Int,
        receive: @MainActor ([PlatformSweep], PlatformObservation?, PlatformMeasurementEvent, PlatformLiveMeasurements.Change) async throws -> Void) async throws {
        guard !Task.isCancelled, expected == revision, var value = measurements else { throw CancellationError() }
        let change = try event.committedEvent.map { try value.apply($0) } ?? .metadata
        try await receive(value.sweeps, value.observation, event, change)
        guard !Task.isCancelled, expected == revision else { throw CancellationError() }
        // Publish only after the consumer applied the model/display/source state.
        // The SDK sends ACK after this method returns; source-only UInt64 values
        // never enter the legacy Int64 plot/cache or block future source events.
        measurements = value; appliedSourceCursor = event.sequence
    }
}
