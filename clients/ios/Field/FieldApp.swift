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

/// Foundation allows cancellation of a pending coordination from another thread.
private final class FieldReadCoordinator: @unchecked Sendable {
    let coordinator = NSFileCoordinator()
    private let lock = NSLock()
    private var cancelled = false

    func cancel() {
        lock.withLock { cancelled = true }
        coordinator.cancel()
    }

    func checkCancellation() throws {
        if lock.withLock({ cancelled }) { throw CancellationError() }
        try Task.checkCancellation()
    }
}

private final class FieldFileAccess: Sendable {
    private let url: URL
    private let scoped: Bool

    init(_ url: URL) {
        self.url = url
        scoped = url.startAccessingSecurityScopedResource()
    }

    deinit { if scoped { url.stopAccessingSecurityScopedResource() } }
}

final class FieldFile: Sendable {
    let document: PlatformFieldDocument
    private let access: FieldFileAccess

    static func open(_ url: URL) async throws -> FieldFile {
        let read = FieldReadCoordinator()
        return try await withTaskCancellationHandler {
            try await withThrowingTaskGroup(of: FieldFile.self) { group in
                group.addTask(priority: .userInitiated) { try FieldFile(url: url, read: read) }
                guard let result = try await group.next() else { throw CancellationError() }
                try Task.checkCancellation()
                return result
            }
        } onCancel: { read.cancel() }
    }

    private init(url: URL, read: FieldReadCoordinator) throws {
        try read.checkCancellation()
        guard url.isFileURL, url.pathExtension.lowercased() == "orifield" else {
            throw CocoaError(.fileReadUnsupportedScheme)
        }
        access = FieldFileAccess(url)
        var coordinationError: NSError?
        var result: Result<PlatformFieldDocument, Error>?
        read.coordinator.coordinate(readingItemAt: url, options: .withoutChanges, error: &coordinationError) { source in
            result = Result { try PlatformFieldDocument(opening: source, checkCancellation: read.checkCancellation) }
        }
        try read.checkCancellation()
        if let coordinationError { throw coordinationError }
        guard let result else { throw CocoaError(.fileReadUnknown) }
        document = try result.get()
    }
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
                            Section("О приложении") {
                                Link("Конфиденциальность", destination: URL(string: "https://github.com/qlolp/openradioinspector/blob/612f8b7faa16ee3cf5dbbd2c324ffdd65fbef39c/docs/field/privacy.md")!)
                                    .accessibilityIdentifier("privacy-policy")
                                Link("Поддержка", destination: URL(string: "https://github.com/qlolp/openradioinspector/blob/612f8b7faa16ee3cf5dbbd2c324ffdd65fbef39c/docs/field/support.md")!)
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
            do {
                let verified = try await FieldFile.open(requested.url)
                try Task.checkCancellation()
                guard request?.id == requested.id else { return }
                file = verified
            } catch {
                if !Task.isCancelled, request?.id == requested.id { self.error = error.localizedDescription }
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
