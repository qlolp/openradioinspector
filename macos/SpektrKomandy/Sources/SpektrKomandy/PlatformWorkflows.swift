import Foundation

public struct PlatformCandidate: Decodable, Identifiable, Equatable {
    public let id: String
    public let sessionId: String
    public let sourceStreamId: String
    public let interval: PlatformInterval
    public let centerFrequencyHz: Int64
    public let bandwidthHz: Int64?
    public let peakLevel: Double?
    public let levelUnit: String
    public let score: Double?
    public let observationIds: [String]
    public let state: String

    private enum CodingKeys: String, CodingKey {
        case id, sessionId, sourceStreamId, interval, centerFrequencyHz
        case bandwidthHz, peakLevel, levelUnit, score, observationIds, state
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        sessionId = try container.decode(String.self, forKey: .sessionId)
        sourceStreamId = try container.decode(String.self, forKey: .sourceStreamId)
        interval = try container.decode(PlatformInterval.self, forKey: .interval)
        centerFrequencyHz = try container.decode(Int64.self, forKey: .centerFrequencyHz)
        bandwidthHz = try container.decodeIfPresent(Int64.self, forKey: .bandwidthHz)
        peakLevel = try container.decodeIfPresent(Double.self, forKey: .peakLevel)
        levelUnit = try container.decode(String.self, forKey: .levelUnit)
        score = try container.decodeIfPresent(Double.self, forKey: .score)
        observationIds = try container.decodeIfPresent([String].self, forKey: .observationIds) ?? []
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "new"
        guard centerFrequencyHz > 0, bandwidthHz == nil || bandwidthHz! > 0,
              peakLevel?.isFinite != false, score?.isFinite != false else {
            throw DecodingError.dataCorruptedError(
                forKey: .centerFrequencyHz, in: container,
                debugDescription: "Некорректные частота или уровень кандидата"
            )
        }
    }
}

public struct PlatformInvestigation: Decodable, Identifiable, Equatable {
    public let id: String
    public let candidateId: String
    public let targetStreamId: String
    public let targetFrequencyHz: Int64
    public let state: String
    public let priority: Int
    public let mode: String?
    public let createdAt: String
    public let startedAt: String?
    public let endedAt: String?
    public let resultObservationIds: [String]
    public let error: String?

    private enum CodingKeys: String, CodingKey {
        case id, candidateId, targetStreamId, targetFrequencyHz, state, priority, mode
        case createdAt, startedAt, endedAt, resultObservationIds, error
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        candidateId = try container.decode(String.self, forKey: .candidateId)
        targetStreamId = try container.decode(String.self, forKey: .targetStreamId)
        targetFrequencyHz = try container.decode(Int64.self, forKey: .targetFrequencyHz)
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "queued"
        priority = try container.decodeIfPresent(Int.self, forKey: .priority) ?? 0
        mode = try container.decodeIfPresent(String.self, forKey: .mode)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        startedAt = try container.decodeIfPresent(String.self, forKey: .startedAt)
        endedAt = try container.decodeIfPresent(String.self, forKey: .endedAt)
        resultObservationIds = try container.decodeIfPresent([String].self, forKey: .resultObservationIds) ?? []
        error = try container.decodeIfPresent(String.self, forKey: .error)
        guard targetFrequencyHz > 0, priority >= 0 else {
            throw DecodingError.dataCorruptedError(
                forKey: .targetFrequencyHz, in: container,
                debugDescription: "Некорректная частота или приоритет исследования"
            )
        }
    }
}

public struct PlatformAutomaticPolicy: Decodable, Equatable {
    public let enabled: Bool
    public let intervalSeconds: Double?
    public let streamId: String?
    public let thresholdDbm: Double?
    public let dedupBandwidthHz: Int64?
    public let cooldownSeconds: Double?
    public let maxCandidatesPerSweep: Int?
    public let minimumPriority: Int?
    public let receiverStreamId: String?
    public let maxConsecutiveFailures: Int?
    public let retryDelaySeconds: Double?
}

