import SpektrKomandy
import SwiftUI

struct ArPane: View {
    @ObservedObject var model: ArModel
    @State private var selection: Int?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("AOR AR-DV1")
                        .font(.title2.weight(.semibold))
                    Text("Приёмник. Уровень — отсчёт S-метра, не дБм.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                GroupBox("Порт") {
                    VStack(alignment: .leading, spacing: 8) {
                        PortFields(path: $model.portPath, connected: model.connected, ports: model.knownPorts, onRefresh: model.refreshPorts) {
                            Picker("Скорость", selection: $model.baud) {
                                ForEach(model.baudRates, id: \.self) { rate in
                                    Text(String(rate)).tag(rate)
                                }
                            }
                            .pickerStyle(.menu)
                            .disabled(model.connected)
                            Text("8N1, без потока. Скорость должна уже совпадать с приёмником. Команду смены скорости программа не шлёт.")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        HStack {
                            Button("Подключить", action: model.connect)
                                .disabled(model.connected || model.connecting)
                            Button("Отключить", action: model.disconnect)
                                .disabled(!model.connected && !model.connecting)
                            Spacer()
                            Text(model.status)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        ErrorLine(text: model.errorText)
                    }
                    .padding(4)
                }
                GroupBox("Сеанс") {
                    VStack(alignment: .leading, spacing: 6) {
                        labeled("Коды ответа", model.reHeader)
                        labeled("Прошивка VR", model.firmware)
                        labeled("Номер RN", model.serialNumber)
                        labeled("Шумодав", model.squelchText)
                    }
                    .padding(4)
                }
                GroupBox("Обзор") {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            TextField("F1, МГц", text: $model.f1)
                                .textFieldStyle(.roundedBorder)
                            TextField("F2, МГц", text: $model.f2)
                                .textFieldStyle(.roundedBorder)
                        }
                        .disabled(model.scanning)
                        Picker("Шаг", selection: $model.stepParameter) {
                            ForEach(ArDv1Step.all) { step in
                                Text(step.title).tag(step.parameter)
                            }
                        }
                        .pickerStyle(.menu)
                        .disabled(model.scanning)
                        Picker("Режим", selection: $model.mode) {
                            Text("FM, без цифры").tag(ArDv1AnalogMode.fm)
                            Text("AM, без цифры").tag(ArDv1AnalogMode.am)
                        }
                        .pickerStyle(.segmented)
                        .disabled(model.scanning)
                        HStack {
                            TextField("Пауза, мс", text: $model.pauseMs)
                                .textFieldStyle(.roundedBorder)
                            TextField("Порог S-метра", text: $model.threshold)
                                .textFieldStyle(.roundedBorder)
                            TextField("Одинаковых подряд", text: $model.stuckLimit)
                                .textFieldStyle(.roundedBorder)
                        }
                        .disabled(model.scanning)
                        Text("Пауза после RF: старт \(ArDv1Timing.startingPauseMilliseconds) мс, на живом приёмнике её нужно подобрать. Одинаковые отсчёты останавливают обзор без команды сброса. Точек: \(pointCount).")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        HStack {
                            if model.scanning {
                                Button("Стоп", action: model.stopScan)
                            } else {
                                Button("Скан", action: model.startScan)
                                    .disabled(!model.connected || model.connecting || model.tuning)
                            }
                            Text(model.live)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(4)
                }
                GroupBox("Трасса") {
                    VStack(alignment: .leading, spacing: 8) {
                        if model.current.isEmpty {
                            Text("Трассы ещё нет")
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, minHeight: 220)
                        } else {
                            TraceChart(
                                current: model.current,
                                minimum: model.minimum,
                                maximum: model.maximum,
                                threshold: parseUserNumber(model.threshold),
                                yTitle: "Отсчёт S-метра"
                            )
                        }
                        Text("Потолок антенного входа приёмника +0 дБм. Отсчёт S-метра в эти единицы не переводится.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if !model.tracePath.isEmpty {
                            Text(model.tracePath)
                                .font(.caption)
                                .textSelection(.enabled)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(4)
                }
                GroupBox("Водопад") {
                    VStack(alignment: .leading, spacing: 8) {
                        WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "отсчёт S-метра")
                        Text("Строка — один законченный свип, свежая сверху. Шкала подстраивается под видимые строки.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(4)
                }
                GroupBox("Превышения") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Частота — середина интервала. Выбор строки останавливает обзор и ставит приёмник на эту частоту. Звук идёт с динамика приёмника.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Table(model.rows, selection: $selection) {
                            TableColumn("Частота, МГц") { row in
                                Text(FrequencyText.formatMHzRussian(from10Hz: row.center))
                            }
                            TableColumn("Ширина, кГц") { row in
                                Text(FrequencyText.formatKHzRussian(from10Hz: row.width))
                            }
                            TableColumn("Попадания") { row in
                                Text(String(row.hits))
                            }
                        }
                        .frame(height: 200)
                    }
                    .padding(4)
                }
                GroupBox("Ответы") {
                    ReplyLog(text: model.journal)
                        .padding(4)
                }
            }
            .padding(16)
        }
        .onAppear(perform: model.refreshPorts)
        .onChange(of: selection) { _, newValue in
            guard let newValue, let row = model.rows.first(where: { $0.id == newValue }) else { return }
            model.stopScanAndSetFrequency(units: row.center)
        }
    }

    private var pointCount: String {
        guard let start = FrequencyText.parseMHz(model.f1),
              let end = FrequencyText.parseMHz(model.f2),
              let step = ArDv1Step.all.first(where: { $0.parameter == model.stepParameter })
        else {
            return "—"
        }
        let count = FrequencyText.grid(from: start, to: end, step: step.unitsOf10Hz).count
        if count > ScanBudget.maximumPoints {
            return "больше \(ScanBudget.maximumPoints)"
        }
        return String(count)
    }

    private func labeled(_ title: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .foregroundStyle(.secondary)
                .frame(width: 120, alignment: .leading)
            Text(value)
                .textSelection(.enabled)
        }
        .font(.body)
    }
}
