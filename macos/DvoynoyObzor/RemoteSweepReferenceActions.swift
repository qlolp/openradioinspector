import Foundation
import AppKit
import UniformTypeIdentifiers
import SpektrKomandy

extension RemoteDualModel {
    var currentReferenceScope: ReferenceScope? {
        guard connected, connectedServerURL == serverURL, !selectedSessionID.isEmpty,
              PlatformExactID.same(session?.id, selectedSessionID), let stream = analyzer?.id else { return nil }
        return ReferenceScope(server: serverURL, session: selectedSessionID, stream: stream)
    }

    var referenceStorageDescription: String {
        guard let catalog = referenceCatalog else { return "Хранение каталога неизвестно" }
        if catalog.persistent { return "SQLite: постоянный каталог" }
        let mode = catalog.storageMode == "sqlite" ? "SQLite (:memory:)" : "Memory"
        return "\(mode): временный каталог, исчезнет при перезапуске ядра"
    }

    var referenceRequestPending: Bool { referenceCatalogPending || referenceSavePending }
    var canRefreshSweepReferences: Bool { currentReferenceScope != nil && !referenceRequestPending }
    var canSaveSweepReference: Bool {
        guard let scope = currentReferenceScope, let reference = comparisonReference else { return false }
        return !referenceRequestPending && !uncertainReferenceScopes.contains(scope)
            && PlatformExactID.same(reference.sessionId, scope.session) && PlatformExactID.same(reference.streamId, scope.stream)
            && (try? normalizedSweepReferenceName(referenceName)) != nil
    }
    var canUseSavedReference: Bool {
        guard let scope = currentReferenceScope else { return false }
        return savedSweepReferences.contains {
            PlatformExactID.same($0.id, savedReferenceSelectionID) && PlatformExactID.same($0.sessionId, scope.session) && PlatformExactID.same($0.streamId, scope.stream)
        }
    }

    func useSavedSweepReference() {
        guard canUseSavedReference, let record = savedSweepReferences.first(where: { PlatformExactID.same($0.id, savedReferenceSelectionID) }) else { return }
        cancelSweepComparison()
        comparisonReference = PinnedSweepReference(record)
    }

    func refreshSweepReferences() {
        guard canRefreshSweepReferences, let scope = currentReferenceScope, let client else { return }
        loadedReferenceScope = scope
        beginReferenceRequest(scope: scope, saving: false)
        let revision = referenceRequestRevision
        referenceTask = Task { [weak self] in
            do {
                let catalog = try await client.sweepReferences(sessionID: scope.session, streamID: scope.stream)
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                self.referenceCatalog = catalog
                self.savedSweepReferences = catalog.items
                if !catalog.items.contains(where: { PlatformExactID.same($0.id, self.savedReferenceSelectionID) }) {
                    self.savedReferenceSelectionID = catalog.items.first?.id ?? ""
                }
                self.referenceCatalogError = nil
                self.uncertainReferenceScopes.remove(scope)
                if self.referenceNoticeScope == scope {
                    self.referenceWriteNotice = nil
                    self.referenceNoticeScope = nil
                    self.referenceSaveStatus = "Каталог обновлён. Проверьте имя; повтор с тем же именем и свипом безопасен."
                }
                self.finishReferenceRequest()
            } catch {
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                self.referenceCatalog = nil
                self.savedSweepReferences = []
                self.savedReferenceSelectionID = ""
                self.referenceCatalogError = self.describeReferenceError(error)
                self.finishReferenceRequest()
            }
        }
    }

