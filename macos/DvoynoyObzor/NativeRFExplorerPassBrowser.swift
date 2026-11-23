import Foundation
import Observation
import SpektrKomandy

struct NativeRFExplorerPassQuery: Equatable, Hashable {
    var before: String?
    var pass: PlatformExactID?
    var holdSince: String?
    var columns = 768

    var followsLatest: Bool { before == nil && pass == nil }
}

@MainActor @Observable final class NativeRFExplorerPassBrowser {
    private(set) var query: NativeRFExplorerPassQuery?
    private(set) var page: PlatformRFExplorerPassPage?
    private(set) var panorama: PlatformRFExplorerPanorama?
    private(set) var busy = false
    private(set) var message = ""
    private var revision = UUID()

    var selectedSummary: PlatformRFExplorerPassSummary? {
        if let panorama { return panorama.summary }
        if let pass = query?.pass { return page?.items.first { $0.id == pass } }
        return page?.items.first
    }

    func load(_ request: NativeRFExplorerPassQuery,
              list: (String?) async throws -> PlatformRFExplorerPassPage,
              detail: (String, Int, String?) async throws -> PlatformRFExplorerPanorama) async {
        let token = UUID()
        revision = token
        if request != query {
            query = request; page = nil; panorama = nil
        }
        busy = true
        defer { if token == revision { busy = false } }
        do {
            let result = try await list(request.before)
            guard token == revision, !Task.isCancelled else { return }
            page = result
            guard let id = request.pass?.rawValue ?? result.items.first?.passId else {
                panorama = nil; message = "Сохранённых проходов пока нет."; return
            }
            let value = try await detail(id, request.columns, request.holdSince)
            guard token == revision, !Task.isCancelled else { return }
            panorama = value; message = ""
        } catch {
            if token == revision, !Task.isCancelled { message = error.localizedDescription }
        }
    }
}