public struct PlatformAutomaticRuntime: Decodable, Equatable {
    public let lastResult: String?
    public let lastError: String?
    public let sweepsProcessed: Int?
    public let candidatesCreated: Int?
    public let duplicatesSuppressed: Int?
    public let successfulDispatches: Int?
    public let failedDispatches: Int?
    public let consecutiveFailures: Int?
}

public struct PlatformAutomaticStatus: Decodable, Equatable {
    public let policy: PlatformAutomaticPolicy
    public let runtime: PlatformAutomaticRuntime
    public let schedulerAlive: Bool
    public let policyPersistent: Bool
    public let restartRequiresOptIn: Bool
    public let processesExistingArchive: Bool?
    public let automaticallyEnqueuesInvestigations: Bool?
}

private enum PlatformWorkflowValidation {
    static func text(_ value: String, _ label: String) throws -> String {
        let result = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !result.isEmpty else { throw PlatformError.invalidCommand("\(label) не должно быть пустым") }
        return result
    }

    static func optionalID(_ value: String?) throws -> String? {
        guard let value else { return nil }
        let result = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return result.isEmpty ? nil : result
    }

    static func frequency(_ value: Int64, _ label: String) throws -> Int64 {
        guard value > 0 else { throw PlatformError.invalidCommand("\(label) должна быть положительным целым числом Гц") }
        return value
    }

