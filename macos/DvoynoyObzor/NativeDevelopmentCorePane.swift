import SwiftUI
import AppKit

struct NativeDevelopmentCorePane: View {
    let core: NativeDevelopmentCore
    @ObservedObject var model: RemoteDualModel
    @ObservedObject var connection: NativeNodeConnectionModel
    @Binding var connectionTask: Task<Void, Never>?
    @Binding var windowClosed: Bool
    @State private var configurationError: String?

    var body: some View {
        DisclosureGroup("Локальное ядро на этом Mac") {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Button(core.ownsProcess ? "Подключиться к локальному ядру" : "Запустить локальное ядро") {
                        connectionTask?.cancel()
                        let originalServer = model.serverURL
                        let originalClient = model.client
                        let originalConnected = model.connected
                        let originalPhase = connection.phase
                        let originalRequest = connection.authorizedNode?.request
                        connectionTask = Task {
                            guard !Task.isCancelled, !windowClosed else { return }
                            if !core.ownsProcess { await core.start() }
                            guard !Task.isCancelled, !windowClosed,
                                  model.serverURL == originalServer, model.client === originalClient,
                                  model.connected == originalConnected, connection.phase == originalPhase,
                                  connection.authorizedNode?.request == originalRequest,
                                  let endpoint = core.endpoint else { return }
                            connection.cancel()
                            model.disconnect()
                            model.serverURL = endpoint
                            if let node = await connection.preflight(server: endpoint),
                               !Task.isCancelled, !windowClosed, model.serverURL == endpoint,
                               core.endpoint == endpoint, connection.accepts(node) {
                                model.connect(using: node.client)
                            }
                        }
                    }.disabled(core.busy)
                    Button("Остановить локальное ядро", role: .destructive) {
                        connectionTask?.cancel()
                        connectionTask = nil
                        Task {
                            if model.serverURL == core.endpoint { connection.cancel(); model.disconnect() }
                            await core.stop()
                        }
                    }.disabled(!core.ownsProcess || core.busy)
                    Button("Выбрать окружение…", action: chooseInterpreter)
                        .disabled(core.ownsProcess || core.busy)
                    if core.busy { ProgressView().controlSize(.small) }
                }
                Text(core.message).font(.caption)
                Text("Вспомогательное ядро принадлежит этому окну. Закрытие окна останавливает ядро и приём. Для автономной охраны используйте отдельную службу.")
                    .font(.caption).foregroundStyle(.secondary)
                if let configurationError { Text(configurationError).font(.caption).foregroundStyle(.red) }
                DisclosureGroup("Настройки запуска") {
                    Text(core.configurationDescription).font(.caption).textSelection(.enabled)
                }
            }
        }.padding(.horizontal, 16).padding(.top, 8)
    }

    private func chooseInterpreter() {
        let panel = NSOpenPanel()
        panel.title = "Выберите Python подготовленного окружения OpenRadioInspector"
        panel.prompt = "Выбрать"
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.showsHiddenFiles = true
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            do {
                try core.configurePython(executable: url)
                configurationError = nil
            } catch {
                configurationError = error.localizedDescription
            }
        }
    }
}
