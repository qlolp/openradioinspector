import SwiftUI
import ServiceManagement
import AppKit

struct NativeLocalServicePane: View {
    @ObservedObject var service: NativeLocalService
    @StateObject private var audioPermission = NativeAudioPermission()
    let connect: () -> Void
    let disconnect: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label("Личная macOS RC · Rust-служба", systemImage: "desktopcomputer")
                Spacer()
                if service.busy { ProgressView().controlSize(.small) }
            }
            HStack {
                Button("Запустить службу") { Task { await service.start() } }.disabled(service.busy)
                Button("Проверить") { Task { await service.inspect() } }.disabled(service.busy)
                Button("Подключить интерфейс", action: connect).disabled(service.status == nil || service.busy)
                Button("Остановить службу", role: .destructive) {
                    disconnect()
                    Task { await service.stop() }
                }.disabled(service.busy || service.phase == .inactive)
            }
            Text(service.message).font(.callout).foregroundStyle(service.phase == .failed ? Color.red : Color.secondary)
            HStack {
                Button("Папка архива…") {
                    let panel = NSOpenPanel()
                    panel.canChooseFiles = false; panel.canChooseDirectories = true; panel.canCreateDirectories = true
                    panel.allowsMultipleSelection = false
                    panel.prompt = "Выбрать для архива"
                    panel.message = "Выберите пустую папку на этом Mac. Служба проверит копию текущего архива перед переключением."
                    if panel.runModal() == .OK, let destination = panel.url { Task { await service.chooseArchiveDirectory(destination) } }
                }.disabled(service.busy || service.phase != .stopped)
                Text("Папку можно изменить после остановки службы.").font(.caption)
            }
            if !service.archiveLocationMessage.isEmpty { Text(service.archiveLocationMessage).font(.callout).textSelection(.enabled) }
            if service.phase == .approval {
                Button("Открыть настройки фоновых служб") { SMAppService.openSystemSettingsLoginItems() }
            }
            if service.phase == .failed {
                Button("Снять регистрацию для восстановления · охрана остановится", role: .destructive) {
                    disconnect()
                    Task { await service.removeRegistrationForRecovery() }
                }.disabled(service.busy)
            }
            if NativeRCBundle.nativeAudioPackage {
                Text("Аудиокандидат · " + audioPermission.message).font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Разрешить аудиовход Mac") { Task { await audioPermission.request() } }
                        .disabled(service.status == nil || audioPermission.phase != .notRequested)
                    Button("Проверить разрешение") { audioPermission.inspect() }
                    Button("Настройки микрофона") {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") { NSWorkspace.shared.open(url) }
                    }
                }
                Text("Выберите аналоговый выход AR-DV1 в подготовке проекта. Нужен совместимый mono PCM16 вход; USB управления не передаёт аудио. Python/DuckDB не входят в пакет. Аппаратная приёмка не выполнена.")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                Text("Аудиозахват и тяжёлая Python/DuckDB-аналитика в RC1 недоступны. Физическая приёмка ещё не выполнена.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.padding(12).background(.quaternary)
            .task { audioPermission.inspect() }
    }
}