    static func integer(_ value: Int, _ label: String, _ range: ClosedRange<Int>) throws -> Int {
        guard range.contains(value) else {
            throw PlatformError.invalidCommand("\(label) должно быть в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    static func int64(_ value: Int64, _ label: String, _ range: ClosedRange<Int64>) throws -> Int64 {
        guard range.contains(value) else {
            throw PlatformError.invalidCommand("\(label) должно быть в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    static func number(_ value: Double, _ label: String, _ range: ClosedRange<Double>) throws -> Double {
        guard value.isFinite, range.contains(value) else {
            throw PlatformError.invalidCommand("\(label) должно быть конечным числом в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }
}

public struct PlatformCandidateDraft: Encodable {
    public let sourceStreamId: String
    public let interval: PlatformInterval
    public let centerFrequencyHz: Int64
    public let bandwidthHz: Int64?
    public let peakLevel: Double?
    public let levelUnit: String
    public let score: Double?
    public let observationIds: [String]

    public init(
        sourceStreamId: String, interval: PlatformInterval, centerFrequencyHz: Int64,
        bandwidthHz: Int64? = nil, peakLevel: Double? = nil, levelUnit: String,
        score: Double? = nil, observationIds: [String] = []
    ) throws {
        self.sourceStreamId = try PlatformWorkflowValidation.text(sourceStreamId, "Исходный поток")
        self.interval = interval
        self.centerFrequencyHz = try PlatformWorkflowValidation.frequency(centerFrequencyHz, "Центральная частота")
        if let bandwidthHz {
            self.bandwidthHz = try PlatformWorkflowValidation.frequency(bandwidthHz, "Полоса")
        } else {
            self.bandwidthHz = nil
        }
        if let peakLevel, !peakLevel.isFinite {
            throw PlatformError.invalidCommand("Пик кандидата должен быть конечным числом")
        }
        if let score, !score.isFinite {
            throw PlatformError.invalidCommand("Оценка кандидата должна быть конечным числом")
        }
        self.peakLevel = peakLevel
        self.levelUnit = try PlatformWorkflowValidation.text(levelUnit, "Единица кандидата")
        self.score = score
        self.observationIds = observationIds
    }
}

public struct PlatformInvestigationDraft: Encodable {
    public let targetStreamId: String
    public let targetFrequencyHz: Int64
    public let mode: String?
    public let priority: Int

    public init(targetStreamId: String, targetFrequencyHz: Int64, mode: String?, priority: Int) throws {
        self.targetStreamId = try PlatformWorkflowValidation.text(targetStreamId, "Receiver")
        self.targetFrequencyHz = try PlatformWorkflowValidation.frequency(targetFrequencyHz, "Частота handoff")
        if let mode, !mode.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            self.mode = mode.trimmingCharacters(in: .whitespacesAndNewlines)
        } else {
            self.mode = nil
        }
        self.priority = try PlatformWorkflowValidation.integer(priority, "Приоритет", 0...1_000_000)
    }
}

public struct PlatformDetectionPolicyDraft: Encodable {
    public let enabled: Bool
    public let intervalSeconds: Double
    public let streamId: String?
    public let thresholdDbm: Double
    public let dedupBandwidthHz: Int64
    public let cooldownSeconds: Double
    public let maxCandidatesPerSweep: Int

    public init(
        enabled: Bool, intervalSeconds: Double, streamId: String?, thresholdDbm: Double,
        dedupBandwidthHz: Int64, cooldownSeconds: Double, maxCandidatesPerSweep: Int
    ) throws {
        self.enabled = enabled
        self.intervalSeconds = try PlatformWorkflowValidation.number(intervalSeconds, "Период обнаружения", 0.1...3600.0)
        self.streamId = try PlatformWorkflowValidation.optionalID(streamId)
        self.thresholdDbm = try PlatformWorkflowValidation.number(thresholdDbm, "Порог dBm", -300.0...100.0)
        self.dedupBandwidthHz = try PlatformWorkflowValidation.int64(dedupBandwidthHz, "Полоса dedup", 1...10_000_000_000)
        self.cooldownSeconds = try PlatformWorkflowValidation.number(cooldownSeconds, "Cooldown", 0.0...86400.0)
        self.maxCandidatesPerSweep = try PlatformWorkflowValidation.integer(maxCandidatesPerSweep, "Максимум кандидатов", 1...100)
    }
}

public struct PlatformHandoffPolicyDraft: Encodable {
    public let enabled: Bool
    public let intervalSeconds: Double
    public let minimumPriority: Int
    public let receiverStreamId: String?
    public let maxConsecutiveFailures: Int
    public let retryDelaySeconds: Double

    public init(
        enabled: Bool, intervalSeconds: Double, minimumPriority: Int,
        receiverStreamId: String?, maxConsecutiveFailures: Int, retryDelaySeconds: Double
    ) throws {
        self.enabled = enabled
        self.intervalSeconds = try PlatformWorkflowValidation.number(intervalSeconds, "Период handoff", 0.1...3600.0)
        self.minimumPriority = try PlatformWorkflowValidation.integer(minimumPriority, "Минимальный приоритет", 0...1_000_000)
        self.receiverStreamId = try PlatformWorkflowValidation.optionalID(receiverStreamId)
        self.maxConsecutiveFailures = try PlatformWorkflowValidation.integer(maxConsecutiveFailures, "Сбоев до отключения", 1...100)
        self.retryDelaySeconds = try PlatformWorkflowValidation.number(retryDelaySeconds, "Задержка повтора", 0.1...86400.0)
    }
}

public struct PlatformEnabledDraft: Encodable {
    public let enabled: Bool
    public init(enabled: Bool) { self.enabled = enabled }
}

private struct PlatformEmptyBody: Encodable {}

public extension PlatformClient {
    func listCandidates(sessionID: String) async throws -> [PlatformCandidate] {
        try await get("/sessions/\(Self.segment(sessionID))/candidates")
    }

    func createCandidate(sessionID: String, draft: PlatformCandidateDraft) async throws -> PlatformCandidate {
        try await post("/sessions/\(Self.segment(sessionID))/candidates", body: draft)
    }

    func dismissCandidate(sessionID: String, candidateID: String) async throws -> PlatformCandidate {
        let id = try PlatformWorkflowValidation.text(candidateID, "Candidate ID")
        return try await post(
            "/sessions/\(Self.segment(sessionID))/candidates/\(Self.segment(id))/dismiss",
            body: PlatformEmptyBody()
        )
    }

    func enqueueInvestigation(
        sessionID: String, candidateID: String, draft: PlatformInvestigationDraft
    ) async throws -> PlatformInvestigation {
        let id = try PlatformWorkflowValidation.text(candidateID, "Candidate ID")
        return try await post(
            "/sessions/\(Self.segment(sessionID))/candidates/\(Self.segment(id))/investigations",
            body: draft
        )
    }

    func listInvestigations(sessionID: String) async throws -> [PlatformInvestigation] {
        try await get("/sessions/\(Self.segment(sessionID))/investigations")
    }

    func nextInvestigation(sessionID: String) async throws -> PlatformInvestigation? {
        try await get("/sessions/\(Self.segment(sessionID))/investigations/next")
    }

    func startNextInvestigation(
        sessionID: String, investigationID: String, confirm: Bool
    ) async throws -> PlatformInvestigation {
        guard confirm else {
            throw PlatformError.invalidCommand("Полуавтоматический handoff требует явного confirm=true")
        }
        let id = try PlatformWorkflowValidation.text(investigationID, "Investigation ID")
        struct Body: Encodable { let investigationId: String; let confirm: Bool }
        return try await post(
            "/sessions/\(Self.segment(sessionID))/investigations/start-next",
            body: Body(investigationId: id, confirm: true)
        )
    }

    func startInvestigation(sessionID: String, investigationID: String) async throws -> PlatformInvestigation {
        try await investigationAction(sessionID: sessionID, investigationID: investigationID, action: "start")
    }

    func completeInvestigation(sessionID: String, investigationID: String) async throws -> PlatformInvestigation {
        try await investigationAction(sessionID: sessionID, investigationID: investigationID, action: "complete")
    }

    func cancelInvestigation(sessionID: String, investigationID: String) async throws -> PlatformInvestigation {
        try await investigationAction(sessionID: sessionID, investigationID: investigationID, action: "cancel")
    }

    func failInvestigation(
        sessionID: String, investigationID: String, error: String
    ) async throws -> PlatformInvestigation {
        let reason = try PlatformWorkflowValidation.text(error, "Причина сбоя")
        struct Body: Encodable { let error: String }
        return try await investigationAction(
            sessionID: sessionID, investigationID: investigationID, action: "fail",
            body: Body(error: reason)
        )
    }

    func automaticHandoff(sessionID: String) async throws -> PlatformAutomaticStatus {
        try await get("/sessions/\(Self.segment(sessionID))/automatic-handoff")
    }

    func automaticDetection(sessionID: String) async throws -> PlatformAutomaticStatus {
        try await get("/sessions/\(Self.segment(sessionID))/automatic-detection")
    }

    func configureAutomaticHandoff(
        sessionID: String, draft: PlatformHandoffPolicyDraft
    ) async throws -> PlatformAutomaticStatus {
        try await post("/sessions/\(Self.segment(sessionID))/automatic-handoff", body: draft)
    }

    func configureAutomaticDetection(
        sessionID: String, draft: PlatformDetectionPolicyDraft
    ) async throws -> PlatformAutomaticStatus {
        try await post("/sessions/\(Self.segment(sessionID))/automatic-detection", body: draft)
    }

    func disableAutomaticHandoff(sessionID: String) async throws -> PlatformAutomaticStatus {
        try await post(
            "/sessions/\(Self.segment(sessionID))/automatic-handoff",
            body: PlatformEnabledDraft(enabled: false)
        )
    }

    func disableAutomaticDetection(sessionID: String) async throws -> PlatformAutomaticStatus {
        try await post(
            "/sessions/\(Self.segment(sessionID))/automatic-detection",
            body: PlatformEnabledDraft(enabled: false)
        )
    }

    private func investigationAction(
        sessionID: String, investigationID: String, action: String
    ) async throws -> PlatformInvestigation {
        try await investigationAction(
            sessionID: sessionID, investigationID: investigationID, action: action,
            body: PlatformEmptyBody()
        )
    }

    private func investigationAction<Body: Encodable>(
        sessionID: String, investigationID: String, action: String, body: Body
    ) async throws -> PlatformInvestigation {
        let id = try PlatformWorkflowValidation.text(investigationID, "Investigation ID")
        return try await post(
            "/sessions/\(Self.segment(sessionID))/investigations/\(Self.segment(id))/\(action)",
            body: body
        )
    }
}
