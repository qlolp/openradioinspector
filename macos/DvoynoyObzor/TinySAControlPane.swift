import SwiftUI
import WebKit
import SpektrKomandy
import AppKit

private struct NativeLabIdentity: Hashable {
    let server: PlatformExactID
    let session: PlatformExactID
    let stream: PlatformExactID
    let client: ObjectIdentifier?
    let access: NativeNodeAccess?
}

/// The full common control surface is embedded, not forked into a second set
/// of device commands. Every operation still goes through core /api/v1.
struct RemoteTinySAControlPane: View {
    let server: String
    let sessionID: String
    let streamID: String
    let client: PlatformClient?
    let access: NativeNodeAccess?
    @State private var cookie: HTTPCookie?
    @State private var prepared = false
    @State private var error: String?
    var body: some View {
        VStack(alignment: .leading) {
            Text("tinySA Ultra · управление общим ядром").font(.headline)
            if let context = try? PlatformTinySAControlContext(server: server, sessionID: sessionID, streamID: streamID),
               let url = try? context.url() {
                if prepared {
                    TinySAWebControl(url: url, cookie: cookie).id(PlatformExactID(url.absoluteString))
                } else if error == nil { ProgressView("Подготовка защищённой панели…") }
            } else {
                Text("Подключите ядро и выберите tinySA serial-поток.").foregroundStyle(.secondary)
            }
            ErrorLine(text: error)
        }.padding(12).frame(minWidth: 900, minHeight: 650)
            .task(id: NativeLabIdentity(server: PlatformExactID(server), session: PlatformExactID(sessionID), stream: PlatformExactID(streamID),
                                      client: client.map(ObjectIdentifier.init), access: access)) {
                prepared = false; error = nil; cookie = nil
                guard let client, let access else { error = "Подключите ядро и подтвердите доступ к нему."; return }
                do {
                    if access == .pairedTLS {
                        let session = try await client.browserSession()
                        guard !Task.isCancelled else { return }
                        cookie = try session.makeCookie()
                    }
                    guard !Task.isCancelled else { return }
                    prepared = true
                } catch {
                    guard !Task.isCancelled else { return }
                    self.error = "Защищённая браузерная сессия не подтверждена. Проверьте доступ к узлу и поддержку панели в ядре."
                }
            }
    }
}

struct TinySAWebControl: NSViewRepresentable {
    let url: URL
    let cookie: HTTPCookie?
    func makeCoordinator() -> Coordinator { Coordinator(origin: url) }
    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        context.coordinator.load(view, cookie: cookie)
        return view
    }
    func updateNSView(_ view: WKWebView, context: Context) {}
    static func dismantleNSView(_ view: WKWebView, coordinator: Coordinator) {
        coordinator.retire()
        // A disappearing native scope must end its heartbeat and release only
        // its own lease. Expiry covers a failed final HTTP request.
        view.evaluateJavaScript("window.OpenRadioTinySA?.retire()") { _, _ in
            view.stopLoading(); view.navigationDelegate = nil
        }
    }
    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKDownloadDelegate {
        let origin: URL
        private var retired = false
        private var loadTask: Task<Void, Never>?
        init(origin: URL) { self.origin = origin }
        func load(_ view: WKWebView, cookie: HTTPCookie?) {
            loadTask = Task { @MainActor [weak self, weak view] in
                guard let self, let view else { return }
                if let cookie { await view.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) }
                guard !Task.isCancelled, !self.retired else { return }
                // Program bytes are public. Authenticated fetch/SSE/WS use the
                // separate HttpOnly cookie, never a token in JavaScript or URL.
                view.load(URLRequest(url: self.origin))
            }
        }
        func retire() { retired = true; loadTask?.cancel(); loadTask = nil }
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
