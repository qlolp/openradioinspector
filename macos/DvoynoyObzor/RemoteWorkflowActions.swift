import Foundation
import AppKit
import UniformTypeIdentifiers
import SpektrKomandy

extension RemoteDualModel {
    func createCandidateFromLatestPeak() {
        guard !spectrumPresentation.paused else { return }
        do {
            guard let analyzer, analyzer.isDbmAnalyzer,
                  let sweep = spectrumPresentation.sweeps.last, PlatformExactID.same(sweep.streamId, analyzer.id), sweep.levelUnit == "dBm",
                  let index = sweep.levels.indices.max(by: { sweep.levels[$0] < sweep.levels[$1] }) else {
                throw PlatformError.invalidCommand("Нет фактического dBm sweep для создания кандидата")
            }
            let draft = try PlatformCandidateDraft(
                sourceStreamId: analyzer.id, interval: sweep.interval,
                centerFrequencyHz: sweep.frequenciesHz[index],
                peakLevel: sweep.levels[index], levelUnit: sweep.levelUnit,
                observationIds: [sweep.id]
            )
            perform { try await $0.createCandidate(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    func enqueueSelectedCandidate() {
        do {
            guard let selectedCandidate else {
                throw PlatformError.invalidCommand("Выберите кандидата")
            }
            guard let receiver, receiver.isReceiver else {
                throw PlatformError.invalidCommand("Для исследования нужен receiver сеанса")
            }
            let priority = try integerField(investigationPriority, "Приоритет", 0...1_000_000)
            let draft = try PlatformInvestigationDraft(
                targetStreamId: receiver.id,
                targetFrequencyHz: selectedCandidate.centerFrequencyHz,
                mode: mode, priority: priority
            )
            let candidateID = selectedCandidate.id
            perform {
                try await $0.enqueueInvestigation(sessionID: $1, candidateID: candidateID, draft: draft)
            }
        } catch {
            errorText = describeSession(error)
        }
    }

    func dismissSelectedCandidate() {
        guard let selectedCandidate else {
            errorText = "Выберите кандидата"
            return
        }
        let id = selectedCandidate.id
        perform { try await $0.dismissCandidate(sessionID: $1, candidateID: id) }
    }

    func startSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.startInvestigation(sessionID: $1, investigationID: id) }
    }

    func startNextInvestigation() {
        guard let nextInvestigation else {
            errorText = "Очередь исследований пуста"
            return
        }
        let id = nextInvestigation.id
        perform { try await $0.startNextInvestigation(sessionID: $1, investigationID: id, confirm: true) }
    }

    func completeSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.completeInvestigation(sessionID: $1, investigationID: id) }
    }

