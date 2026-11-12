import SwiftUI

struct ContentView: View {
    @StateObject private var model: RemoteDualModel
    @StateObject private var workspace: MonitoringWorkspaceModel
    @StateObject private var nodeConnection = NativeNodeConnectionModel()
    @AppStorage("native.workspace") private var workspaceMode = NativeWorkspace.monitoring.rawValue
    private var mode: NativeWorkspace { NativeWorkspace(rawValue: workspaceMode) ?? .monitoring }

    init() {
        let workspace = MonitoringWorkspaceModel()
        _workspace = StateObject(wrappedValue: workspace)
        _model = StateObject(wrappedValue: RemoteDualModel(onArchiveContextReset: { [weak workspace] watermark in
            workspace?.resetArchiveContext(watermark: watermark)
        }))
    }

    var body: some View {
        NavigationSplitView {
            List(NativeWorkspace.allCases, selection: Binding<NativeWorkspace?>(
                get: { mode }, set: { if let value = $0 { workspaceMode = value.rawValue } }
            )) { value in
                Label(value.title, systemImage: value.symbol).tag(value)
            }
            .navigationTitle("Рабочее пространство")
            .navigationSplitViewColumnWidth(min: 175, ideal: 205, max: 260)
        } detail: {
            VStack(spacing: 0) {
                NativeConnectionPane(model: model, connection: nodeConnection)
                NativeContinuityBar(model: model, workspace: workspace)
                RemoteArchiveCapacityPane(model: model).padding(.horizontal, 16)
                VSplitView {
                    HSplitView {
                        RemoteUltraPane(model: model, research: mode == .research).frame(minWidth: 460)
                        RemoteArPane(model: model).frame(minWidth: 340, idealWidth: 380, maxWidth: 560)
                    }.frame(minHeight: 400)
                    VStack(spacing: 8) {
                        // One model and one view identity survive local workspace changes.
                        MonitoringWorkspacePane(server: model.connected ? model.serverURL : "", client: model.connected ? model.client : nil, sessionID: model.selectedSessionID,
                            workspace: workspace, mode: mode, onProjectSession: { id in
                                model.selectSession(id)
                                Task { await model.reloadSessions() }
                            }, onResearch: { workspaceMode = NativeWorkspace.research.rawValue })
                        if mode == .research {
                            DisclosureGroup("Задания и автоматический анализ") { RemoteWorkflowPane(model: model).frame(minHeight: 280, maxHeight: 400) }
                                .padding(.horizontal)
                        }
                    }.frame(minHeight: 220, idealHeight: 360)
                }
            }
            .navigationTitle(mode.title)
        }
        .frame(minWidth: 1150, minHeight: 850)
        .sheet(isPresented: $model.showSerialForm) { RemoteSerialSessionPane(model: model) }
        .sheet(isPresented: $model.showTinySAControl) {
            RemoteTinySAControlPane(server: model.serverURL, sessionID: model.selectedSessionID, streamID: model.analyzer?.id ?? "",
                client: model.client, access: nodeConnection.authorizedNode?.access)
        }
        .background(WindowCloseObserver { nodeConnection.cancel(); model.disconnect() })
    }
}
