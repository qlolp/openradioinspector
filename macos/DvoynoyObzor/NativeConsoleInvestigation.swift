import Foundation
import Observation
import SpektrKomandy

/// Creates a source-bound queue entry only. Starting an investigation remains
/// an explicit existing Core command; no device transport is owned by this UI.
@MainActor @Observable
final class NativeConsoleInvestigation {
    struct Scope: Hashable {
        let server, project, event, session, analyzer, receiver, source: PlatformExactID
        init(server: String, project: String, event: String, session: String,
             analyzer: String, receiver: String, source: String) {
            self.server = PlatformExactID(server); self.project = PlatformExactID(project)
            self.event = PlatformExactID(event); self.session = PlatformExactID(session)
            self.analyzer = PlatformExactID(analyzer); self.receiver = PlatformExactID(receiver)
            self.source = PlatformExactID(source)
        }
    }
    private struct Receipt {
        var candidate: String?
        var investigation: String?
        var uncertain = false
    }
    private var current: Scope?
    private var revision = 0
    private var receipts: [Scope: Receipt] = [:]
    private var order: [Scope] = []
    private(set) var pending = false
    private(set) var message = ""
    private(set) var error = ""
    var investigationID: String? { current.flatMap { receipts[$0]?.investigation } }
    var requiresReconciliation: Bool { current.flatMap { receipts[$0]?.uncertain } == true }

    func configure(_ scope: Scope?) {
        guard scope != current else { return }
        revision += 1; current = scope; pending = false; message = ""; error = ""
    }

    func enqueue(scope: Scope, original: PlatformSweep, startHz: Int64, stopHz: Int64, mode: String?,
                 create: (PlatformCandidateDraft) async throws -> String,
                 enqueue: (String, PlatformInvestigationDraft) async throws -> String) async throws {
        guard current == scope, !pending, investigationID == nil, !requiresReconciliation else { return }
        guard original.exactID == scope.source,
              PlatformExactID(original.sessionId) == scope.session, PlatformExactID(original.streamId) == scope.analyzer,
              original.levelUnit == "dBm", original.frequenciesHz.count == original.levels.count,
              let peak = original.levels.indices.filter({
                  (startHz...max(startHz, stopHz)).contains(original.frequenciesHz[$0]) && original.levels[$0].isFinite
              }).max(by: { original.levels[$0] < original.levels[$1] }) else {
            throw PlatformError.invalidCommand("Нужен точный исходный dBm-свип с отсчётом внутри события.")
        }
        let candidate = try PlatformCandidateDraft(sourceStreamId: original.streamId, interval: original.interval,
            centerFrequencyHz: original.frequenciesHz[peak], peakLevel: original.levels[peak],
            levelUnit: original.levelUnit, observationIds: [original.id])
        let investigation = try PlatformInvestigationDraft(targetStreamId: scope.receiver.rawValue,
            targetFrequencyHz: original.frequenciesHz[peak], mode: mode, priority: 0)
        let token = revision
        pending = true; error = ""
        defer { if token == revision { pending = false } }
        var receipt = receipts[scope] ?? Receipt()
        do {
            if receipt.candidate == nil {
                receipt.candidate = try await create(candidate)
                save(receipt, for: scope)
            }
            guard token == revision, current == scope, !Task.isCancelled, let id = receipt.candidate else { return }
            receipt.investigation = try await enqueue(id, investigation)
            save(receipt, for: scope)
            guard token == revision, current == scope, !Task.isCancelled else { return }
            message = "В очереди AR-DV1: \(receipt.investigation ?? ""). Запуск и результат приёма проверяются отдельно."
        } catch {
            // A lost POST response is not proof that the server did nothing.
            receipt.uncertain = true
            save(receipt, for: scope)
            if token == revision {
                self.error = error.localizedDescription + " Проверьте кандидатов и очередь: повторная отправка заблокирована, результат команды не подтверждён."
            }
            throw error
        }
    }

    private func save(_ receipt: Receipt, for scope: Scope) {
        receipts[scope] = receipt
        order.removeAll { $0 == scope }; order.append(scope)
        while order.count > 32 { receipts[order.removeFirst()] = nil }
    }
}
