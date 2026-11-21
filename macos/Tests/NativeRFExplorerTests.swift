import Foundation
import Testing
import SpektrKomandy
@testable import DvoynoyObzorSession

@Suite struct NativeRFExplorerTests {
    private final class Core {
        let port = "/dev/cu.usbserial-fixture"
        private let lock = NSLock()
        private var actions: [String] = []
        private var created = false
        private var running = false
        private var acquiring = false
        private var holdSessionStart: Bool
        private var failNextWorker: Bool
        private var pendingStart: NativeNodeConnectionTests.Stub?
        var startIsPending: Bool { lock.withLock { pendingStart != nil } }
        var posted: [String] { lock.withLock { actions } }
        let timestamp = ISO8601DateFormatter().string(from: Date())

        init(holdSessionStart: Bool = false, failNextWorker: Bool = false) {
            self.holdSessionStart = holdSessionStart
            self.failNextWorker = failNextWorker
        }

        func releaseSessionStart() throws {
            let stub = lock.withLock {
                holdSessionStart = false
                defer { pendingStart = nil }
                return pendingStart
            }
            if let stub { try reply(stub) }
        }

        private var sweep: [String: Any] {
            ["id": "captured", "session_id": "rf", "stream_id": "source", "level_unit": "dBm",
             "interval": ["started_at": timestamp, "ended_at": timestamp],
             "settings": ["detector": "max_hold", "rbw_hz": 10_000, "active_input": "right"],
             "frequencies_hz": (0..<112).map { 100_000_000 + $0 * 10_000 },
             "levels": (0..<112).map { -100.0 + Double($0 % 9) / 2 }]
        }

        func reply(_ stub: NativeNodeConnectionTests.Stub) throws {
            try lock.withLock {
                let path = stub.request.url!.path
                let sessionPath = "/api/v1/sessions/rf"
                let workerPath = sessionPath + "/streams/source"
                if holdSessionStart, stub.request.httpMethod == "POST", path == sessionPath + "/start" {
                    pendingStart = stub
                    return
                }
                if stub.request.httpMethod == "POST" {
                    actions.append(path)
                    if failNextWorker, path == workerPath + "/start" {
                        failNextWorker = false
                        try stub.reply([:], status: 503)
                        return
                    }
                    switch path {
                    case "/api/v1/sessions": created = true
                    case sessionPath + "/start": running = true
                    case workerPath + "/start": acquiring = true
                    case workerPath + "/stop": acquiring = false
                    default: break
                    }
                }
                let session: [String: Any] = ["id": "rf", "name": "RF Explorer", "state": running ? "running" : "draft",
                    "streams": [["id": "source", "device": "RF Explorer 3G Combo", "role": "spectrum_analyzer",
                        "level_unit": "dBm", "port": port, "capabilities": ["sweep","waterfall"],
                        "state": acquiring ? "connected" : "disconnected", "settings": ["adapter": "rf-explorer-3g-serial"]]]]
                let worker: [String: Any] = ["stream_id": "source", "state": acquiring ? "running" : "stopped", "alive": acquiring]
                switch path {
                case "/api/v1/capabilities":
                    try stub.reply(["features": ["rf_explorer_3g_spectrum": true, "graceful_device_stop": true]])
                case "/api/v1/sessions":
                    if stub.request.httpMethod == "POST" { try stub.reply(session) }
                    else { try stub.reply(created ? [session] : []) }
                case sessionPath, sessionPath + "/start": try stub.reply(session)
                case workerPath + "/start", workerPath + "/stop": try stub.reply(worker)
                case sessionPath + "/workers": try stub.reply([worker])
                case sessionPath + "/sweeps": try stub.reply([sweep])
                case sessionPath + "/sweeps/captured": try stub.reply(sweep)
                case sessionPath + "/sweep-summaries":
                    try stub.reply(["items": [["id": "captured", "session_id": "rf", "stream_id": "source",
                        "level_unit": "dBm", "point_count": 112, "min_frequency_hz": 100_000_000, "max_frequency_hz": 101_110_000,
                        "peak_frequency_hz": 100_080_000, "peak_level": -96,
                        "interval": ["started_at": timestamp, "ended_at": timestamp]]]])
                default: try stub.reply([], status: 404)
                }
            }
        }
    }

