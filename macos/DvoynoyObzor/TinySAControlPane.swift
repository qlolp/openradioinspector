import SwiftUI
import WebKit
import SpektrKomandy
import AppKit

/// The full common control surface is embedded, not forked into a second set
/// of device commands. Every operation still goes through core /api/v1.
struct RemoteTinySAControlPane: View {
    let server: String
    let sessionID: String
    let streamID: String
    var body: some View {
        VStack(alignment: .leading) {
            Text("tinySA Ultra · управление общим ядром").font(.headline)
            if let context = try? PlatformTinySAControlContext(server: server, sessionID: sessionID, streamID: streamID),
               let url = try? context.url() {
                TinySAWebControl(url: url).id(url.absoluteString)
            } else {
                Text("Подключите ядро и выберите tinySA serial-поток.").foregroundStyle(.secondary)
            }
        }.padding(12).frame(minWidth: 900, minHeight: 650)
    }
}

struct TinySAWebControl: NSViewRepresentable {
    let url: URL
    func makeCoordinator() -> Coordinator { Coordinator(origin: url) }
    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.load(URLRequest(url: url))
        return view
    }
    func updateNSView(_ view: WKWebView, context: Context) {}
    static func dismantleNSView(_ view: WKWebView, coordinator: Coordinator) {
        // A disappearing native scope must end its heartbeat and release only
        // its own lease. Expiry covers a failed final HTTP request.
        view.evaluateJavaScript("window.OpenRadioTinySA?.retire()") { _, _ in
            view.stopLoading(); view.navigationDelegate = nil
        }
    }
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKDownloadDelegate {
        let origin: URL
        init(origin: URL) { self.origin = origin }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
            guard let target = action.request.url,
                  target.scheme == origin.scheme, target.host == origin.host,
                  target.port == origin.port else { decisionHandler(.cancel); return }
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        }
        func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                     decisionHandler: @escaping @MainActor @Sendable (WKNavigationResponsePolicy) -> Void) {
            guard let target = response.response.url,
                  target.scheme == origin.scheme, target.host == origin.host,
                  target.port == origin.port else { decisionHandler(.cancel); return }
            decisionHandler(response.canShowMIMEType ? .allow : .download)
        }
        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
            download.delegate = self
        }
        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
            download.delegate = self
        }
        func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                      suggestedFilename: String, completionHandler: @escaping @MainActor @Sendable (URL?) -> Void) {
            let panel = NSSavePanel()
            panel.nameFieldStringValue = URL(fileURLWithPath: suggestedFilename).lastPathComponent
            panel.begin { result in completionHandler(result == .OK ? panel.url : nil) }
        }
    }
}
