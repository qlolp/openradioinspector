import Foundation
import Combine
import ServiceManagement

@MainActor
final class NativeLocalService: ObservableObject {
    enum Phase: Equatable { case inactive, checking, approval, stopped, ready, failed }
    enum Registration: Equatable { case enabled, approval, absent }
    static let plistName = "org.openradioinspector.macos.rc.core.plist"
    static let endpoint = "http://127.0.0.1:8772"
    static var isPersonalRC: Bool { Bundle.main.object(forInfoDictionaryKey: "ORIPersonalRC") as? Bool == true }

    @Published private(set) var phase: Phase = .inactive
    @Published private(set) var message = "Служба ещё не проверена. Подключение приборов и охрана включаются отдельно."
    @Published private(set) var status: NativeServiceStatus?
    @Published private(set) var busy = false
    private var generation = 0
    private var identityChecked = false
    private var expectedIdentity: String?
    private let expectedBackendID: @Sendable () async throws -> String?
    private let exchange: @Sendable (String, Duration) async throws -> Data
    private let registration: @MainActor () async throws -> Registration
    private let unregister: @MainActor () async throws -> Void

    init(exchange: @escaping @Sendable (String, Duration) async throws -> Data = { try await NativeServiceSocket.request($0, timeout: $1) },
         registration: @escaping @MainActor () async throws -> Registration = {
             let service = SMAppService.agent(plistName: NativeLocalService.plistName)
             if service.status == .notRegistered { try service.register() }
             switch service.status {
             case .enabled: return .enabled
             case .requiresApproval: return .approval
             default: return .absent
             }
         }, unregister: @escaping @MainActor () async throws -> Void = {
             try await SMAppService.agent(plistName: NativeLocalService.plistName).unregister()
         }, expectedBackendID: @escaping @Sendable () async throws -> String? = {
             guard await NativeLocalService.isPersonalRC else { return nil }
             return try await Task.detached { try NativeRCBundle.validateCurrentInstallation() }.value
         }) {
        self.exchange = exchange; self.registration = registration; self.unregister = unregister; self.expectedBackendID = expectedBackendID
    }

    private func begin(clearStatus: Bool = true) -> Int {
        generation += 1; busy = true
        if clearStatus { phase = .checking; status = nil }
        return generation
    }
    private func current(_ token: Int) -> Bool { token == generation && !Task.isCancelled }
    private func verifyIdentity(_ reply: NativeServiceReply) async throws {
        if !identityChecked { expectedIdentity = try await expectedBackendID(); identityChecked = true }
        guard expectedIdentity == nil || reply.backendID == expectedIdentity else {
            throw NativeServiceError.differentBuild
        }
    }
    private func accept(_ reply: NativeServiceReply) {
        status = reply.readyStatus
        if let status {
            phase = .ready
            message = status.guardArmed ? "Служба готова, охрана работает. Command-Q закроет только интерфейс." : "Rust-служба готова. Приборы и охрана требуют явной подготовки."
        } else {
            phase = .stopped
            switch reply.reason {
            case "system_sleep_preflight_required":
                message = "После сна зафиксирован разрыв покрытия. Ядро остановлено; запустите его и повторите проверку приборов."
            case "idle_sleep_protection_failed":
                message = "macOS не подтвердила защиту от idle sleep. Ядро остановлено; устраните ошибку перед охраной."
            default:
                message = "Служба зарегистрирована, ядро остановлено. Запуск не подключает приборы автоматически."
            }
        }
    }
    private func fail(_ error: Error, token: Int) {
        guard current(token) else { return }
        phase = .failed; status = nil
        message = "Служба недоступна: \(error.localizedDescription) Автоматической замены ядра нет."
    }
    func inspect() async {
        guard !busy else { return }
        let token = begin(clearStatus: false)
        defer { if token == generation { busy = false } }
        do {
            let reply = try NativeServiceReply.decode(await exchange("status", .seconds(2)))
            guard current(token) else { return }
            try await verifyIdentity(reply)
            guard current(token) else { return }
            accept(reply)
        } catch {
            guard current(token) else { return }
            let registration = SMAppService.agent(plistName: Self.plistName).status
            if registration == .notRegistered {
                phase = .stopped; status = nil
                message = "Служба ещё не зарегистрирована. Нажмите «Запустить службу»; подключение приборов выполняется отдельно."
            } else if registration == .requiresApproval {
                phase = .approval; status = nil
                message = "Разрешите фоновую службу в настройках macOS."
            } else { fail(error, token: token) }
        }
    }
    func start() async {
        guard !busy else { return }
        let token = begin()
        defer { if token == generation { busy = false } }
        do {
            if !identityChecked { expectedIdentity = try await expectedBackendID(); identityChecked = true }
            let state = try await registration()
            guard current(token) else { return }
            if state == .approval {
                phase = .approval; message = "Разрешите фоновую службу OpenRadioInspector в настройках macOS, затем повторите запуск."
                return
            }
            guard state == .enabled else { throw NativeServiceError.unavailable("LaunchAgent не зарегистрирован из установленного приложения.") }
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: .seconds(30))
            while true {
                do {
                    let reply = try NativeServiceReply.decode(await exchange("status", .seconds(2)))
                    if let expectedIdentity, reply.backendID != expectedIdentity {
                        throw NativeServiceError.differentBuild
                    }
                    break
                }
                catch {
                    if let serviceError = error as? NativeServiceError, case .differentBuild = serviceError { throw error }
                    guard current(token), clock.now < deadline else { throw error }
                    try await Task.sleep(for: .milliseconds(200))
                }
            }
            guard current(token) else { return }
            let reply = try NativeServiceReply.decode(await exchange("start", .seconds(35)))
            guard current(token) else { return }
            try await verifyIdentity(reply)
            guard current(token) else { return }
            accept(reply)
        } catch { fail(error, token: token) }
    }
    func stop() async {
        let token = begin()
        defer { if token == generation { busy = false } }
        do {
            let data = try await exchange("stop", .seconds(125))
            guard current(token) else { return }
            struct Ack: Decodable { let ok: Bool }
            guard try JSONDecoder().decode(Ack.self, from: data).ok else {
                throw NativeServiceError.unavailable("Остановка и освобождение ресурсов не подтверждены. Обновление заблокировано.")
            }
            // The private ACK follows the actual core join and lease/listener
            // release. Only then remove registration; never kill by PID/port.
            try await unregister()
            guard current(token) else { return }
            phase = .stopped; status = nil
            message = "Служба остановлена, ресурсы освобождены. Измерения сохранены."
        } catch { fail(error, token: token) }
    }

    /// Explicit recovery when normal private control is unavailable. Removing
    /// registration alone is never reported as proof that resources are free.
    func removeRegistrationForRecovery() async {
        let token = begin()
        defer { if token == generation { busy = false } }
        do {
            try await unregister()
            guard current(token) else { return }
            phase = .stopped; status = nil
            message = "Регистрация снята для восстановления. Освобождение ресурсов не подтверждено; обновление пока запрещено. Следующий запуск проверит владельца архива и порт."
        } catch { fail(error, token: token) }
    }
}
