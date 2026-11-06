import SwiftUI

struct ContentView: View {
    @StateObject private var receiver = ArModel()
    @StateObject private var analyzer = UltraModel()

    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Двойной обзор")
                    .font(.title.weight(.semibold))
                Text("Два прибора в одном окне. Цифру разбирает приёмник. Слева отсчёт S-метра, справа дБм.")
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            Divider()
            HSplitView {
                ArPane(model: receiver)
                    .frame(minWidth: 460)
                UltraPane(model: analyzer) { hertz in
                    let units = hertz >= 0 ? (hertz + 5) / 10 : 0
                    receiver.stopScanAndSetFrequency(units: units)
                }
                .frame(minWidth: 460)
            }
        }
        .frame(minWidth: 980, minHeight: 700)
        .background(WindowCloseObserver {
            receiver.disconnect()
            analyzer.disconnect()
        })
    }
}

// Observe this particular window; other WindowGroup windows keep their own sessions.
struct WindowCloseObserver: NSViewRepresentable {
    var onClose: () -> Void

    func makeNSView(context: Context) -> WindowCloseView {
        let view = WindowCloseView()
        view.onClose = onClose
        return view
    }

    func updateNSView(_ view: WindowCloseView, context: Context) {
        view.onClose = onClose
    }
}

final class WindowCloseView: NSView {
    var onClose: (() -> Void)?

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        NotificationCenter.default.removeObserver(self, name: NSWindow.willCloseNotification, object: nil)
        if let window {
            NotificationCenter.default.addObserver(self, selector: #selector(windowWillClose),
                name: NSWindow.willCloseNotification, object: window)
        }
    }

    @objc private func windowWillClose(_ notification: Notification) {
        onClose?()
    }

    deinit { NotificationCenter.default.removeObserver(self) }
}
