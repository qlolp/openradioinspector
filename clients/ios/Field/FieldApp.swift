import Foundation
import SpektrKomandy
import SwiftUI
import UniformTypeIdentifiers

extension UTType {
    static let oriField = UTType(exportedAs: "org.openradioinspector.orifield", conformingTo: .zip)
}

@main
struct FieldApp: App {
    var body: some Scene { WindowGroup { FieldHome() } }
}

/// Keep the Files provider's read lease until the last view releases its document.
final class FieldFile: Sendable {
    let document: PlatformFieldDocument
    private let url: URL
    private let scoped: Bool

    init(url: URL) throws {
        guard url.isFileURL, url.pathExtension.lowercased() == "orifield" else {
            throw CocoaError(.fileReadUnsupportedScheme)
        }
        self.url = url
        let access = url.startAccessingSecurityScopedResource()
        self.scoped = access
        do {
            var coordinationError: NSError?
            var result: Result<PlatformFieldDocument, Error>?
            NSFileCoordinator().coordinate(readingItemAt: url, options: .withoutChanges, error: &coordinationError) { source in
                result = Result { try PlatformFieldDocument(opening: source) }
            }
            if let coordinationError { throw coordinationError }
            guard let result else { throw CocoaError(.fileReadUnknown) }
            document = try result.get()
        } catch {
            if access { url.stopAccessingSecurityScopedResource() }
            throw error
        }
    }

    deinit { if scoped { url.stopAccessingSecurityScopedResource() } }
}

private struct ArchiveRequest: Identifiable {
    let id = UUID()
    let url: URL
    let demo: Bool
}

struct FieldHome: View {
    @State private var tab = 0
    @State private var choosing = false
    @State private var request: ArchiveRequest?
    @State private var file: FieldFile?
    @State private var error: String?
    @State private var path: [FieldRecordKey] = []

    var body: some View {
        TabView(selection: $tab) {
            NavigationStack(path: $path) {
                Group {
                    if let file {
                        ArchiveList(document: file.document, demo: request?.demo == true)
                    } else if request != nil && error == nil {
                        ProgressView("Проверка полевого архива…")
                    } else {
                        List {
                            Section {
                                Button("Открыть из Файлов", systemImage: "folder") { choosing = true }
                                Button("Открыть учебный выезд", systemImage: "waveform.path.ecg") {
                                    if let url = Bundle.main.url(forResource: "field-archive-v1", withExtension: "orifield") {
                                        open(url, demo: true)
                                    }
                                }.accessibilityIdentifier("open-demo")
                            } footer: {
                                Text("Учебный выезд содержит синтетические измерения, заметку и точку GPS. Доступ к сети не требуется.")
                            }
                            if let error { Text(error).foregroundStyle(.red) }
                        }
                    }
                }
                .navigationTitle("Полевой архив")
                .toolbar {
                    if request != nil {
                        ToolbarItem(placement: .topBarTrailing) {
                            Button("Закрыть") { request = nil; path = []; file = nil; error = nil }
                        }
                    }
                }
                .navigationDestination(for: FieldRecordKey.self) { key in
                    if let file { ArchiveRecord(file: file, key: key) }
                }
            }
            .tabItem { Label("Архив", systemImage: "archivebox") }.tag(0)

            NodeView(isVisible: tab == 1)
                .tabItem { Label("Узел", systemImage: "network") }.tag(1)
        }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.oriField]) { result in
            do { open(try result.get(), demo: false) }
            catch { if (error as NSError).code != NSUserCancelledError { self.error = error.localizedDescription } }
        }
        .onOpenURL { open($0, demo: false) }
        .task(id: request?.id) {
            guard let requested = request else { return }
            let loading = Task.detached(priority: .userInitiated) { try FieldFile(url: requested.url) }
            do {
                let verified = try await loading.value
                try Task.checkCancellation()
                file = verified
            } catch {
                if !Task.isCancelled { self.error = error.localizedDescription }
            }
        }
    }

    private func open(_ url: URL, demo: Bool) {
        tab = 0
        path = []
        file = nil
        error = nil
        request = ArchiveRequest(url: url, demo: demo)
    }
}
