import SwiftUI

struct ContentView: View {
    @StateObject private var model: RemoteDualModel
    @StateObject private var workspace: MonitoringWorkspaceModel
    @StateObject private var nodeConnection = NativeNodeConnectionModel()
    @StateObject private var localService = NativeLocalService()
    @State private var showRCPreparation = false
    @State private var setupExpanded = true
    @State private var archiveExpanded = false
    @State private var operationsExpanded = false
    @State private var developmentCore = NativeDevelopmentCore()
    @State private var developmentConnectionTask: Task<Void, Never>?
    @State private var windowClosed = false
    @AppStorage("native.workspace") private var workspaceMode = NativeWorkspace.monitoring.rawValue
    private var mode: NativeWorkspace { NativeWorkspace(rawValue: workspaceMode) ?? .monitoring }

    init() {
        let workspace = MonitoringWorkspaceModel()
        _workspace = StateObject(wrappedValue: workspace)
        let remote = RemoteDualModel(requireRuntimeOwner: NativeLocalService.isPersonalRC, onArchiveContextReset: { [weak workspace] watermark in
            workspace?.resetArchiveContext(watermark: watermark)
        })
        if NativeLocalService.isPersonalRC { remote.serverURL = NativeLocalService.endpoint }
        _model = StateObject(wrappedValue: remote)
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
                NativeOperatorHeader(model: model, setupExpanded: $setupExpanded)
                if setupExpanded {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 8) {
                            if NativeLocalService.isPersonalRC {
                                NativeLocalServicePane(service: localService, connect: {
                                    connectLocalService()
                                }, disconnect: { nodeConnection.cancel(); model.disconnect() })
                            }
                            if NativeLocalService.isPersonalRC {
                                HStack {
                                    Button("Подготовить приборы…") { showRCPreparation = true }
                                        .disabled(!model.connected)
                                    DisclosureGroup("Сеансы и дополнительные действия") {
                                        NativeConnectionPane(model: model, connection: nodeConnection, localServiceRequired: true, localServiceReady: localService.status != nil, onLocalConnect: connectLocalService)
                                    }
                                }.padding(.horizontal, 12)
                            } else {
                                NativeDevelopmentCorePane(core: developmentCore, model: model, connection: nodeConnection,
                                    connectionTask: $developmentConnectionTask, windowClosed: $windowClosed)
                                NativeConnectionPane(model: model, connection: nodeConnection)
                            }
                        }
                    }.frame(maxHeight: 240)
                        .padding(.horizontal, 16).padding(.bottom, 8)
                }
                NativeContinuityBar(model: model, workspace: workspace)
                DisclosureGroup("Хранилище и диагностика записи", isExpanded: $archiveExpanded) {
                    RemoteArchiveCapacityPane(model: model)
                }.font(.caption).padding(.horizontal, 16).padding(.vertical, 6)
                VStack(spacing: 0) {
                    HSplitView {
                        RemoteUltraPane(model: model, research: mode == .research).frame(minWidth: 460)
                        if model.receiver != nil {
                            RemoteArPane(model: model).frame(minWidth: 300, idealWidth: 340, maxWidth: 480)
                        }
                    }.frame(minHeight: 380, maxHeight: .infinity).layoutPriority(1)
                    VStack(spacing: 8) {
                        HStack {
                            Button {
                                operationsExpanded.toggle()
                            } label: {
                                Label("События и охрана", systemImage: operationsExpanded ? "chevron.down" : "chevron.right")
                            }.buttonStyle(.plain).font(.headline)
                            Text("\(workspace.events.count) в списке").font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Text("Проект · события · решения оператора").font(.caption).foregroundStyle(.secondary)
                        }.padding(.horizontal, 16).padding(.top, 6)
                        ScrollView {
                            VStack(spacing: 8) {
                                // Keep one workspace identity while its drawer is collapsed.
                                MonitoringWorkspacePane(server: model.connected ? model.serverURL : "", client: model.connected ? model.client : nil, sessionID: model.selectedSessionID,
                                    workspace: workspace, mode: mode, onProjectSession: { id in
                                        model.selectSession(id)
                                        Task { await model.reloadSessions() }
                                    }, onResearch: { workspaceMode = NativeWorkspace.research.rawValue })
                                if mode == .research {
                                    DisclosureGroup("Задания и автоматический анализ") {
                                        RemoteWorkflowPane(model: model).frame(minHeight: 280, maxHeight: 400)
                                    }.padding(.horizontal)
                                }
                            }
                        }
                        .frame(height: operationsExpanded ? 220 : 0)
                        .clipped().disabled(!operationsExpanded).allowsHitTesting(operationsExpanded).accessibilityHidden(!operationsExpanded)
                    }.frame(minHeight: operationsExpanded ? 200 : 32, idealHeight: operationsExpanded ? 260 : 32,
                            maxHeight: operationsExpanded ? nil : 32)
                }
            }
            .background(Color.primary.opacity(0.025))
            .navigationTitle(mode.title)
        }
        .frame(minWidth: 960, minHeight: 740)
        .sheet(isPresented: $showRCPreparation) { NativeRCPreparationPane(model: model, workspace: workspace) }
        .sheet(isPresented: $model.showSerialForm) { RemoteSerialSessionPane(model: model) }
        .sheet(isPresented: $model.showTinySAControl) {
            RemoteTinySAControlPane(server: model.serverURL, sessionID: model.selectedSessionID, streamID: model.analyzer?.id ?? "",
                client: model.client, access: nodeConnection.authorizedNode?.access)
        }
        .background(WindowCloseObserver {
            windowClosed = true
            developmentConnectionTask?.cancel()
            developmentConnectionTask = nil
            nodeConnection.cancel()
            model.disconnect()
            // Retain the owner until exit is confirmed, even if the first stop times out.
            let core = developmentCore
            Task {
                while core.ownsProcess || core.busy {
                    await core.stop()
                    if core.ownsProcess { try? await Task.sleep(for: .seconds(1)) }
                }
            }
        })
        .onChange(of: workspaceMode) { _, value in
            if value == NativeWorkspace.research.rawValue { operationsExpanded = true }
        }
        .onChange(of: model.coreReachable) { _, reachable in
            if reachable { setupExpanded = false }
        }
        .onChange(of: model.serverURL) { _, _ in cancelSupersededDevelopmentStartup() }
        .onChange(of: model.connected) { _, _ in cancelSupersededDevelopmentStartup() }
        .onChange(of: nodeConnection.phase) { _, _ in cancelSupersededDevelopmentStartup() }
        .onChange(of: localService.status?.instanceID) { _, owner in
            guard NativeLocalService.isPersonalRC else { return }
            nodeConnection.cancel()
            model.observeVerifiedRuntimeOwner(owner)
            if owner != nil { connectLocalService() }
        }
    }

    private func cancelSupersededDevelopmentStartup() {
        // Changes made by another connection control supersede an outstanding launch.
        // Once ready, the pane's preflight checks protect its own connection handoff.
        if developmentCore.phase == .starting { developmentConnectionTask?.cancel() }
    }

    private func connectLocalService() {
        guard let owner = localService.status?.instanceID else { return }
        model.serverURL = NativeLocalService.endpoint
        Task {
            if let node = await nodeConnection.preflight(server: model.serverURL, runtimeInstanceID: owner),
               localService.status?.instanceID == owner, nodeConnection.accepts(node) {
                model.connect(using: node.client)
            }
        }
    }
}
