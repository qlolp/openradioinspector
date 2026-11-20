import Foundation

enum NativeConsoleServiceWatch {
    @MainActor static func run(enabled: Bool, inspect: () async -> Void,
        wait: () async throws -> Void = { try await Task.sleep(for: .seconds(2)) }) async {
        guard enabled else { return }
        while !Task.isCancelled {
            await inspect()
            do { try await wait() } catch { return }
        }
    }
}
