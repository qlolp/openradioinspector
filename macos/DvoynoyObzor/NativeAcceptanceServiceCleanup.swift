import Foundation

@MainActor
enum NativeAcceptanceServiceCleanup {
    static func finish(service:NativeLocalService,publish:@MainActor(Bool,String)->Void) async {
        // This reuses the real private stop ACK, followed by SMAppService.unregister.
        // A process exit or an idle supervisor alone cannot authorize another run.
        await service.stop()
        publish(service.phase == .stopped,service.message)
    }
}