    func savePinnedSweepReference() {
        guard canSaveSweepReference, let scope = currentReferenceScope, let client,
              let reference = comparisonReference, let name = try? normalizedSweepReferenceName(referenceName) else { return }
        beginReferenceRequest(scope: scope, saving: true)
        referenceSaveStatus = nil
        let revision = referenceRequestRevision
        referenceTask = Task { [weak self] in
            do {
                let record = try await client.saveSweepReference(sessionID: scope.session, sweepID: reference.id, name: name)
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                guard PlatformExactID.same(record.streamId, scope.stream) else {
                    throw PlatformError.invalidResponse("Сервер вернул опорный свип другого потока")
                }
                self.savedSweepReferences.removeAll { PlatformExactID.same($0.id, record.id) }
                self.savedSweepReferences.append(record)
                self.savedSweepReferences.sort { $0.createdAt < $1.createdAt }
                self.savedReferenceSelectionID = record.id
                self.referenceSaveStatus = "Сохранено: \(record.name). Каталог ограничен 100 записями на сеанс."
                self.finishReferenceRequest()
            } catch {
                guard let self, !Task.isCancelled, self.referenceRequestMatches(scope, revision, client) else { return }
                if case PlatformError.http(let status, _, _) = error, [400, 404, 409].contains(status) {
                    self.referenceSaveStatus = self.describeReferenceError(error)
                } else {
                    self.markReferenceWriteUncertain(scope)
                }
                self.finishReferenceRequest()
            }
        }
    }

    private func describeReferenceError(_ error: Error) -> String {
        switch error {
        case PlatformError.http(status: 404, code: _, message: _):
            return "Каталог или свип не найден; старый сервер может не поддерживать именованные опорные свипы (404). Локальное закрепление доступно."
        case PlatformError.http(status: 409, code: "reference_name_conflict", message: _):
            return "Имя уже занято другим свипом. Выберите новое имя; перезапись не выполняется."
        case PlatformError.http(status: 409, code: "reference_limit_reached", message: _):
            return "Достигнут предел: 100 опорных свипов на сеанс."
        case PlatformError.http(status: 409, code: _, message: let message):
            return "Свип несовместим (409): \(message)"
        default: return describeSession(error)
        }
    }

    private func referenceRequestMatches(_ scope: ReferenceScope, _ revision: Int, _ client: PlatformClient) -> Bool {
        referenceRequestRevision == revision && currentReferenceScope == scope && self.client === client
    }

