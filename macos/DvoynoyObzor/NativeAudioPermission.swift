import AVFoundation
import Combine

/// UI permission is distinct from actual LaunchAgent capture authorization.
/// No constructor, status query or grant starts a recording or selects an input.
@MainActor
final class NativeAudioPermission: ObservableObject {
    enum Phase: Equatable { case unavailable, notRequested, requesting, authorized, denied, restricted }
    @Published private(set) var phase: Phase
    private let enabled: Bool
    private let authorization: @MainActor () -> AVAuthorizationStatus
    private let prompt: @MainActor () async -> Bool

    init(enabled: Bool = NativeRCBundle.nativeAudioPackage,
         authorization: @escaping @MainActor () -> AVAuthorizationStatus = { AVCaptureDevice.authorizationStatus(for: .audio) },
         request: @escaping @MainActor () async -> Bool = { await AVCaptureDevice.requestAccess(for: .audio) }) {
        self.enabled = enabled; self.authorization = authorization; self.prompt = request
        phase = enabled ? .notRequested : .unavailable
    }
    func inspect() {
        guard enabled, phase != .requesting else { return }
        switch authorization() {
        case .authorized: phase = .authorized
        case .notDetermined: phase = .notRequested
        case .denied: phase = .denied
        case .restricted: phase = .restricted
        @unknown default: phase = .restricted
        }
    }
    func request() async {
        guard enabled, phase != .requesting else { return }
        inspect()
        guard phase == .notRequested else { return }
        phase = .requesting
        phase = await prompt() ? .authorized : .denied
    }
    var message: String {
        switch phase {
        case .unavailable: "Аудиозахват недоступен в этом пакете. RC1 остаётся без аудио."
        case .notRequested: "Разрешение на аудиовход Mac ещё не запрошено. Источник выбирается отдельно в подготовке проекта."
        case .requesting: "Ожидание решения macOS о доступе к аудиовходу."
        case .authorized: "Разрешение приложению выдано. Доступ фоновой службы и реальный тракт ещё нужно проверить записью."
        case .denied: "Доступ к аудиовходу отклонён. Разрешите его в настройках macOS; ошибка записи службы остаётся видимой."
        case .restricted: "macOS ограничивает доступ к аудиовходу. Обзор tinySA не зависит от аудиозаписи."
        }
    }
}
