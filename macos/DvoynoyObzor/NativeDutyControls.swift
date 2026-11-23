import Foundation
import SwiftUI
import SpektrKomandy

struct NativeDutyControls: View {
    @ObservedObject var workspace: MonitoringWorkspaceModel
    let verified, canStart: Bool
    @State private var schedule = NativeDutyScheduleDraft()
    @State private var channel = "telegram"
    @State private var enabled = false
    @State private var address = ""
    @State private var secret = ""
    private let days = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    private var stored: MonitoringValue { workspace.operationsSettings["notifications"][channel] }
    private var configuring: Bool { workspace.operationsPending.contains("notification-settings") }
    private var mutating: Bool { workspace.operationsPending.contains("duty") || workspace.pending }
    private var scheduleValidation: String? {
        do { _ = try schedule.value(); return nil } catch { return error.localizedDescription }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Toggle("Звуковой сигнал на этом Mac", isOn: $workspace.localSoundEnabled)
            if workspace.notificationChannelsAvailable { notifications }
            scheduleFields
            if let detail = scheduleValidation { Text(detail).font(.caption).foregroundStyle(.orange) }
            HStack {
                Button("Поставить на дежурство") {
                    guard let value = try? schedule.value() else { return }
                    let resume = schedule.resume
                    workspace.perform { await workspace.startDuty(schedule: value, resume: resume) }
                }.buttonStyle(.borderedProminent).accessibilityIdentifier("preparation.arm")
                    .disabled(!canStart || scheduleValidation != nil || mutating || workspace.dutyEnabled || workspace.isArmed)
                Button("Снять с дежурства") { workspace.perform { await workspace.command("disarm", success: "Дежурство отменено.") } }
                    .disabled(!workspace.projectStatusIsCurrent() || (!workspace.dutyEnabled && !workspace.isArmed) || mutating)
                Text(verified ? status : "Состояние уточняется на узле").font(.caption)
            }
            if !workspace.duty["last_error"].text.isEmpty { ErrorLine(text: workspace.duty["last_error"].text) }
            if workspace.dutyEnabled {
                let active = workspace.duty["schedule"]
                Text("Задание узла · " + scheduleLabel(active) +
                     (workspace.duty["resume_after_restart"].bool == true ? " · возобновление разрешено" : " · возобновление выключено")).font(.caption)
            }
            ForEach(workspace.duty["deliveries"].items.prefix(8).map { $0 }, id: \.exactID) { delivery in
                Text(delivery["created_at"].text + " · " + delivery["channel"].text + " · " + delivery["state"].text +
                    " · попыток " + delivery["attempts"].scalar).font(.caption)
                if !delivery["error"].text.isEmpty { Text(delivery["error"].text).font(.caption).foregroundStyle(.orange) }
            }
        }
        .task { await workspace.refreshDuty() }
        .onChange(of: channel, initial: true) { _, _ in clearChannel() }
        .onChange(of: stored["enabled"].bool) { _, value in enabled = value ?? false }
        .onDisappear { address = ""; secret = "" }
    }

    private var status: String {
        if workspace.isArmed { return "Охрана активна" }
        if workspace.dutyEnabled { return "Дежурство · " + workspace.duty["status"].text }
        return workspace.duty == .null ? "Ожидание состояния задания" : "Дежурство выключено"
    }

    private func scheduleLabel(_ s: MonitoringValue) -> String {
        switch s["mode"].text {
        case "continuous": "постоянно"
        case "until": "до " + s["until_at"].text
        case "weekly": s["weekdays"].items.compactMap { $0.integer.flatMap { (1...7).contains($0) ? days[Int($0)-1] : nil } }.joined(separator: ", ") +
            " · " + s["start_local"].text + "–" + s["end_local"].text + " · " + s["timezone"].text
        default: s["mode"].text
        }
    }

    private var scheduleFields: some View {
        VStack(alignment: .leading) {
            Picker("Дежурство", selection: $schedule.mode) {
                Text("Постоянно").tag("continuous"); Text("До указанного времени").tag("until"); Text("По дням недели").tag("weekly")
            }.pickerStyle(.segmented)
            if schedule.mode == "until" {
                DatePicker("Окончание · \(TimeZone.current.identifier)", selection: $schedule.until)
            } else if schedule.mode == "weekly" {
                HStack {
                    ForEach(days.indices, id: \.self) { i in
                        Toggle(days[i], isOn: Binding(get: { schedule.weekdays.contains(i + 1) }, set: { selected in
                            if selected { schedule.weekdays.insert(i + 1) } else { schedule.weekdays.remove(i + 1) }
                        })).toggleStyle(.button)
                    }
                }
                HStack {
                    TextField("Начало ЧЧ:ММ", text: $schedule.start)
                    TextField("Конец ЧЧ:ММ", text: $schedule.end)
                    TextField("Часовой пояс IANA", text: $schedule.timezone)
                }.textFieldStyle(.roundedBorder)
            }
            Toggle("Возобновлять по расписанию после перезапуска узла", isOn: $schedule.resume)
        }.disabled(workspace.dutyEnabled || workspace.isArmed)
    }

    private func clearChannel() { secret = ""; address = ""; enabled = stored["enabled"].bool ?? false }

    private var notifications: some View {
        DisclosureGroup("Уведомления с узла") {
            VStack(alignment: .leading, spacing: 5) {
                Picker("Канал", selection: $channel) { Text("Telegram").tag("telegram"); Text("Webhook").tag("webhook") }
                    .disabled(configuring)
                Toggle("Включить канал", isOn: $enabled)
                Text(stored["configured"].bool == true ? "Реквизиты сохранены на узле. Заполняйте только изменяемые поля." : "Введите реквизиты доставки.").font(.caption)
                SecureField(channel == "telegram" ? "Chat ID" : "URL webhook", text: $address)
                SecureField(channel == "telegram" ? "Токен бота" : "Секрет webhook", text: $secret)
                HStack {
                    Button("Сохранить канал") {
                        let name = channel
                        var fields: [String: MonitoringValue] = ["enabled": .boolean(enabled)]
                        if !secret.isEmpty { fields[name == "telegram" ? "bot_token" : "secret"] = .string(secret) }
                        if !address.isEmpty { fields[name == "telegram" ? "chat_id" : "url"] = .string(address) }
                        secret = ""; address = ""
                        workspace.perform { await workspace.setNotificationChannel(name, settings: fields) }
                    }.disabled(configuring || stored == .null)
                    Button("Отправить тест") {
                        let name = channel
                        workspace.perform { await workspace.testNotification(name) }
                    }.disabled(stored["enabled"].bool != true || configuring || workspace.operationsPending.contains("notification-test"))
                }
            }.textFieldStyle(.roundedBorder)
        }
    }
}
