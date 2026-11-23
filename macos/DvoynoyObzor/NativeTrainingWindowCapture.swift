import AppKit
import ScreenCaptureKit
import SpektrKomandy

/// Explicit training-fixture capture. ScreenCaptureKit's current-process list
/// contains only windows available without requesting access to the desktop.
@MainActor
enum NativeTrainingWindowCapture {
    static func run(profile: NativeAcceptanceTrace.Profile, model: RemoteDualModel, workspace: MonitoringWorkspaceModel,
                    presentDuty: (NativeRCPreparationDraft) -> Void) async {
        let output = URL(fileURLWithPath: profile.outputDirectory, isDirectory: true)
        var receipt: [String: Any] = ["run_id": profile.runID, "successful": false, "session_id": profile.sessionID]
        do {
            guard #available(macOS 14.4, *), profile.phase == "ui-test", profile.sessionID == "ui-one" else {
                throw PlatformError.invalidCommand("Для учебного снимка нужна macOS 14.4 и учебный сеанс")
            }
            let event = PlatformExactID("ui-event-one")
            try await wait {
                model.coreReachable && model.selectedSessionID == "ui-one" && model.session?.name == "Учебный сеанс 1" &&
                    model.analyzer?.id == "ui-spectrum-one" && model.receiver?.id == "ui-receiver-one" &&
                    workspace.selected == "ui-project-one" && workspace.project["name"].text == "Учебное наблюдение 1" &&
                    workspace.projectStatusIsCurrent() && workspace.signalQueue.rows.contains { $0.id == event } &&
                    !model.displayInspectorSweeps.isEmpty && model.displayInspectorSweeps.allSatisfy {
                        $0.sessionId == "ui-one" && $0.streamId == "ui-spectrum-one" &&
                            $0.settings?["adapter"] == .string("synthetic-ui-fixture") && $0.settings?["simulated"] == .boolean(true)
                    }
            }
            workspace.selectSignalEvent(nil)
            try await capture(name: "macos-monitoring", directory: output)
            workspace.selectSignalEvent(event)
            await workspace.loadSignalEvent(event)
            try await wait {
                let detail = NativeGuardSignal(value: workspace.signalEvent)
                return detail.id == event && detail.source?.sweepID.rawValue == "ui-one-sweep-1" && detail.receiverSimulated
            }
            try await capture(name: "macos-event", directory: output)
            guard let parent = NSApp.mainWindow else { throw PlatformError.connection("Окно учебного сеанса закрыто") }
            var draft = NativeRCPreparationDraft()
            draft.source = .ardv1
            draft.name = "Учебное дежурство AR-DV1"; draft.site = "Учебный объект"
            draft.receiverPort = "/dev/cu.ui-fixture-receiver"
            draft.startHz = "100000000"; draft.stopHz = "400000000"
            presentDuty(draft)
            try await wait { parent.attachedSheet?.isVisible == true }
            guard let sheet = parent.attachedSheet, sheet.sheetParent === parent else {
                throw PlatformError.connection("Не открыта подготовка учебного дежурства")
            }
            try await capture(name: "macos-first-duty-ardv1", directory: output, sheet: sheet)
            receipt["first_duty"] = "macos-first-duty-ardv1.png"
            receipt["event_id"] = event.rawValue
            receipt["sweep_id"] = "ui-one-sweep-1"
            receipt["successful"] = true
        } catch { receipt["error"] = error.localizedDescription }
        if let bytes = try? JSONSerialization.data(withJSONObject: receipt) {
            try? bytes.write(to: output.appendingPathComponent("training-windows.json"), options: .withoutOverwriting)
        }
    }

    private static func wait(_ ready: () -> Bool) async throws {
        let clock = ContinuousClock(), deadline = clock.now.advanced(by: .seconds(25))
        while !ready() {
            try Task.checkCancellation()
            guard clock.now < deadline else { throw PlatformError.connection("Истёк срок проверки учебного сеанса и исходников") }
            try await Task.sleep(for: .milliseconds(150))
        }
    }

    @available(macOS 14.4, *)
    private static func capture(name: String, directory: URL, sheet: NSWindow? = nil) async throws {
        guard let window = sheet ?? NSApp.mainWindow, window.isVisible, !window.isMiniaturized, window.attachedSheet == nil,
              window.frame.width >= (sheet == nil ? 920 : 700), window.frame.height >= 580 else {
            throw PlatformError.connection("Окно учебного сеанса недоступно")
        }
        // Allow SwiftUI and the spectrum renderer to present the current selection.
        window.displayIfNeeded()
        try await Task.sleep(for: .milliseconds(350))
        let content = try await SCShareableContent.currentProcess
        guard let target = content.windows.first(where: {
            $0.windowID == CGWindowID(window.windowNumber) && $0.owningApplication?.processID == getpid()
        }) else { throw PlatformError.connection("ScreenCaptureKit не нашёл окно этого процесса") }
        let filter = SCContentFilter(desktopIndependentWindow: target)
        let configuration = SCStreamConfiguration()
        configuration.showsCursor = false
        configuration.ignoreShadowsSingleWindow = true
        configuration.width = Int(target.frame.width * window.backingScaleFactor)
        configuration.height = Int(target.frame.height * window.backingScaleFactor)
        let frame = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
        guard let data = NSBitmapImageRep(cgImage: frame).representation(using: .png, properties: [:]) else {
            throw PlatformError.invalidResponse("Не удалось сохранить пиксели учебного окна")
        }
        try data.write(to: directory.appendingPathComponent(name + ".png"), options: .withoutOverwriting)
    }
}
