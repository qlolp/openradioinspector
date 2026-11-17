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

    @AppStorage("native.workbench.tab") private var tab = "range"
    @State private var showReceiver = false

    var body: some View {
        VStack(spacing: 0) {
            if let evidence=NativeAcceptanceTrace.shared {
                Text("Приёмочный тест · "+evidence.profile.phase+" · "+evidence.profile.runID).font(.caption).foregroundStyle(.orange)
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
                Picker("Рабочая область", selection: $tab) {
                    Text("Приборы").tag("devices")
                    Text("Контроль диапазона").tag("range")
                    Text(workspace.signalQueue.unread.isEmpty ? "Сигналы" : "Сигналы (\(workspace.signalQueue.unread.count))").tag("signals")
                    Text("Архив").tag("archive")
                    Text("Охрана и исследование").tag("guard")
                }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 720)
                Spacer(minLength: 0)
                Button("История уровней") { model.showFrequencyHistory = true }.disabled(!model.connected || model.analyzer == nil)
                if tab == "range" {
                    Toggle("AR-DV1", isOn: $showReceiver).toggleStyle(.button).disabled(model.receiver == nil)
                }
            }.fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 10).padding(.vertical, 6).controlSize(.small)
            Divider()
            NativeOperatorHeader(model: model, setupExpanded: $setupExpanded)
            ZStack {
                devicesPage.workbenchVisible(tab == "devices")
                HSplitView {
                    RemoteUltraPane(model: model, research: true, scanBands: currentScanBands, bandPlanWorkspace: workspace)
                        .frame(minWidth: 640)
                    if showReceiver, model.receiver != nil {
                        RemoteArPane(model: model).frame(minWidth: 280, idealWidth: 300, maxWidth: 420)
                    }
                }.workbenchVisible(tab == "range")
                NativeSignalsPage(model: model, workspace: workspace, openSpectrum: { tab = "range" }, openGuard: { tab = "guard" })
                    .workbenchVisible(tab == "signals")
                NativeArchivePage(model: model, openSpectrum: { tab = "range" })
                    .workbenchVisible(tab == "archive")
                VStack {
                    Picker("Режим", selection: $workspaceMode) {
                        ForEach(NativeWorkspace.allCases) { Text($0.title).tag($0.rawValue) }
                    }.pickerStyle(.segmented).frame(maxWidth: 400)
                    MonitoringWorkspacePane(server: model.connected ? model.serverURL : "", client: model.connected ? model.client : nil,
                        sessionID: model.selectedSessionID, workspace: workspace, mode: mode,
                        onProjectSession: { id in model.selectSession(id); Task { await model.reloadSessions() } },
                        onResearch: { workspaceMode = NativeWorkspace.research.rawValue })
                    Spacer(minLength: 0)
                }.padding(8).workbenchVisible(tab == "guard")
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            NativeContinuityBar(model: model, workspace: workspace)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .frame(minWidth: 1000, minHeight: 700)
        .onChange(of: setupExpanded) { _, expanded in if expanded { tab = "devices"; setupExpanded = false } }
        .onChange(of: model.coreReachable) { _, reachable in if reachable, tab == "devices" { tab = "range" } }
        .onChange(of: model.reviewedSweepRequest) { _, _ in if model.reviewedSweep != nil { tab = "range" } }
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
            if evidence.profile.phase=="load" {
                tab="range";model.connect()
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
            if value == NativeWorkspace.research.rawValue { tab = "guard" }
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
                    Button("Подготовить приборы…") { showRCPreparation = true }.disabled(!model.connected)
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