    private func beginReferenceRequest(scope: ReferenceScope, saving: Bool) {
        referenceRequestRevision += 1
        referenceRequestScope = scope
        referenceSavePending = saving
        referenceCatalogPending = !saving
        referenceCatalogError = nil
        let revision = referenceRequestRevision
        referenceTimeoutTask = Task { [weak self, comparisonTimeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: comparisonTimeoutNanoseconds) } catch { return }
            guard let self, self.referenceRequestRevision == revision, self.referenceRequestPending else { return }
            self.cancelReferenceRequest()
            if !saving {
                self.referenceCatalog = nil
                self.savedSweepReferences = []
                self.savedReferenceSelectionID = ""
                self.referenceCatalogError = "Время ожидания каталога истекло. Нажмите Обновить каталог."
            }
        }
    }

    private func markReferenceWriteUncertain(_ scope: ReferenceScope) {
        uncertainReferenceScopes.insert(scope)
        referenceNoticeScope = scope
        referenceWriteNotice = "Результат сохранения неизвестен: \(scope.server), сеанс \(scope.session). Запись могла сохраниться на сервере. Вернитесь к этому сеансу и нажмите Обновить каталог перед повтором."
    }

    /// Stops waiting locally, never promises that the server write was canceled.
    func cancelReferenceRequest() {
        if referenceSavePending, let scope = referenceRequestScope { markReferenceWriteUncertain(scope) }
        referenceRequestRevision += 1
        referenceTask?.cancel()
        finishReferenceRequest()
    }

    private func finishReferenceRequest() {
        referenceTimeoutTask?.cancel()
        referenceTimeoutTask = nil
        referenceTask = nil
        referenceRequestScope = nil
        referenceSavePending = false
        referenceCatalogPending = false
    }

    var canPinComparisonReference: Bool {
        currentReferenceScope != nil && comparisonSweeps.contains(where: { PlatformExactID.same($0.id, comparisonSelectionID) })
    }

    var canCompareSweep: Bool {
        currentReferenceScope != nil && comparisonReference != nil && comparisonSweeps.last != nil
    }

    func pinComparisonReference() {
        guard currentReferenceScope != nil, let selected = comparisonSweeps.first(where: { PlatformExactID.same($0.id, comparisonSelectionID) }),
              PlatformExactID.same(selected.sessionId, selectedSessionID), PlatformExactID.same(selected.streamId, analyzer?.id),
              selected.levelUnit == "dBm" else { return }
        cancelSweepComparison()
        comparisonReference = PinnedSweepReference(selected)
    }

    func clearSweepComparison() {
        cancelSweepComparison()
        comparisonReference = nil
    }

    func cancelSweepComparison() {
        comparisonRevision += 1
        comparisonTask?.cancel()
        comparisonTimeoutTask?.cancel()
        comparisonTask = nil
        comparisonTimeoutTask = nil
        sweepComparisonPending = false
        sweepComparison = nil
        sweepComparisonError = nil
    }

    func resetComparisonScope() {
        clearSpectrumPresentation()
        sweepAverage.reset()
        threeSweepThresholds.reset()
        sweepContext.reset()
        spectralMask.reset(); sweepQuality.reset()
        frequencyRaster.reset()
        measurementResults.reset()
        frequencyCounter.reset()
        dvbEvaluation.reset()
        phonogram.reset()
        signalList.reset()
        measurementMap.reset()
        audioStream.reset()
        sweepThreshold.reset()
        cancelReferenceRequest()
        loadedReferenceScope = nil
        referenceCatalog = nil
        savedSweepReferences = []
        savedReferenceSelectionID = ""
        referenceName = ""
        referenceCatalogError = nil
        referenceSaveStatus = nil
        clearSweepComparison()
        comparisonSweeps = []
        comparisonSelectionID = ""
    }

    /// Explicit only: polling never moves the pinned reference or refreshes its comparison.
    func compareLatestSweep() {
        guard currentReferenceScope != nil, let client, let reference = comparisonReference,
              let target = comparisonSweeps.last, PlatformExactID.same(reference.sessionId, selectedSessionID),
              PlatformExactID.same(target.sessionId, selectedSessionID), PlatformExactID.same(reference.streamId, analyzer?.id),
              PlatformExactID.same(target.streamId, reference.streamId) else { return }
        cancelSweepComparison()
        let revision = comparisonRevision
        let requestGeneration = generation
        let connection = connectionGeneration
        let sessionID = selectedSessionID
        let streamID = reference.streamId
        sweepComparisonPending = true
        comparisonTask = Task { [weak self] in
            do {
                let result = try await client.sweepComparison(
                    sessionID: sessionID, referenceID: reference.id, targetID: target.id
                )
                guard let self, !Task.isCancelled, revision == self.comparisonRevision,
                      requestGeneration == self.generation, connection == self.connectionGeneration,
                      self.client === client, PlatformExactID.same(self.selectedSessionID, sessionID),
                      PlatformExactID.same(self.analyzer?.id, streamID) else { return }
                guard PlatformExactID.same(result.streamId, streamID) else {
                    throw PlatformError.invalidResponse("Сервер вернул сравнение другого потока")
                }
                self.sweepComparison = result
                self.finishSweepComparison()
            } catch {
                guard let self, !Task.isCancelled, revision == self.comparisonRevision,
                      requestGeneration == self.generation, connection == self.connectionGeneration,
                      self.client === client, PlatformExactID.same(self.selectedSessionID, sessionID),
                      PlatformExactID.same(self.analyzer?.id, streamID) else { return }
                self.sweepComparison = nil
                switch error {
                case PlatformError.http(status: 404, code: _, message: _):
                    self.sweepComparisonError = "Архивный свип не найден или сервер не поддерживает сравнение (404)"
                case PlatformError.http(status: 409, code: _, message: let message):
                    self.sweepComparisonError = "Свипы несовместимы (409): \(message)"
                default:
                    self.sweepComparisonError = describeSession(error)
                }
                self.finishSweepComparison()
            }
        }
        comparisonTimeoutTask = Task { [weak self, comparisonTimeoutNanoseconds] in
            do { try await Task.sleep(nanoseconds: comparisonTimeoutNanoseconds) }
            catch { return }
            guard let self, revision == self.comparisonRevision, self.sweepComparisonPending else { return }
            self.cancelSweepComparison()
            self.sweepComparisonError = "Время ожидания сравнения истекло. Можно повторить запрос."
        }
    }

    private func finishSweepComparison() {
        comparisonTimeoutTask?.cancel()
        comparisonTimeoutTask = nil
        comparisonTask = nil
        sweepComparisonPending = false
    }

}
