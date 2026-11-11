import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

struct RemoteSweepComparisonPane: View {
    @ObservedObject var model: RemoteDualModel

    var body: some View {
        GroupBox("Сравнение архивных свипов · dB") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Опорный свип из snapshot", selection: $model.comparisonSelectionID) {
                    if model.comparisonSweeps.isEmpty { Text("Нет dBm свипов").tag("") }
                    ForEach(model.comparisonSweeps) { sweep in
                        Text("\(sweep.id) · \(sweep.interval.startedAt)").tag(sweep.id)
                    }
                }
                Button("Закрепить опорный свип") { model.pinComparisonReference() }
                    .disabled(!model.canPinComparisonReference)
                savedReferences
                if let reference = model.comparisonReference {
                    if let name = reference.name { Text("Сохранённое имя: \(name)") }
                    Text("Опорный: \(reference.id)")
                    Text("UTC: \(reference.interval.startedAt) — \(reference.interval.endedAt)")
                        .font(.caption)
                    HStack {
                        Button("Сравнить с последним свипом") { model.compareLatestSweep() }
                            .disabled(!model.canCompareSweep)
                        Button("Очистить") { model.clearSweepComparison() }
                    }
                    if model.sweepComparisonPending {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Ядро сравнивает архивные свипы…").font(.caption)
                            Button("Отмена") { model.cancelSweepComparison() }
                        }
                    }
                }
                if let result = model.sweepComparison {
                    Text("Целевой: \(result.target.id)")
                    Text("UTC: \(result.target.interval.startedAt) — \(result.target.interval.endedAt)")
                        .font(.caption)
                    Text("Опорный результат: \(result.reference.id) · UTC \(result.reference.interval.startedAt) — \(result.reference.interval.endedAt)")
                        .font(.caption)
                    TraceChart(
                        current: zip(result.frequenciesHz, result.deltaLevels).map {
                            PlotPoint(x: Double($0) / 1_000_000, y: $1)
                        }, minimum: [], maximum: [], threshold: 0, yTitle: "dB · целевой − опорный",
                        currentTitle: "разность", thresholdTitle: "ноль"
                    )
                    Text("Частота, МГц").font(.caption).foregroundStyle(.secondary)
                    Text("Максимальное изменение: \(change(result.maxIncrease))")
                    Text("Минимальное изменение: \(change(result.minChange))")
                    Text("\(result.points) точек · исходные уровни dBm сохранены отдельно")
                        .font(.caption).foregroundStyle(.secondary)
                }
                ErrorLine(text: model.sweepComparisonError)
                Text("Только по кнопке, без интерполяции и калибровки. Настройки сравнивает ядро; совпадение записанных настроек не доказывает одинаковую антенну или состояние прибора.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            .textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var savedReferences: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField("Имя опорного свипа (1–80 символов Unicode)", text: $model.referenceName)
            Button("Сохранить закреплённый опорный свип") { model.savePinnedSweepReference() }
                .disabled(!model.canSaveSweepReference)
            HStack {
                Button("Обновить каталог") { model.refreshSweepReferences() }
                    .disabled(!model.canRefreshSweepReferences)
                if model.referenceRequestPending {
                    ProgressView().controlSize(.small)
                    Button("Прекратить ожидание") { model.cancelReferenceRequest() }
                }
            }
            if let catalog = model.referenceCatalog {
                Text(model.referenceStorageDescription)
                    .font(.caption)
                Text("\(model.savedSweepReferences.count) в потоке · максимум \(catalog.limit) на сеанс")
                    .font(.caption)
            }
            Picker("Сохранённый опорный свип", selection: $model.savedReferenceSelectionID) {
                if model.savedSweepReferences.isEmpty { Text("Нет записей").tag("") }
                ForEach(model.savedSweepReferences) { record in
                    Text("\(record.name) · \(record.sweepId)").tag(record.id)
                }
            }
            Button("Использовать сохранённый опорный свип") { model.useSavedSweepReference() }
                .disabled(!model.canUseSavedReference)
            if let status = model.referenceSaveStatus { Text(status).font(.caption) }
            ErrorLine(text: model.referenceCatalogError)
            ErrorLine(text: model.referenceWriteNotice)
        }
    }

    private func change(_ value: PlatformComparisonExtremum) -> String {
        String(format: "%+.2f dB · %.6f МГц", value.deltaDb, Double(value.frequencyHz) / 1_000_000)
    }
}
