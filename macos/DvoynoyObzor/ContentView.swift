import SwiftUI
import SpektrKomandy
import AppKit

struct ContentView: View {
    @State private var model: RemoteDualModel
    @StateObject private var workspace: MonitoringWorkspaceModel
    @StateObject private var nodeConnection = NativeNodeConnectionModel()
    @StateObject private var localService = NativeLocalService()
    @State private var showRCPreparation = false
    @State private var setupExpanded = false
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
        if let evidence=NativeAcceptanceTrace.shared {remote.serverURL=evidence.profile.coreURL}
        else if NativeLocalService.isPersonalRC { remote.serverURL = NativeLocalService.endpoint }
        _model = State(initialValue: remote)
    }

    @AppStorage("native.workbench.tab") private var legacyTab = "range"
    @AppStorage("native.console.navigation.v1") private var savedNavigation = Data()
    @State private var navigation = NativeConsoleNavigation()
    @State private var restoredNavigation = false

    private var consoleContext: NativeConsoleContext {
        .init(server: model.serverURL, project: workspace.selected,
              session: model.selectedSessionID, stream: model.analyzer?.id ?? "")
    }

    var body: some View {
        VStack(spacing: 0) {
            if let evidence=NativeAcceptanceTrace.shared {
                Text((evidence.profile.phase == "ui-test" ? "UI-автотест · синтетические данные · " : "Приёмочный тест · "+evidence.profile.phase+" · ")+evidence.profile.runID)
                    .font(.caption).foregroundStyle(.orange).accessibilityIdentifier("engineering.scope")
            }
            HStack(spacing: 12) {
                Label {
                    Text("Двойной обзор")
                } icon: {
                    if let icon = NSImage(named: NSImage.applicationIconName) {
                        Image(nsImage: icon)
                            .resizable()
                            .scaledToFit()
                            .frame(width: 28, height: 28)
                            .accessibilityHidden(true)
                    } else {
                        Image(systemName: "waveform.path")
                    }
                }.font(.headline)
                Picker("Рабочая область", selection: Binding(get: { navigation.destination.page },
                    set: { navigation.destination.page = $0 })) {
                    ForEach(NativeConsolePage.allCases, id: \.self) { Text($0.title).tag($0) }
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 320)
                    .accessibilityIdentifier("workbench.navigation")
                Spacer(minLength: 0)
                Button("Настройки", systemImage: "gearshape") { navigation.destination.settings = .connection }
                    .accessibilityIdentifier("workbench.settings")
            }.fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 10).padding(.vertical, 6).controlSize(.small)
            Divider()
            NativeOperatorHeader(model: model, setupExpanded: $setupExpanded, workspace: mode,
                                 openWorkspace: { navigation.destination.settings = .guardSettings })
            NativeObservationActions(model: model, workspace: workspace,
                prepare: { showRCPreparation = true },
                settings: { navigation.destination.settings = $0 })
            Group {
                switch navigation.destination.page {
                case .observation:
                    HSplitView {
                        RemoteUltraPane(model: model, research: true, scanBands: currentScanBands,
                                        bandPlanWorkspace: workspace, compact: true)
                            .frame(minWidth: 620, maxWidth: .infinity, maxHeight: .infinity)
                        NativeGuardSignalsPane(workspace: workspace,
                            openGuard: { navigation.destination.settings = .guardSettings }, compact: true,
                            openOriginal: { sweep in
                                model.reviewedSweep = sweep
                                model.reviewedSweepRequest = UUID()
                            }, openReceiver: { navigation.destination.settings = .receiver })
                            .frame(minWidth: 300, idealWidth: 350, maxWidth: 540, maxHeight: .infinity)
                            .accessibilityIdentifier("observation.events")
                    }
                case .archive:
                    NativeArchivePage(model: model, openSpectrum: { navigation.destination.page = .observation })
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            NativeContinuityBar(model: model, workspace: workspace)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .frame(minWidth: 1000, minHeight: 700)
        .focusedSceneValue(\.nativeConsoleActions, NativeConsoleActions(
            observe: { navigation.destination.page = .observation },
            archive: { navigation.destination.page = .archive },
            settings: { navigation.destination.settings = .connection },
            prepare: { if model.connected { showRCPreparation = true } else { navigation.destination.settings = .connection } }))
        .background(NativeMonitoringLifecycle(model: model, workspace: workspace))
        .onChange(of: consoleContext, initial: true) { _, context in
            if !restoredNavigation {
                navigation = (try? JSONDecoder().decode(NativeConsoleNavigation.self, from: savedNavigation)) ?? NativeConsoleNavigation()
                navigation.destination.settings = nil
                restoredNavigation = true
            }
            navigation.activate(context, legacyTab: legacyTab)
        }
        .onChange(of: navigation) { _, value in
            var stored = value
            stored.destination.settings = nil
            if let data = try? JSONEncoder().encode(stored) { savedNavigation = data }
        }
        .onChange(of: setupExpanded) { _, expanded in
            if expanded { navigation.destination.settings = .connection; setupExpanded = false }
        }
        .sheet(item: Binding(get: { navigation.destination.settings }, set: { navigation.destination.settings = $0 })) { section in
            NativeConsoleSettingsPane(model: model, workspace: workspace, workspaceMode: $workspaceMode,
                                      initialSection: section) { devicesPage }
        }
        .onChange(of: model.reviewedSweepRequest) { _, _ in if model.reviewedSweep != nil { navigation.destination.page = .observation } }
        .sheet(isPresented: $model.showFrequencyHistory) { NativeFrequencyHistoryPane(model:model) }
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
        .task {
            guard let evidence=NativeAcceptanceTrace.shared else{return}
            NSApp.activate(ignoringOtherApps:true)
            if evidence.profile.phase=="load" || evidence.profile.phase=="ui-test" {
                navigation.destination.page = .observation; model.connect()
                await model.reloadSessions()
                model.selectSession(evidence.profile.warmupSessionID ?? evidence.profile.sessionID)
            }
            let root=URL(fileURLWithPath:evidence.profile.outputDirectory)
            var switched=false
            var serviceStopHandled=false
            while !Task.isCancelled {
                if evidence.profile.phase=="hardware",!serviceStopHandled,
                   let value=try? String(contentsOf:root.appendingPathComponent("service-stop.request"),encoding:.utf8),
                   value.trimmingCharacters(in:.whitespacesAndNewlines)==evidence.profile.runID {
                    serviceStopHandled=true
                    await NativeAcceptanceServiceCleanup.finish(service:localService){successful,message in
                        evidence.serviceStopped(successful:successful,message:message)
                    }
                    if localService.phase == .stopped {NSApp.terminate(nil);return}
                }
                if !switched,let value=try? String(contentsOf:root.appendingPathComponent("use-workload.request"),encoding:.utf8),
                   value.trimmingCharacters(in:.whitespacesAndNewlines)==evidence.profile.runID {
                    switched=true;model.selectSession(evidence.profile.sessionID)
                    await model.reloadSessions()
                }
                if let value=try? String(contentsOf:root.appendingPathComponent("ui-stop.request"),encoding:.utf8),
                   value.trimmingCharacters(in:.whitespacesAndNewlines)==evidence.profile.runID {
                    NSApp.terminate(nil);return
                }
                do {try await Task.sleep(for:.milliseconds(100))}catch{return}
            }
        }
        .onChange(of: workspaceMode) { _, value in
            NativeAcceptanceTrace.shared?.record("workspace.changed", ["value": NativeWorkspace(rawValue: value)?.rawValue ?? "unknown"])
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

    private var currentScanBands: [MonitoringValue] {
        let project = workspace.project
        guard !model.selectedSessionID.isEmpty,
              PlatformExactID.same(project["active_session_id"].text, model.selectedSessionID) else { return [] }
        return project["scan_plan"]["config"]["bands"].items
    }

    private var devicesPage: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if NativeLocalService.isPersonalRC {
                    NativeLocalServicePane(service: localService, connect: connectLocalService,
                        disconnect: { nodeConnection.cancel(); model.disconnect() })
                    NativeConnectionPane(model: model, connection: nodeConnection, localServiceRequired: true,
                        localServiceReady: localService.status != nil, onLocalConnect: connectLocalService)
                } else {
                    NativeDevelopmentCorePane(core: developmentCore, model: model, connection: nodeConnection,
                        connectionTask: $developmentConnectionTask, windowClosed: $windowClosed)
                    NativeConnectionPane(model: model, connection: nodeConnection)
                }
                if model.receiver != nil { RemoteArPane(model: model).frame(minHeight: 320) }
            }.padding(12)
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

extension View {
    /// Keep presentation and project polling alive while changing workspace tabs.
    func workbenchVisible(_ visible: Bool) -> some View {
        self.opacity(visible ? 1 : 0).allowsHitTesting(visible).accessibilityHidden(!visible)
            .frame(maxWidth: visible ? .infinity : 0, maxHeight: visible ? .infinity : 0).clipped()
    }
}
