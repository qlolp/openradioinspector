import SpektrKomandy
import SwiftUI

struct UltraPane: View {
    @ObservedObject var model: UltraModel
    var handoff: (Int64) -> Void
    @State private var selection: Int?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("tinySA Ultra")
                        .font(.title2.weight(.semibold))
                    Text("Анализатор. Уровень — дБм. LNA и режим ultra в этом срезе не включаются.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                GroupBox("Порт") {
                    VStack(alignment: .leading, spacing: 8) {
                        PortFields(path: $model.portPath, connected: model.connected, ports: model.knownPorts, onRefresh: model.refreshPorts) {
                            Text("Скорость USB-консоли страницами прибора не задана. Для открытия порта взята заглушка termios \(UltraPortBaud.termiosPlaceholder). Её нужно сверить на приборе.")
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
                        caption("info", model.infoText)
                        caption("version", model.versionText)
                        caption("Номер пользователя", model.userNumber)
                        Text("Номер — то, что прибор отдаёт на deviceid. Это не заводской серийный номер.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(4)
                }
                GroupBox("Обзор") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Команды при подключении: \(UltraSessionScript.identityTexts.joined(separator: ", "))."))
                            .font(.caption)
                        Text("Команды обзора:")
                            .font(.caption)
                        Text((UltraSessionScript.setupTexts + [UltraSessionScript.scanText]).joined(separator: "\n"))
                            .font(.system(.caption, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                        Text("Конец строки команды — CR (0x0D). В описании этот байт не задан, на приборе его нужно проверить.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        TextField("Порог, дБм", text: $model.threshold)
                            .textFieldStyle(.roundedBorder)
                            .disabled(model.scanning)
                        Text("Потолок нормального входа при автоаттенюаторе — +0 дБм. Выше него вход не рассчитан. Программа уровень на прибор не отправляет: поле задаёт только линию на графике.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        if model.aboveInputCeiling {
                            Text("Заданный уровень выше +0 дБм. Это линия на графике, не команда анализатору. Такой сигнал уже выше рекомендуемого потолка входа.")
                                .font(.callout)
                                .foregroundStyle(.orange)
                        }
                        Toggle("Текст info или version другой — всё равно слать команды обзора", isOn: $model.forceDespiteIdentity)
                            .disabled(model.scanning)
                            .font(.callout)
                        HStack {
                            if model.scanning {
                                Button("Стоп", action: model.stopOverview)
                            } else {
                                Button("Обзор", action: model.startOverview)
                                    .disabled(!model.connected || model.connecting)
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
                                threshold: model.thresholdDbm,
                                yTitle: "дБм"
                            )
                        }
                        Text("Числа ответа подписаны как дБм. Как именно строка scan с маской 3 ложится в текст, описание не задаёт.")
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
                        WaterfallView(history: model.waterfall, revision: model.waterfallRevision, unitTitle: "дБм")
                        Text("Строка — один ответ scan, свежая сверху. Шкала подстраивается под видимые строки.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(4)
                }
                GroupBox("Превышения") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("Частота — середина интервала, ширина — от первого до последнего пересечения порога. Выбор строки ставит приёмник на эту частоту и не останавливает анализатор.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Table(model.rows, selection: $selection) {
                            TableColumn("Частота, МГц") { row in
                                Text(FrequencyText.formatMHzRussian(fromHertz: row.center))
                            }
                            TableColumn("Ширина, кГц") { row in
                                Text(FrequencyText.formatKHzRussian(fromHertz: row.width))
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
            handoff(row.center)
        }
    }

    private func caption(_ title: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value.isEmpty ? "—" : value)
                .font(.system(.body, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}
