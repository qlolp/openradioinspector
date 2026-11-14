import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite(.serialized)
struct NativeConnectionHealthTests {
    final class Stub: URLProtocol {
        static var handler: ((Stub) -> Void)?
        override class func canInit(with request: URLRequest) -> Bool { true }
        override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
        override func startLoading() { Self.handler?(self) }
        override func stopLoading() {}
        func reply(_ value: Any, status: Int = 200) {
            let body: [String: Any] = status == 200
                ? ["contract": "openradioinspector-platform", "contract_version": 1, "data": value]
                : ["error": ["code": "unavailable", "message": "Synthetic failure"]]
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status,
                httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
            client?.urlProtocolDidFinishLoading(self)
        }
    }

    final class Responses: @unchecked Sendable {
        private let lock = NSLock()
        private var timestamp: Date
        private var failure = false
        private var empty = false
        private var revoked = false
        private var observationDate: Date?
        init(date: Date = Date(), observationDate: Date? = nil) { timestamp = date; self.observationDate = observationDate }
        func change(date: Date? = nil, fail: Bool = false, empty: Bool = false, revoked: Bool = false) {
            lock.withLock {
                if let date { timestamp = date }
                failure = fail; self.empty = empty; self.revoked = revoked
            }
        }
        func handle(_ stub: Stub) {
            lock.withLock {
                let session: [String: Any] = ["id": "s", "name": "Synthetic", "state": "running", "streams": [
                    ["id": "a", "device": "tinySA", "role": "spectrum_analyzer", "level_unit": "dBm", "state": "connected", "capabilities": ["sweep"]],
                    ["id": "r", "device": "AR-DV1", "role": "receiver", "level_unit": "s_meter_count", "state": "connected", "capabilities": ["tune"]]]]
                let date = ISO8601DateFormatter().string(from: timestamp)
                switch stub.request.url!.path {
                case "/api/v1/sessions": stub.reply([session], status: revoked ? 401 : failure ? 503 : 200)
                case "/api/v1/sessions/s": stub.reply(session, status: revoked ? 401 : failure ? 503 : 200)
                case "/api/v1/sessions/s/sweeps": stub.reply(empty ? [] : [["id": date, "session_id": "s", "stream_id": "a", "level_unit": "dBm", "frequencies_hz": [100, 200], "levels": [-80, -70], "interval": ["started_at": date, "ended_at": date]]])
                case "/api/v1/sessions/s/observations":
                    if let observationDate {
                        let observed = ISO8601DateFormatter().string(from: observationDate)
                        stub.reply([["id": "receiver-new", "session_id": "s", "stream_id": "r", "frequency_hz": 100, "mode": "FM", "level_unit": "s_meter_count", "level": 5, "interval": ["started_at": observed, "ended_at": observed]]])
                    } else { stub.reply([]) }
                case "/api/v1/sessions/s/workers": stub.reply([])
                default: stub.reply([:], status: 404)
                }
            }
        }
    }

    @MainActor private func connect(_ responses: Responses, authenticated: Bool = false) throws -> RemoteDualModel {
        Stub.handler = responses.handle
        let model = RemoteDualModel(makeClient: { base in
            let configuration = URLSessionConfiguration.ephemeral
            configuration.protocolClasses = [Stub.self]
            return try PlatformClient(baseURL: base, session: URLSession(configuration: configuration),
                credential: authenticated ? PlatformBearerCredential(token: "synthetic-test-token") : nil)
        })
        model.serverURL = "https://example.test"
        model.connect(startPolling: false)
        return model
    }
    @MainActor private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(5))
        }
        Issue.record("Synthetic response did not finish")
    }

    @Test @MainActor func intentDoesNotConfirmConnection() async throws {
        let responses = Responses(); responses.change(fail: true)
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        #expect(model.connected)
        #expect(!model.coreReachable)
        try await waitFor { model.errorText != nil }
        #expect(model.connected)
        #expect(!model.coreReachable)
        #expect(model.lastCoreContactAt == nil)
        #expect(model.measurementsStale)
    }

    @Test @MainActor func transientFailureRetainsOnlyStaleDisplayAndRequiresNewMeasurement() async throws {
        let responses = Responses(date: Date().addingTimeInterval(-3))
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && !model.current.isEmpty }
        #expect(!model.measurementsStale)
        let original = model.current, time = model.lastMeasurementAt
        responses.change(fail: true); await model.refresh()
        #expect(model.connected && !model.coreReachable)
        #expect(model.measurementsStale)
        #expect(model.current.isEmpty)
        #expect(model.spectrumPresentation.sweeps.isEmpty)
        #expect(model.displayCurrent == original)
        #expect(model.lastMeasurementAt == time)
        responses.change(); await model.refresh()
        #expect(model.coreReachable)
        #expect(model.measurementsStale)
        #expect(model.lastMeasurementAt == time)
        responses.change(date: Date()); await model.refresh()
        #expect(!model.measurementsStale)
        #expect(model.lastMeasurementAt != time)
    }

    @Test @MainActor func archivedSamplesAndEmptySnapshotsNeverClaimFreshness() async throws {
        let responses = Responses(date: Date().addingTimeInterval(-3600))
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && !model.current.isEmpty }
        #expect(model.measurementsStale)
        let time = model.lastMeasurementAt
        await model.refresh()
        #expect(model.lastMeasurementAt == time)
        #expect(model.measurementsStale)
        model.selectedSessionID = "other"
        #expect(model.lastMeasurementAt == nil)
        #expect(model.displayCurrent.isEmpty)
        model.disconnect()
        #expect(!model.coreReachable)
        #expect(model.lastCoreContactAt == nil)
    }

    @Test @MainActor func changingServerClearsStaleFallback() async throws {
        let responses = Responses()
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && !model.current.isEmpty }
        responses.change(fail: true); await model.refresh()
        #expect(!model.displayCurrent.isEmpty)
        model.serverURL += "/other"
        #expect(model.staleSpectrum == nil)
        #expect(model.displayCurrent.isEmpty)
        #expect(model.lastMeasurementAt == nil)
    }
    @Test @MainActor func authorizationRevocationClearsTrustedAndStaleMeasurements() async throws {
        let responses = Responses()
        let model = try connect(responses, authenticated: true)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && !model.current.isEmpty }
        responses.change(fail: true); await model.refresh()
        #expect(!model.displayCurrent.isEmpty)
        responses.change(revoked: true); await model.refresh()
        #expect(model.authorizationRequired)
        #expect(!model.connected && !model.coreReachable)
        #expect(model.displayCurrent.isEmpty)
        #expect(model.staleSpectrum == nil)
        #expect(model.lastMeasurementAt == nil)
    }

    @Test @MainActor func metadataWithoutMeasurementsConfirmsOnlyCore() async throws {
        let responses = Responses(); responses.change(empty: true)
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && model.session != nil }
        #expect(model.lastCoreContactAt != nil)
        #expect(model.measurementsStale)
        #expect(model.lastMeasurementAt == nil)
        #expect(model.displayCurrent.isEmpty)
    }

    @Test @MainActor func recentReceiverCannotMakeArchivedAnalyzerSpectrumFresh() async throws {
        let responses = Responses(date: Date().addingTimeInterval(-3600), observationDate: Date())
        let model = try connect(responses)
        defer { model.disconnect(); Stub.handler = nil }
        try await waitFor { model.coreReachable && !model.current.isEmpty }
        #expect(model.latestObservation != nil)
        #expect(model.measurementsStale)
        #expect(try #require(model.lastMeasurementAt).timeIntervalSinceNow < -3500)
        await model.refresh()
        #expect(model.measurementsStale)
    }

}
