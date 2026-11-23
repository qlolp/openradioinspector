import SwiftUI
import SpektrKomandy

private struct NativeMonitoringConnection: Equatable {
    let server: PlatformExactID
    let client: ObjectIdentifier?
}

/// Root-owned tasks continue while pages and inspectors come and go.
/// Nothing here opens a device or changes reception/guard state.
struct NativeMonitoringLifecycle: View {
    @Bindable var model: RemoteDualModel
    @ObservedObject var workspace: MonitoringWorkspaceModel
    @State private var sessionFollow = MonitoringProjectSessionFollowState()

    private var endpoint: String { model.connected ? model.serverURL : "" }
    private var connection: NativeMonitoringConnection {
        .init(server: PlatformExactID(endpoint), client: model.connected ? model.client.map(ObjectIdentifier.init) : nil)
    }

    var body: some View {
        Color.clear.frame(width: 0, height: 0).accessibilityHidden(true)
            .task(id: connection) {
                workspace.configure(endpoint, client: model.connected ? model.client : nil)
                guard !endpoint.isEmpty else { return }
                async let capabilities: Void = workspace.loadMonitoringCapabilities()
                await workspace.load()
                await capabilities
                await workspace.loadBandPlan()
            }
            .task(id: [PlatformExactID(endpoint), PlatformExactID(workspace.selected)]) {
                let server = endpoint, project = workspace.selected
                guard !server.isEmpty, !project.isEmpty else { return }
                await workspace.refreshStatus(syncConfiguration: true)
                await workspace.loadOperationsSettings()
                var tick = 0
                while !Task.isCancelled, workspace.matchesOperations(endpoint: server, projectID: project) {
                    await workspace.refreshStatus()
                    await workspace.pollSignalEvents()
                    await workspace.pollAlerts()
                    await workspace.refreshDuty()
                    if workspace.search["active"].bool == true || workspace.coverage == "restoring" {
                        await workspace.refreshSearch()
                    }
                    if tick % 6 == 0 { await workspace.refreshStorage() }
                    tick += 1
                    do { try await Task.sleep(for: .seconds(5)) } catch { return }
                }
            }
            .onChange(of: PlatformExactID(workspace.project["active_session_id"].text)) { _, id in
                if let next = sessionFollow.sessionToFollow(endpoint: endpoint, projectID: workspace.selected,
                                                           activeSessionID: id.rawValue), (!model.isStandaloneRFExplorer || workspace.includesSession(model.selectedSessionID)) {
                    model.selectSession(next)
                    Task { await model.reloadSessions() }
                }
            }
            .onDisappear { workspace.configure("", client: nil) }
    }
}