    func failSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        let reason = failureReason
        perform { try await $0.failInvestigation(sessionID: $1, investigationID: id, error: reason) }
    }

    func cancelSelectedInvestigation() {
        guard let selectedInvestigation else {
            errorText = "Выберите исследование"
            return
        }
        let id = selectedInvestigation.id
        perform { try await $0.cancelInvestigation(sessionID: $1, investigationID: id) }
    }

    func saveAutomaticDetection() {
        configureAutomaticDetection(enabled: nil)
    }

    func enableAutomaticDetection() {
        configureAutomaticDetection(enabled: true)
    }

    func disableAutomaticDetection() {
        perform { try await $0.disableAutomaticDetection(sessionID: $1) }
    }

    func saveAutomaticHandoff() {
        configureAutomaticHandoff(enabled: nil)
    }

    func enableAutomaticHandoff() {
        configureAutomaticHandoff(enabled: true)
    }

    func disableAutomaticHandoff() {
        perform { try await $0.disableAutomaticHandoff(sessionID: $1) }
    }

    func saveAutomaticCompletion() {
        configureAutomaticCompletion(enabled: nil)
    }

    func enableAutomaticCompletion() {
        configureAutomaticCompletion(enabled: true)
    }

    func disableAutomaticCompletion() {
        perform { try await $0.disableAutomaticCompletion(sessionID: $1) }
    }

    func enableTaskScheduler() {
        configureTaskScheduler(enabled: true)
    }

    func disableTaskScheduler() {
        configureTaskScheduler(enabled: false)
    }

    func refreshArchiveAnalytics() {
        guard let client, !selectedSessionID.isEmpty, !archiveAnalyticsPending else { return }
        let sessionID = selectedSessionID
        let requestGeneration = generation
        archiveAnalyticsPending = true
        archiveAnalyticsError = nil
        Task {
            do {
                let summary = try await client.analyticsSummary(sessionID: sessionID)
                guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                      self.client === client else { return }
                archiveAnalytics = summary
            } catch {
                guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                      self.client === client else { return }
                archiveAnalytics = nil
                archiveAnalyticsError = describeSession(error)
            }
            guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                  self.client === client else { return }
            archiveAnalyticsPending = false
        }
    }

    func exportArchive() {
        guard let client, !selectedSessionID.isEmpty, !archiveExportPending else { return }
        let sessionID = selectedSessionID
        let safeName = sessionID.replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "\\", with: "_")
        let panel = NSSavePanel()
        panel.title = "Сохранить NDJSON snapshot"
        panel.nameFieldStringValue = "archive-\(safeName).ndjson"
        guard let ndjsonType = UTType(filenameExtension: "ndjson", conformingTo: .plainText) else {
            archiveExportStatus = "Не удалось определить тип файла NDJSON"
            return
        }
        panel.allowedContentTypes = [ndjsonType]
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        let requestGeneration = generation
        archiveExportPending = true
        archiveExportStatus = "Сервер формирует NDJSON snapshot…"
        Task {
            do {
                let result = try await client.exportArchive(
                    sessionID: sessionID, destination: destination
                )
                guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                      self.client === client else { return }
                archiveExportStatus = "NDJSON сохранён: \(result.sweeps) sweeps, \(result.observations) observations · \(result.fileURL.lastPathComponent)"
            } catch {
                guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                      self.client === client else { return }
                archiveExportStatus = "NDJSON не сохранён: \(describeSession(error))"
            }
            guard requestGeneration == generation, PlatformExactID.same(sessionID, selectedSessionID),
                  self.client === client else { return }
            archiveExportPending = false
        }
    }

    func createTask() {
        guard let client, !taskCreatePending else { return }
        let requestGeneration = generation
        taskCreatePending = true
        newTaskStatus = "ControlTask создаётся через API…"
        Task {
            do {
                let duration = try numberField(newTaskDuration, "duration_s", 0.001...86_400.0)
                let draft = try PlatformControlTaskDraft(
                    name: newTaskName, taskFile: newTaskFile, startAt: newTaskStartAt,
                    durationS: duration, equipment: newTaskEquipment
                )
                let created = try await client.createTask(draft: draft)
                guard requestGeneration == generation, self.client === client else { return }
                newTaskStatus = "Создано: \(created.name) · \(created.status) · \(created.startAt ?? "время не задано")"
                showTaskCreate = false
                newTaskName = ""
                newTaskFile = ""
                newTaskStartAt = ""
                newTaskEquipment = ""
                await refreshTaskScheduler(client)
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                newTaskStatus = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            taskCreatePending = false
        }
    }

    func cancelTask(name: String) {
        guard let client, !taskCancelPending else { return }
        let requestGeneration = generation
        taskCancelPending = true
        taskCancelStatus = "Отмена \(name) через API…"
        Task {
            do {
                let cancelled = try await client.cancelTask(name: name)
                guard requestGeneration == generation, self.client === client else { return }
                taskCancelStatus = "Отменено: \(cancelled.name) · \(cancelled.status) · \(cancelled.outcome)"
                await refreshTaskScheduler(client)
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                taskCancelStatus = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            taskCancelPending = false
        }
    }

    private func configureTaskScheduler(enabled: Bool) {
        guard let client else { return }
        let requestGeneration = generation
        taskSchedulerCommandRevision += 1
        busy = true
        Task {
            do {
                let status = try await client.configureTaskScheduler(enabled: enabled)
                guard requestGeneration == generation, self.client === client else { return }
                taskSchedulerStatus = status
                taskSchedulerError = nil
            } catch {
                guard requestGeneration == generation, self.client === client else { return }
                taskSchedulerError = describeSession(error)
            }
            guard requestGeneration == generation, self.client === client else { return }
            busy = false
        }
    }

    func refreshTaskScheduler(_ client: PlatformClient) async {
        let requestGeneration = generation
        let schedulerRevision = taskSchedulerCommandRevision
        do {
            let status = try await client.taskScheduler()
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerStatus = status
            taskSchedulerError = nil
        } catch {
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerError = describeSession(error)
        }
        do {
            let tasks = try await client.taskSchedulerTasks()
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerTasks = tasks
            taskSchedulerTasksError = nil
        } catch {
            guard requestGeneration == generation, self.client === client,
                  schedulerRevision == taskSchedulerCommandRevision else { return }
            taskSchedulerTasksError = describeSession(error)
        }
    }

    private func configureAutomaticCompletion(enabled: Bool?) {
        do {
            let draft = try PlatformCompletionPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(completionInterval, "Период завершения", 0.1...3600.0),
                minimumListeningSeconds: try numberField(completionMinimumListening, "Минимум прослушивания", 0.0...86_400.0),
                minimumObservations: try integerField(completionMinimumObservations, "Минимум наблюдений", 1...1000),
                receiverStreamId: completionReceiverStreamID
            )
            perform { try await $0.configureAutomaticCompletion(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func configureAutomaticDetection(enabled: Bool?) {
        do {
            let draft = try PlatformDetectionPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(detectionInterval, "Период обнаружения", 0.1...3600.0),
                streamId: detectionStreamID,
                thresholdDbm: try numberField(detectionThreshold, "Порог dBm", -300.0...100.0),
                dedupBandwidthHz: try int64Field(detectionDedupBandwidth, "Полоса dedup", 1...10_000_000_000),
                cooldownSeconds: try numberField(detectionCooldown, "Cooldown", 0.0...86400.0),
                maxCandidatesPerSweep: try integerField(detectionMaximumCandidates, "Максимум кандидатов", 1...100)
            )
            perform { try await $0.configureAutomaticDetection(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func configureAutomaticHandoff(enabled: Bool?) {
        do {
            let draft = try PlatformHandoffPolicyDraft(
                enabled: enabled,
                intervalSeconds: try numberField(handoffInterval, "Период handoff", 0.1...3600.0),
                minimumPriority: try integerField(handoffMinimumPriority, "Минимальный приоритет", 0...1_000_000),
                receiverStreamId: handoffReceiverStreamID,
                maxConsecutiveFailures: try integerField(handoffMaximumFailures, "Сбоев до отключения", 1...100),
                retryDelaySeconds: try numberField(handoffRetryDelay, "Задержка повтора", 0.1...86400.0)
            )
            perform { try await $0.configureAutomaticHandoff(sessionID: $1, draft: draft) }
        } catch {
            errorText = describeSession(error)
        }
    }

    private func integerField(_ text: String, _ label: String, _ range: ClosedRange<Int>) throws -> Int {
        guard let value = Int(text.trimmingCharacters(in: .whitespacesAndNewlines)), range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется целое в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    private func int64Field(_ text: String, _ label: String, _ range: ClosedRange<Int64>) throws -> Int64 {
        guard let value = Int64(text.trimmingCharacters(in: .whitespacesAndNewlines)), range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется целое в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }

    private func numberField(_ text: String, _ label: String, _ range: ClosedRange<Double>) throws -> Double {
        guard let value = parseUserNumber(text), value.isFinite, range.contains(value) else {
            throw PlatformError.invalidCommand("\(label): требуется конечное число в диапазоне \(range.lowerBound)…\(range.upperBound)")
        }
        return value
    }


    func applyWorkflow(
        _ snapshot: PlatformSnapshot, schedulerRevision: Int
    ) {
        candidates = snapshot.candidates
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            frequencyRounding.configure(client: client, scope: .init(server: serverURL, session: selectedSessionID),
                candidates: snapshot.candidates, ready: !snapshot.workflowErrors.contains { $0.hasPrefix("candidates:") })
        } else { frequencyRounding.reset() }

        frequencyRaster.configure(client: (PlatformExactID.same(connectedServerURL, serverURL) ? client : nil),
            scope: PlatformExactID.same(connectedServerURL, serverURL)
                ? .init(server: serverURL, session: selectedSessionID) : nil,
            candidates: snapshot.candidates)
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           let stream = analyzer?.id {
            measurementResults.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream))
            frequencyCounter.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream),
                sweeps: snapshot.sweeps)
            fieldStrength.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID, stream: stream), sweeps: snapshot.sweeps)
        } else { measurementResults.reset(); frequencyCounter.reset(); fieldStrength.reset() }
        if let client, PlatformThreeSweepThresholds.sameID(connectedServerURL, serverURL),
           PlatformThreeSweepThresholds.sameID(snapshot.session.id, selectedSessionID) {
            dvbEvaluation.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID))
            measurementMap.configure(client: client,
                scope: .init(server: serverURL, session: selectedSessionID))
            configureAuxiliaryStreams(client: client, session: snapshot.session)
        } else { dvbEvaluation.reset(); measurementMap.reset(); configureAuxiliaryStreams(client: nil, session: nil) }
        investigations = snapshot.investigations
        nextInvestigation = snapshot.nextInvestigation
        automaticHandoffStatus = snapshot.automaticHandoff
        automaticDetectionStatus = snapshot.automaticDetection
        automaticCompletionStatus = snapshot.automaticCompletion
        if schedulerRevision == taskSchedulerCommandRevision {
            taskSchedulerStatus = snapshot.taskScheduler
            taskSchedulerError = snapshot.taskSchedulerError
            taskSchedulerTasks = snapshot.taskSchedulerTasks
            taskSchedulerTasksError = snapshot.taskSchedulerTasksError
        }
        workflowErrors = snapshot.workflowErrors
        if !candidates.contains(where: { PlatformExactID.same($0.id, selectedCandidateID) }) {
            selectedCandidateID = candidates.first(where: { $0.state == "new" })?.id ?? candidates.first?.id ?? ""
        }
        if !investigations.contains(where: { PlatformExactID.same($0.id, selectedInvestigationID) }) {
            selectedInvestigationID = investigations.first(where: {
                ["queued", "tuning", "listening", "decoding"].contains($0.state)
            })?.id ?? investigations.first?.id ?? ""
        }
        loadPolicyFormsIfNeeded()
    }

    private func loadPolicyFormsIfNeeded() {
        if !detectionFormInitialized, let policy = automaticDetectionStatus?.policy {
            detectionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            detectionStreamID = policy.streamId ?? ""
            detectionThreshold = policy.thresholdDbm.map { String(describing: $0) } ?? "-70"
            detectionDedupBandwidth = policy.dedupBandwidthHz.map { String($0) } ?? "100000"
            detectionCooldown = policy.cooldownSeconds.map { String(describing: $0) } ?? "60"
            detectionMaximumCandidates = policy.maxCandidatesPerSweep.map { String($0) } ?? "10"
            detectionFormInitialized = true
        }
        if !handoffFormInitialized, let policy = automaticHandoffStatus?.policy {
            handoffInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            handoffMinimumPriority = policy.minimumPriority.map { String($0) } ?? "0"
            handoffReceiverStreamID = policy.receiverStreamId ?? ""
            handoffMaximumFailures = policy.maxConsecutiveFailures.map { String($0) } ?? "3"
            handoffRetryDelay = policy.retryDelaySeconds.map { String(describing: $0) } ?? "30"
            handoffFormInitialized = true
        }
        if !completionFormInitialized, let policy = automaticCompletionStatus?.policy {
            completionInterval = policy.intervalSeconds.map { String(describing: $0) } ?? "1"
            completionMinimumListening = policy.minimumListeningSeconds.map { String(describing: $0) } ?? "0"
            completionMinimumObservations = policy.minimumObservations.map { String($0) } ?? "1"
            completionReceiverStreamID = policy.receiverStreamId ?? ""
            completionFormInitialized = true
        }
    }

}