    @MainActor private func withConnectedCore(_ core: Core,
        perform: @MainActor (RemoteDualModel, PlatformClient) async throws -> Void) async throws {
        let host = "rf-\(UUID().uuidString.lowercased()).example"
        NativeNodeConnectionTests.Stub.register(host: host, handler: core.reply)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [NativeNodeConnectionTests.Stub.self]
        let http = URLSession(configuration: configuration)
        let model = RemoteDualModel()
        defer { model.disconnect(); http.invalidateAndCancel(); NativeNodeConnectionTests.Stub.remove([host]) }
        model.serverURL = "https://\(host)"
        let client = try PlatformClient(baseURL: model.serverURL, session: http)
        model.connect(startPolling: false, using: client)
        for _ in 0..<400 where !model.coreReachable { try await Task.sleep(for: .milliseconds(5)) }
        #expect(model.coreReachable)
        try await perform(model, client)
    }

    @Test @MainActor func connectSelectsRFExplorerAndDisplaysPersistedBinsAfterStop() async throws {
        let core = Core()
        try await withConnectedCore(core) { model, client in
            let connected = await model.connectRFExplorer(try PlatformRFExplorerConfiguration(port: core.port))
            #expect(connected)
            #expect(model.selectedSessionID == "rf")
            #expect(model.isStandaloneRFExplorer)
            #expect(model.tinySAAnalyzer == nil)
            #expect(model.receiver == nil)
            #expect(model.sessions.map(\.id) == ["rf"])
            #expect(model.worker(for: "source")?.alive == true)
            #expect(core.posted == ["/api/v1/sessions", "/api/v1/sessions/rf/start", "/api/v1/sessions/rf/streams/source/start"])
            let sweep = try #require(model.displayInspectorSweeps.last)
            #expect(model.displayCurrent.count == 112)
            #expect(model.displayWaterfall.rows.last == sweep.levels)
            #expect(sweep.frequenciesHz.first == 100_000_000 && sweep.frequenciesHz.last == 101_110_000)
            let description = NativeSpectrumRangeHeader.acquisitionDetails(sweep, rfExplorer: true)
            #expect(description.contains("Max Hold"))
            #expect(description.contains("10000 Гц"))
            #expect(description.contains("правый вход SMA"))
            model.stopStream(model.analyzer)
            for _ in 0..<400 where model.busy { try await Task.sleep(for: .milliseconds(5)) }
            #expect(core.posted.last == "/api/v1/sessions/rf/streams/source/stop")
            #expect(model.worker(for: "source")?.alive == false)
            #expect(model.displayInspectorSweeps.last?.id == "captured")
            let archive = NativeArchiveBrowser()
            await archive.search(client: client, session: model.selectedSessionID, stream: "source", loadSources: true)
            #expect(archive.rows.first?.pointCount == 112)
            #expect(archive.sources.first == sweep)
        }
    }

    @Test @MainActor func changingSelectionDuringSessionStartKeepsSelectionAndDoesNotOpenPort() async throws {
        let core = Core(holdSessionStart: true)
        try await withConnectedCore(core) { model, _ in
            let configuration = try PlatformRFExplorerConfiguration(port: core.port)
            let connecting = Task { await model.connectRFExplorer(configuration) }
            for _ in 0..<400 where !core.startIsPending { try await Task.sleep(for: .milliseconds(5)) }
            #expect(core.startIsPending)
            model.selectSession("another")
            try core.releaseSessionStart()
            let connected = await connecting.value
            #expect(!connected)
            #expect(model.selectedSessionID == "another")
            #expect(!core.posted.contains("/api/v1/sessions/rf/streams/source/start"))
            #expect(!model.busy)
        }
    }

    @Test @MainActor func failedWorkerCanRestartInTheAlreadySelectedSession() async throws {
        let core = Core(failNextWorker: true)
        try await withConnectedCore(core) { model, _ in
            let connected = await model.connectRFExplorer(try PlatformRFExplorerConfiguration(port: core.port))
            #expect(!connected)
            #expect(model.errorText != nil)
            #expect(model.selectedSessionID == "rf")
            model.startStream(model.analyzer)
            for _ in 0..<400 where model.busy { try await Task.sleep(for: .milliseconds(5)) }
            #expect(model.worker(for: "source")?.alive == true)
            #expect(core.posted == ["/api/v1/sessions", "/api/v1/sessions/rf/start",
                "/api/v1/sessions/rf/streams/source/start", "/api/v1/sessions/rf/streams/source/start"])
        }
    }
}
