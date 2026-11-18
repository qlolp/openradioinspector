import Foundation
import Observation
import SpektrKomandy

/// Window-owned, bounded POST ledger. Navigation never releases an in-flight or
/// uncertain write. Only verified archive replacement invalidates old handles.
@MainActor @Observable
final class NativeConsoleInvestigation {
    struct Scope: Hashable {
        let server, runtime, project, event, session, analyzer, receiver, source: PlatformExactID
        init(server: String, project: String, event: String, session: String,
             analyzer: String, receiver: String, source: String, runtime: String = "") {
            self.server = PlatformExactID(server); self.runtime = PlatformExactID(runtime)
            self.project = PlatformExactID(project); self.event = PlatformExactID(event)
            self.session = PlatformExactID(session); self.analyzer = PlatformExactID(analyzer)
            self.receiver = PlatformExactID(receiver); self.source = PlatformExactID(source)
        }
    }
    private struct Receipt {
        var candidate: String?
        var investigation: String?
        var uncertain = false
        var error = ""
    }
    private var current: Scope?
    private var revision = 0
    private var archiveEpoch = 0
    private var receipts: [Scope: Receipt] = [:]
    private var inFlight: [Scope: UUID] = [:]
    static let capacity = 128
    var pending: Bool { current.map { inFlight[$0] != nil } ?? false }
    var investigationID: String? { current.flatMap { receipts[$0]?.investigation } }
    var requiresReconciliation: Bool { current.flatMap { receipts[$0]?.uncertain } == true }
    var canEnqueue: Bool {
        guard let current else { return false }
        return !pending && investigationID == nil && !requiresReconciliation &&
            (receipts[current] != nil || receipts.count < Self.capacity)
    }
    var message: String {
        if let investigationID { return "В очереди AR-DV1: \(investigationID). Запуск и результат приёма проверяются отдельно." }
        if let current, receipts[current] == nil, receipts.count >= Self.capacity {
            return "Лимит журнала команд этого окна: \(Self.capacity). Сначала сверяйте результаты в очереди; неизвестные ответы не удаляются автоматически."
        }
        return ""
    }
    var error: String {
        guard let current, let receipt = receipts[current], receipt.uncertain else { return "" }
        return receipt.error + " Проверьте кандидатов и очередь: повторная отправка заблокирована, результат команды не подтверждён."
    }

    func configure(_ scope: Scope?) {
        guard scope != current else { return }
        revision += 1; current = scope
    }

    func invalidateArchiveEvidence() {
        archiveEpoch += 1; revision += 1
        for (scope, receipt) in receipts {
            if receipt.uncertain || inFlight[scope] != nil {
                receipts[scope] = Receipt(uncertain: true, error: "Архив заменён во время неподтверждённой команды.")
            } else { receipts[scope] = nil }
        }
    }

    func enqueue(scope: Scope, original: PlatformSweep, startHz: Int64, stopHz: Int64, mode: String?,
                 create: (PlatformCandidateDraft) async throws -> String,
                 enqueue: (String, PlatformInvestigationDraft) async throws -> String) async throws {
        guard current == scope, canEnqueue else { return }
        guard startHz <= stopHz, original.exactID == scope.source,
              PlatformExactID(original.sessionId) == scope.session, PlatformExactID(original.streamId) == scope.analyzer,
              original.levelUnit == "dBm", original.frequenciesHz.count == original.levels.count,
              let peak = original.levels.indices.filter({
                  (startHz...stopHz).contains(original.frequenciesHz[$0]) && original.levels[$0].isFinite
              }).max(by: { original.levels[$0] < original.levels[$1] }) else {
            throw PlatformError.invalidCommand("Нужен точный исходный dBm-свип с отсчётом внутри события.")
        }
        let candidate = try PlatformCandidateDraft(sourceStreamId: original.streamId, interval: original.interval,
            centerFrequencyHz: original.frequenciesHz[peak], peakLevel: original.levels[peak],
            levelUnit: original.levelUnit, observationIds: [original.id])
        let investigation = try PlatformInvestigationDraft(targetStreamId: scope.receiver.rawValue,
            targetFrequencyHz: original.frequenciesHz[peak], mode: mode, priority: 0)
        let token = revision, epoch = archiveEpoch, request = UUID()
        var receipt = receipts[scope] ?? Receipt()
        receipts[scope] = receipt // reserve capacity before the first suspension
        inFlight[scope] = request
        defer { if inFlight[scope] == request { inFlight[scope] = nil } }
        do {
            if receipt.candidate == nil {
                let id = try await create(candidate)
                guard epoch == archiveEpoch else { return }
                receipt.candidate = id; receipts[scope] = receipt
            }
            guard token == revision, current == scope, !Task.isCancelled, let id = receipt.candidate else { return }
            let investigationID = try await enqueue(id, investigation)
            guard epoch == archiveEpoch else { return }
            receipt.investigation = investigationID; receipts[scope] = receipt
        } catch {
            if epoch == archiveEpoch {
                receipt.uncertain = true; receipt.error = error.localizedDescription
                receipts[scope] = receipt
            }
            throw error
        }
    }
}
