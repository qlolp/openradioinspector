import Foundation

/// Optional local diagnostics; timestamps never replace original measurement UTC.
public struct PlatformMeasurementTiming: Sendable {
    public enum Stage: String, Sendable { case received, decoded, queued }
    public let stage: Stage
    public let sourceID: String?
    public let eventType: String
    public let sequence: UInt64
    public let transport: PlatformMeasurementTransport
    /// Host monotonic uptime nanoseconds, not wall time or GPU completion.
    public let atNanoseconds: UInt64
    public let queuedEvents: Int?
    public let payloadBytes: Int
}
