import Foundation
import Observation

struct NativeDevelopmentCoreConfiguration: Sendable {
    static let pythonPreference = "ori.developmentCore.pythonExecutable"
    static let checkoutPreference = "ori.developmentCore.checkout"
    let executable: URL
    let prefixArguments: [String]
    let checkout: URL?
    let dataDirectory: URL

    static var defaultDataDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/OpenRadioInspector/macOS-Development", isDirectory: true)
    }

    static var sourceCheckout: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    static func python(executable: URL, checkout: URL = sourceCheckout,
                       dataDirectory: URL = defaultDataDirectory) throws -> Self {
        guard executable.isFileURL, FileManager.default.isExecutableFile(atPath: executable.path),
              FileManager.default.fileExists(atPath: checkout.appendingPathComponent("radioinspector/launcher.py").path) else {
            throw NativeDevelopmentCoreError.unavailable("Выберите Python из подготовленного окружения и каталог исходников OpenRadioInspector.")
        }
        return Self(executable: executable, prefixArguments: ["-m", "radioinspector.launcher"], checkout: checkout, dataDirectory: dataDirectory)
    }

    static func discover(bundle: Bundle = .main, environment: [String: String] = ProcessInfo.processInfo.environment,
                         preferences: UserDefaults = .standard) throws -> Self {
        let helper = bundle.bundleURL.appendingPathComponent("Contents/Helpers/openradioinspector-core/openradioinspector-core")
        if FileManager.default.isExecutableFile(atPath: helper.path) {
            return Self(executable: helper, prefixArguments: [], checkout: nil, dataDirectory: defaultDataDirectory)
        }
        let checkout = (environment["ORI_DEVELOPMENT_CHECKOUT"] ?? preferences.string(forKey: checkoutPreference))
            .map { URL(fileURLWithPath: $0, isDirectory: true) } ?? sourceCheckout
        if let configured = environment["ORI_DEVELOPMENT_PYTHON"] ?? preferences.string(forKey: pythonPreference) {
            return try python(executable: URL(fileURLWithPath: configured), checkout: checkout)
        }
        for name in [".venv/bin/python3", ".venv/bin/python"] {
            let executable = checkout.appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: executable.path) {
                return try python(executable: executable, checkout: checkout)
            }
        }
        throw NativeDevelopmentCoreError.unavailable("Локальное ядро не найдено. Выберите Python из подготовленного окружения проекта. Установка зависимостей автоматически не выполняется.")
    }

    func arguments(readyFile: URL) -> [String] {
        prefixArguments + ["--data-dir", dataDirectory.path, "--host", "127.0.0.1", "--port", "0", "--ready-file", readyFile.path, "--stdin-lifeline"]
    }
}

enum NativeDevelopmentCoreError: LocalizedError {
    case unavailable(String)
    var errorDescription: String? {
        switch self { case .unavailable(let message): return message }
    }
}

@MainActor
protocol NativeDevelopmentCoreChild: AnyObject {
    var isRunning: Bool { get }
    func closeLifeline()
}

@MainActor
private final class NativeDevelopmentCoreProcess: NativeDevelopmentCoreChild {
    let process: Process
    let input: Pipe
    var isRunning: Bool { process.isRunning }

    init(configuration: NativeDevelopmentCoreConfiguration, readyFile: URL) throws {
        process = Process()
        input = Pipe()
        process.executableURL = configuration.executable
        process.arguments = configuration.arguments(readyFile: readyFile)
        process.currentDirectoryURL = configuration.checkout
        var environment = ProcessInfo.processInfo.environment
        // A selected interpreter must not inherit another environment's module overrides.
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        process.environment = environment
        process.standardInput = input
        // The core writes its own rotating diagnostics under its data directory.
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
    }

    func closeLifeline() { try? input.fileHandleForWriting.close() }
}

/// Development helper only. The personal RC always uses its supervised Rust service.
@Observable @MainActor
final class NativeDevelopmentCore {
    enum Phase: Equatable { case stopped, starting, ready, stopping, failed }
    private(set) var phase: Phase = .stopped
    private(set) var message = "Запуск локального ядра выполняется явно. Приборы подключаются отдельно."
    private(set) var endpoint: String?
    private(set) var ownsProcess = false
    private(set) var configurationDescription = "Локальное окружение определяется при запуске."
    var busy: Bool { phase == .starting || phase == .stopping }

    @ObservationIgnored private var configuration: NativeDevelopmentCoreConfiguration?
    @ObservationIgnored private var child: (any NativeDevelopmentCoreChild)?
    @ObservationIgnored private var runtimeDirectory: URL?
    @ObservationIgnored private var monitor: Task<Void, Never>?
    @ObservationIgnored private var generation = 0
    @ObservationIgnored private let isPersonalRC: Bool
    @ObservationIgnored private let resolve: @MainActor () throws -> NativeDevelopmentCoreConfiguration
    @ObservationIgnored private let launch: @MainActor (NativeDevelopmentCoreConfiguration, URL) throws -> any NativeDevelopmentCoreChild
    @ObservationIgnored private let health: @Sendable (URL) async throws -> Void
    @ObservationIgnored private let startupTimeout: Duration
    @ObservationIgnored private let stopTimeout: Duration
    @ObservationIgnored private let preferences: UserDefaults

