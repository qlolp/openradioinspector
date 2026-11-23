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
    @AppStorage("native.workspace") private var legacyWorkspaceMode = NativeWorkspace.monitoring.rawValue
    private var workspaceMode: String {
        navigation.destination.research == true ? NativeWorkspace.research.rawValue : NativeWorkspace.monitoring.rawValue
    }
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
    @State private var sourceSheet: PlatformSweep?
    @State private var prepareAfterSettings = false

    private var showGuard: Bool { !model.isStandaloneRFExplorer || workspace.includesSession(model.selectedSessionID) }

    private var consoleContext: NativeConsoleContext {
        .init(server: model.client?.baseURL.root.absoluteString ?? "", project: workspace.selected,
              session: model.selectedSessionID, stream: model.analyzer?.id ?? model.receiver?.id ?? "")
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
            if showGuard {
                NativeObservationActions(model: model, workspace: workspace, prepare: presentPreparation,
                    settings: { navigation.destination.settings = $0 })
            } else {
                Button("Подготовить дежурство…", action: presentPreparation).disabled(!model.connected)
                    .accessibilityIdentifier("preparation.open")
            }
            Group {
                switch navigation.destination.page {
                case .observation:
                    HSplitView {
                        if let receiver = model.receiver, receiver.isCyclicARDV1, let client = model.client {
                            NativeReceiverPanoramaPane(model: model, workspace: workspace, client: client,
                                stream: receiver, session: model.selectedSessionID)
                                .id([PlatformExactID(model.selectedSessionID), PlatformExactID(receiver.id), PlatformExactID(String(model.connectionGeneration))])
                                .frame(minWidth: 520, maxWidth: .infinity, maxHeight: .infinity)
                        } else {
                            RemoteUltraPane(model: model, research: true, scanBands: currentScanBands,
                                            bandPlanWorkspace: showGuard ? workspace : nil, compact: true)
                                .frame(minWidth: 620, maxWidth: .infinity, maxHeight: .infinity)
                        }
                        if showGuard {
                            NativeGuardSignalsPane(workspace: workspace,
                                openGuard: { navigation.destination.settings = .guardSettings }, compact: true, research: mode == .research,
                                openReceiver: { navigation.destination.settings = .receiver }, remote: model)
                                .frame(minWidth: 300, idealWidth: 350, maxWidth: 540, maxHeight: .infinity)
                                .accessibilityElement(children: .contain)
                                .accessibilityIdentifier("observation.events")
                        }
                    }
                case .archive:
                    if let receiver = model.receiver, receiver.isCyclicARDV1, let client = model.client {
                        NativeReceiverPanoramaPane(model: model, workspace: workspace, client: client, stream: receiver,
                            session: model.selectedSessionID)
                            .id([PlatformExactID(model.selectedSessionID), PlatformExactID(receiver.id), PlatformExactID(String(model.connectionGeneration))])
                    } else { NativeArchivePage(model: model, openSpectrum: { navigation.destination.page = .observation }) }
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
            prepare: presentPreparation))
        .background(NativeMonitoringLifecycle(model: model, workspace: workspace))
        .task {
            await NativeConsoleServiceWatch.run(enabled: NativeLocalService.isPersonalRC,
                                               inspect: { await localService.inspect() })
        }
        .onChange(of: consoleContext, initial: true) { _, context in
            let migrateMode = !restoredNavigation && savedNavigation.isEmpty
            if !restoredNavigation {
                navigation = (try? JSONDecoder().decode(NativeConsoleNavigation.self, from: savedNavigation)) ?? NativeConsoleNavigation()
                navigation.destination.settings = nil
                restoredNavigation = true
            }
            navigation.activate(context, legacyTab: legacyTab)
            if migrateMode { navigation.destination.research = legacyWorkspaceMode == NativeWorkspace.research.rawValue }
        }
        .onChange(of: navigation) { _, value in
            var stored = value
            stored.destination.settings = nil
            if let data = try? JSONEncoder().encode(stored) { savedNavigation = data }
        }
        .onChange(of: setupExpanded) { _, expanded in
            if expanded { navigation.destination.settings = .connection; setupExpanded = false }
        }
        .sheet(item: Binding(get: { navigation.destination.settings }, set: { navigation.destination.settings = $0 }),
               onDismiss: {
                   if prepareAfterSettings { prepareAfterSettings = false; showRCPreparation = model.connected }
               }) { section in
            NativeConsoleSettingsPane(model: model, workspace: workspace, workspaceMode: Binding(
                get: { workspaceMode },
                set: { navigation.destination.research = $0 == NativeWorkspace.research.rawValue }),
                                      initialSection: section, access: nodeConnection.authorizedNode?.access) { devicesPage }
        }
        .onChange(of: workspace.signalOriginal) { _, sweep in
            guard navigation.destination.settings == nil, let sweep else { return }
            if PlatformExactID.same(sweep.sessionId, model.selectedSessionID),
               PlatformExactID.same(sweep.streamId, model.analyzer?.id ?? "") {
                model.reviewedSweep = sweep; model.reviewedSweepRequest = UUID()
            } else { sourceSheet = sweep }
            workspace.clearSignalOriginal()
        }
        .sheet(item: $sourceSheet) { NativeGuardOriginalPane(sweep: $0) }
        .onChange(of: model.reviewedSweepRequest) { _, _ in if model.reviewedSweep != nil { navigation.destination.page = .observation } }
        .sheet(isPresented: Binding(get: { navigation.destination.settings == nil && model.showFrequencyHistory }, set: { model.showFrequencyHistory = $0 })) { NativeFrequencyHistoryPane(model:model) }
        .sheet(isPresented: $showRCPreparation) { NativeRCPreparationPane(model: model, workspace: workspace) }
        .sheet(isPresented: Binding(get: { navigation.destination.settings == nil && model.showSerialForm }, set: { model.showSerialForm = $0 })) { RemoteSerialSessionPane(model: model) }
        .sheet(isPresented: Binding(get: { navigation.destination.settings == nil && model.showTinySAControl }, set: { model.showTinySAControl = $0 })) {
            if let analyzer = model.tinySAAnalyzer {
                RemoteTinySAControlPane(server: model.serverURL, sessionID: model.selectedSessionID, streamID: analyzer.id,
                    client: model.client, access: nodeConnection.authorizedNode?.access)
            }
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
            if evidence.profile.phase == "ui-test",
               let request = try? String(contentsOf: root.appendingPathComponent("training-window.request"), encoding: .utf8),
               request == evidence.profile.runID {
                await NativeTrainingWindowCapture.run(profile: evidence.profile, model: model, workspace: workspace)
                NSApp.terminate(nil)
                return
            }
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

    private func presentPreparation() {
        if !model.connected {
            prepareAfterSettings = true; navigation.destination.settings = .connection
        } else if navigation.destination.settings != nil {
            prepareAfterSettings = true; navigation.destination.settings = nil
        } else { showRCPreparation = true }
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