    init(isPersonalRC: Bool = NativeLocalService.isPersonalRC,
         resolve: (@MainActor () throws -> NativeDevelopmentCoreConfiguration)? = nil,
         launch: @escaping @MainActor (NativeDevelopmentCoreConfiguration, URL) throws -> any NativeDevelopmentCoreChild = {
             try NativeDevelopmentCoreProcess(configuration: $0, readyFile: $1)
         }, health: @escaping @Sendable (URL) async throws -> Void = NativeDevelopmentCore.checkHealth,
         startupTimeout: Duration = .seconds(30), stopTimeout: Duration = .seconds(30),
         preferences: UserDefaults = .standard) {
        self.isPersonalRC = isPersonalRC
        self.resolve = resolve ?? { try .discover(preferences: preferences) }
        self.launch = launch
        self.health = health
        self.startupTimeout = startupTimeout
        self.stopTimeout = stopTimeout
        self.preferences = preferences
    }

    func configurePython(executable: URL, checkout: URL? = nil) throws {
        guard !isPersonalRC, !ownsProcess, !busy else {
            throw NativeDevelopmentCoreError.unavailable("Окружение нельзя менять при работающем ядре или в personal RC.")
        }
        let selectedCheckout = checkout ?? NativeDevelopmentCoreConfiguration.sourceCheckout
        configuration = try .python(executable: executable, checkout: selectedCheckout)
        configurationDescription = executable.path
        preferences.set(executable.path, forKey: NativeDevelopmentCoreConfiguration.pythonPreference)
        preferences.set(selectedCheckout.path, forKey: NativeDevelopmentCoreConfiguration.checkoutPreference)
    }

    static func readinessEndpoint(_ data: Data) throws -> URL {
        guard data.count <= 1024, let text = String(data: data, encoding: .utf8),
              let url = URL(string: text.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme == "http", url.host == "127.0.0.1", let port = url.port, (1...65535).contains(port),
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/" else {
            throw NativeDevelopmentCoreError.unavailable("Ядро опубликовало неверный локальный адрес.")
        }
        return url
    }

    nonisolated static func checkHealth(_ endpoint: URL) async throws {
        var request = URLRequest(url: endpoint.appendingPathComponent("api/v1/health"))
        request.timeoutInterval = 2
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              response.url == request.url, data.count <= 65536,
              let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let payload = object["data"] as? [String: Any], payload["status"] as? String == "ok" else {
            throw NativeDevelopmentCoreError.unavailable("Локальное ядро не подтвердило готовность API v1.")
        }
    }

    func start() async {
        guard !busy, !ownsProcess else { return }
        guard !isPersonalRC else {
            phase = .failed
            message = "Personal RC использует только штатную Rust-службу. Запустите её в панели службы."
            return
        }
        generation += 1
        let token = generation
        phase = .starting
        message = "Запускается локальное ядро; приборы остаются отключёнными."
        do {
            let selected = try configuration ?? resolve()
            configurationDescription = selected.executable.path
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("ori-native-" + UUID().uuidString, isDirectory: true)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            runtimeDirectory = directory
            let ready = directory.appendingPathComponent("ready")
            child = try launch(selected, ready)
            ownsProcess = true
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: startupTimeout)
            while clock.now < deadline {
                try Task.checkCancellation()
                guard token == generation else { return }
                guard child?.isRunning == true else {
                    throw NativeDevelopmentCoreError.unavailable("Процесс ядра завершился. Проверьте зависимости выбранного окружения и журнал в \(selected.dataDirectory.path).")
                }
                if let data = try? Data(contentsOf: ready) {
                    let url = try Self.readinessEndpoint(data)
                    try await health(url)
                    guard token == generation else { return }
                    guard child?.isRunning == true else {
                        throw NativeDevelopmentCoreError.unavailable("Локальное ядро завершилось во время проверки готовности.")
                    }
                    try Task.checkCancellation()
                    endpoint = url.absoluteString
                    phase = .ready
                    message = "Локальное ядро готово. Закрытие приложения остановит это ядро. Приборы подключаются отдельно."
                    monitor = Task { [weak self] in
                        while !Task.isCancelled {
                            try? await Task.sleep(for: .milliseconds(500))
                            guard let self, self.generation == token else { return }
                            if self.child?.isRunning != true {
                                self.releaseExitedChild()
                                self.phase = .failed
                                self.message = "Локальное ядро завершилось. Повторный запуск выполняется явно."
                                return
                            }
                        }
                    }
                    return
                }
                try await Task.sleep(for: .milliseconds(100))
            }
            throw NativeDevelopmentCoreError.unavailable("Истекло время ожидания локального ядра.")
        } catch {
            guard token == generation else { return }
            let failure = error.localizedDescription
            // Cleanup must proceed even when the initiating UI task was cancelled.
            await Task { await self.stop() }.value
            phase = .failed
            message = failure + (ownsProcess ? " Завершение процесса ещё не подтверждено; повторный запуск заблокирован." : "")
        }
    }

    func stop() async {
        guard phase != .stopping else { return }
        generation += 1
        monitor?.cancel()
        monitor = nil
        endpoint = nil
        guard let child else {
            releaseExitedChild()
            phase = .stopped
            return
        }
        phase = .stopping
        child.closeLifeline()
        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: stopTimeout)
        while child.isRunning, clock.now < deadline {
            // Yield even if the caller was cancelled; never busy-spin during shutdown.
            await Task { try? await Task.sleep(for: .milliseconds(100)) }.value
        }
        guard !child.isRunning else {
            phase = .failed
            message = "Ядро ещё завершает работу. Владение сохранено; повторите остановку."
            return
        }
        releaseExitedChild()
        phase = .stopped
        message = "Локальное ядро остановлено. Архив сохранён."
    }

    private func releaseExitedChild() {
        child?.closeLifeline()
        child = nil
        ownsProcess = false
        endpoint = nil
        if let runtimeDirectory { try? FileManager.default.removeItem(at: runtimeDirectory) }
        runtimeDirectory = nil
    }
}
