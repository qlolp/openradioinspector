import Foundation
import Combine
import AppKit
import AVFoundation
import SwiftUI
import UniformTypeIdentifiers
import SpektrKomandy

private struct MonitoringConnectionIdentity: Hashable {
    let server: PlatformExactID
    let client: ObjectIdentifier?
}

struct MonitoringWorkspacePane: View {
    let server: String
    let client: PlatformClient?
    let sessionID: String
    @ObservedObject var workspace: MonitoringWorkspaceModel
    var mode: NativeWorkspace = .monitoring
    var onProjectSession: (String) -> Void = { _ in }
    var onResearch: () -> Void = {}
    @State private var sessionFollow = MonitoringProjectSessionFollowState()
    @State private var verdict = "unreviewed"
    @State private var verdictComment = ""
    @State private var name = "Наблюдение"
    @State private var site = "Объект"
    @State private var preparationExpanded = true
    @State private var allowedName = "Разрешённый сигнал"
    @State private var allowedStart = "100000000"
    @State private var allowedEnd = "100100000"
    @State private var watchChanges = false
    @State private var manualRole = "receiver"
    @State private var manualFrequency = "100000000"
    @State private var manualMode = "AUTO"
    @State private var searchSpan = "1000000"
    @State private var markLabel = "Точка 1"
    @State private var audioStartFrame = "0"
    @State private var audioEndFrame = ""
    @State private var audioMarkLabel = ""
    @State private var searchExpanded = false
    @State private var historyExpanded = false
    @State private var historyStart = ""
    @State private var historyEnd = ""
    @State private var historyMinHz = ""
    @State private var historyMaxHz = ""
    @State private var historyEventOnly = false

    var body: some View {
        GroupBox(mode == .monitoring ? "Охрана · проект и события" : "Исследование · история и контекст") {
            ScrollView {
              VStack(alignment: .leading, spacing: 10) {
                if mode == .research {
                    historyControls
                    eventControls
                    manualControls
                    searchControls
                } else {
                    eventControls
                }
                if workspace.scanPlansAvailable { MonitoringScanPlanPane(workspace: workspace) }
                preparation
                receiverControls
                allowedControls
                operationsSettingsControls
                detectorControls
                storageControls
                alertControls
                if !workspace.operationsNotice.isEmpty { Text(workspace.operationsNotice).font(.caption) }
                ForEach(workspace.operationsErrors.keys.sorted(), id: \.self) { key in ErrorLine(text: workspace.operationsErrors[key]) }
                ErrorLine(text: workspace.researchError)
                if !workspace.researchPending.isEmpty { ProgressView("Исследование: запрос к ядру…").controlSize(.small) }
                if workspace.pending { ProgressView("Запрос к ядру…").controlSize(.small) }
                if !workspace.notice.isEmpty { Text(workspace.notice).font(.caption) }
                ErrorLine(text: workspace.error)
                Text("Аппаратная проверка не выполнена. Приборы и запись звука обслуживает ядро; закрытие панели не снимает проект с охраны.")
                    .font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Диагностический JSON последнего ответа") {
                    ScrollView { Text(workspace.details).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading) }.frame(maxHeight: 160)
                }
              }
            }
            .frame(maxHeight: mode == .monitoring ? 300 : 440)
        }
        .padding(.horizontal)
        .task(id: MonitoringConnectionIdentity(server: PlatformExactID(server), client: client.map(ObjectIdentifier.init))) {
            workspace.configure(server, client: client); await workspace.load(); await workspace.loadScanPlanCapabilities()
        }
        .onChange(of: PlatformExactID(workspace.selected)) { _, _ in
            workspace.perform { await workspace.refreshStatus(syncConfiguration: true) }
        }
        .onChange(of: workspace.selectedInput) { _, _ in workspace.audioConfirmed = false }
        .onChange(of: workspace.audioEnabled) { _, _ in workspace.audioConfirmed = false }
        .onDisappear { workspace.stopAudio(); workspace.stopOperations() }
        .onChange(of: PlatformExactID(workspace.project["active_session_id"].text)) { _, value in
            let id = value.rawValue
            if let next = sessionFollow.sessionToFollow(endpoint: server, projectID: workspace.selected, activeSessionID: id) { onProjectSession(next) }
        }
        .onChange(of: PlatformExactID(workspace.selectedEvent)) { _, _ in verdict = "unreviewed"; verdictComment = "" }
        .onChange(of: [PlatformExactID(workspace.event["id"].text), PlatformExactID(workspace.event["disposition"].text), PlatformExactID(workspace.event["operator_comment"].text)]) { _, _ in
            let value = workspace.event
            verdict = value["disposition"].text.isEmpty ? "unreviewed" : value["disposition"].text
            verdictComment = value["operator_comment"].text
        }
        .task(id: [PlatformExactID(server), PlatformExactID(workspace.selected)]) {
            let endpoint = server, projectID = workspace.selected
            guard !endpoint.isEmpty, !projectID.isEmpty, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) else { return }
            await workspace.loadOperationsSettings()
            var tick = 0
            while !Task.isCancelled, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) {
                await workspace.pollAlerts()
                guard !Task.isCancelled, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) else { return }
                if workspace.search["active"].bool == true || workspace.coverage == "restoring" {
                    // Keep the narrow series live even when its disclosure is collapsed.
                    await workspace.refreshSearch()
                }
                guard !Task.isCancelled, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) else { return }
                await workspace.refreshStatus()
                guard !Task.isCancelled, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) else { return }
                if tick % 6 == 0 { await workspace.refreshStorage() }
                tick += 1
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
            }
        }
        .task(id: [PlatformExactID(server), PlatformExactID(workspace.selected), PlatformExactID("preflight"), PlatformExactID(String(preparationExpanded))]) {
            let endpoint = server, projectID = workspace.selected
            guard preparationExpanded, !endpoint.isEmpty, !projectID.isEmpty else { return }
            while !Task.isCancelled, workspace.matchesOperations(endpoint: endpoint, projectID: projectID) {
                await workspace.checkPreflight()
                do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
            }
        }
        .task(id: [PlatformExactID(server), PlatformExactID(workspace.selected), PlatformExactID(String(searchExpanded))]) {
            guard searchExpanded, !workspace.selected.isEmpty else { return }
            while !Task.isCancelled {
                if workspace.search["active"].bool != true && workspace.coverage != "restoring" {
                    await workspace.refreshSearch()
                }
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
            }
        }
    }

    private var preparation: some View {
        DisclosureGroup("Подготовка к охране · 4 шага", isExpanded: $preparationExpanded) {
            VStack(alignment: .leading, spacing: 10) {
                Text("1. Выберите сеанс и проект").font(.headline)
                HStack {
                    TextField("Название", text: $name)
                    TextField("Объект", text: $site)
                    Button("Создать") {
                        let projectName = name, projectSite = site, session = sessionID
                        workspace.perform { await workspace.createProject(name: projectName, site: projectSite, sessionID: session) }
                    }.disabled(server.isEmpty || sessionID.isEmpty || workspace.pending)
                    Button("Обновить проекты") { workspace.perform { await workspace.reloadProjects() } }
                        .disabled(server.isEmpty || workspace.pending)
                }
                Picker("Проект", selection: $workspace.selected.exactID) {
                    Text("Не выбран").tag(PlatformExactID(""))
                    ForEach(workspace.projects, id: \.exactID) { p in
                        Text(p["name"].text + " · " + p["site"].text).tag(PlatformExactID(p["id"].text))
                    }
                }
                HStack {
                    Button("Предыдущие проекты") { workspace.perform { await workspace.previousProjects() } }
                        .disabled(server.isEmpty || workspace.pending || workspace.projectPages.pageIndex == 0)
                    Text("Страница проектов \(workspace.projectPages.pageIndex + 1)")
                    Button("Следующие проекты") { workspace.perform { await workspace.nextProjects() } }
                        .disabled(server.isEmpty || workspace.pending || workspace.projectPages.nextCursor.isEmpty)
                }
                Text("До 200 проектов на странице; выбранный проект сохраняется при переходе.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("2. Опишите антенну и подтвердите обстановку").font(.headline)
                TextField("Антенна, тракт приёма, расположение · 1…1000 символов", text: $workspace.antennaNotes, axis: .vertical)
                    .lineLimit(2...4).disabled(!workspace.canEditPreparation)
                Toggle("Подтверждаю подключение и проверку приёмного тракта", isOn: $workspace.receivePathConfirmed).disabled(!workspace.canEditPreparation)
                Toggle("Подтверждаю выбранное расположение и объект наблюдения", isOn: $workspace.locationConfirmed).disabled(!workspace.canEditPreparation)
                Button("Сохранить подготовку") {
                    let notes = workspace.antennaNotes, path = workspace.receivePathConfirmed, location = workspace.locationConfirmed
                    workspace.perform { await workspace.savePreparation(notes: notes, receivePath: path, location: location) }
                }.disabled(!workspace.canEditPreparation || !workspace.preparationInputValid || workspace.operationsPending.contains("preparation"))
                if workspace.preparationSimulation {
                    Text("Симуляторы выбраны явно: физические подтверждения не требуются. Результат не является аппаратной проверкой.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Text("Для serial-профиля сохраните описание и оба подтверждения. Это заявления оператора; приложение не проверяет антенну физически.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if workspace.preparationDirty { Text("Есть несохранённые изменения подготовки.").font(.caption) }
                Text("3. Соберите данные обычной обстановки").font(.headline)
                HStack {
                    Button("Запустить приём и сбор эталона") { workspace.perform { await workspace.startPreparation() } }
                        .disabled(!workspace.canStartPreparation)
                    Button("Остановить сбор") { workspace.perform { await workspace.stopPreparation() } }
                        .disabled(!workspace.isCollecting || workspace.isArmed || workspace.operationsPending.contains("preparation"))
                    Button("Обновить готовность") { workspace.perform { await workspace.checkPreflight() } }
                        .disabled(workspace.selected.isEmpty || workspace.operationsPending.contains("preflight"))
                }
                Text("Запуск выполняется только по кнопке через ядро. Генератор RF не включается. Закрытие раздела останавливает только опрос прогресса; потоки ядра продолжают приём.")
                    .font(.caption).foregroundStyle(.secondary)
                baselineProgress
                Text("4. Подтвердите обычную обстановку и сохраните эталон").font(.headline)
                HStack {
                    Toggle("Подтверждаю обычную обстановку", isOn: $workspace.baselineConfirmed)
                    Button("Зафиксировать эталон") {
                        let body: [String: MonitoringValue] = ["name": .string(name), "location": .string(site), "confirm": .boolean(workspace.baselineConfirmed)]
                        workspace.perform { await workspace.command("baselines", body: body, success: "Эталон сохранён.") }
                    }.disabled(workspace.project["scan_plan"] != .null || !workspace.baselineConfirmed || workspace.isArmed || workspace.preflight["baseline_collection"]["ready"].bool != true || (!workspace.preparationSimulation && (!workspace.savedPreparationReady || workspace.preparationDirty)))
                    Button("Анализ новых свипов") { workspace.perform { await workspace.command("analyze", success: "Анализ новых свипов выполнен.") } }
                }.disabled(workspace.selected.isEmpty || workspace.pending || workspace.operationsPending.contains("preparation"))
                if workspace.preflight != .null {
                    Text(workspace.preflight["ready"].bool == true ? "Проверки подготовки: успешно" : "Проверки подготовки: требуется внимание")
                    Text("Сохранённый эталон: \(workspace.preflight["baseline_ready"].bool == true ? "готов" : "не создан")").font(.caption)
                    ForEach(Array(workspace.preflight["checks"].items.enumerated()), id: \.offset) { _, check in
                        Text("\(check["ok"].bool == true ? "✓" : "⚠") \(check["detail"].text)").font(.caption)
                    }
                }
            }
        }
    }

    private var baselineProgress: some View {
        let collection = workspace.preflight["baseline_collection"]
        return VStack(alignment: .leading, spacing: 4) {
            if collection == .null { Text("Прогресс сбора ещё не получен от ядра.").font(.caption).foregroundStyle(.secondary) }
            else if !collection["bands"].items.isEmpty {
                Text("Подтверждайте эталон каждого поддиапазона в разделе «План последовательного обзора».").font(.caption)
            }
            else {
                Text("Свипы: \(collection["sweeps"].scalar) / \(collection["minimum_sweeps"].scalar) · длительность: \(collection["elapsed_seconds"].scalar) / \(collection["minimum_seconds"].scalar) с").font(.caption)
                ProgressView(value: min(collection["sweeps"].double ?? 0, max(1, collection["minimum_sweeps"].double ?? 20)), total: max(1, collection["minimum_sweeps"].double ?? 20))
                ProgressView(value: min(collection["elapsed_seconds"].double ?? 0, max(1, collection["minimum_seconds"].double ?? 60)), total: max(1, collection["minimum_seconds"].double ?? 60))
                Text(collection["ready"].bool == true ? "Данные для эталона собраны. Требуется подтверждение оператора." : "Дождитесь требуемого числа свипов и полного интервала.").font(.caption)
            }
        }
    }

    private var receiverControls: some View {
        GroupBox("Приёмник и автономная охрана") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button("Обновить состояние") { workspace.perform { await workspace.refreshStatus() } }
                        .disabled(workspace.selected.isEmpty || workspace.pending)
                    Button("Поставить на охрану") { workspace.perform { await workspace.command("arm", success: "Команда постановки на охрану выполнена.") } }
                        .disabled(!workspace.canArm)
                    Button("Снять с охраны") { workspace.perform { await workspace.command("disarm", success: "Проект снят с охраны.") } }
                        .disabled(workspace.selected.isEmpty || workspace.pending)
                }
                Text("Покрытие: " + coverageLabel(workspace.coverage))
                    .font(.headline).foregroundStyle(workspace.coverage == "wide" ? Color.primary : Color.orange)
                if workspace.status == .null {
                    Text("Состояние неизвестно: запросите его у ядра.").foregroundStyle(.secondary)
                } else {
                    Text("Состояние проекта: \(label(workspace.project["state"].text)) · охрана: \(workspace.status["active"].bool == true ? "активна" : "не активна")")
                    Text("Приёмник: \(workspace.status["receiver_busy"].bool == true ? "исследует событие" : "свободен") · аудиовход: \(workspace.status["audio_configured"].bool == true ? "назначен" : "не назначен")").font(.caption)
                    ErrorLine(text: workspace.status["error"].text)
                    ForEach(Array(workspace.status["workers"].items.enumerated()), id: \.offset) { _, worker in
                        Text("Исполнитель \(worker["stream_id"].text): \(label(worker["state"].text)) · \(worker["last_error"].text)").font(.caption)
                    }
                }
                HStack {
                    Picker("Режим приёмника", selection: $workspace.receiverMode) {
                        ForEach(workspace.receiverModes, id: \.self) { Text($0).tag($0) }
                    }.frame(width: 240).disabled(!workspace.canConfigure)
                    Button("Обновить аудиовходы ядра") { workspace.perform { await workspace.loadAudioInputs() } }
                        .disabled(server.isEmpty || workspace.pending)
                }
                Text("AUTO и режимы AR-DV1 задаются явно; TETRA не входит в AUTO.").font(.caption).foregroundStyle(.secondary)
                Toggle("Записывать аналоговый звук AR-DV1", isOn: $workspace.audioEnabled).disabled(!workspace.canConfigure)
                if workspace.audioInputs["available"].bool != true {
                    Text("Аудиовходы недоступны: \(workspace.audioInputs["reason"].text.isEmpty ? "список ещё не получен" : workspace.audioInputs["reason"].text)")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if workspace.audioEnabled {
                    Picker("Аудиовход на сервере", selection: $workspace.selectedInput) {
                        Text("Выберите вход явно").tag("")
                        ForEach(Array(workspace.devices.enumerated()), id: \.offset) { _, device in
                            Text("\(device["index"].scalar) · \(device["name"].text)").tag(device["index"].scalar)
                        }
                    }.disabled(!workspace.canConfigure)
                    Toggle("Подтверждаю, что выбран аналоговый выход AR-DV1", isOn: $workspace.audioConfirmed)
                        .disabled(!workspace.canConfigure || workspace.chosenDevice == nil)
                }
                Button("Сохранить режим и аудиовход") {
                    guard let body = workspace.configurationBody else { return }
                    workspace.perform { await workspace.saveConfiguration(body) }
                }
                    .disabled(!workspace.canSaveConfiguration)
                if workspace.configurationDirty { Text("Есть несохранённые настройки; сохраните их перед постановкой на охрану.").font(.caption) }
            }
        }
    }

    private var allowedControls: some View {
        GroupBox("Разрешённые сигналы") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Название сигнала", text: $allowedName)
                    TextField("От, Гц", text: $allowedStart)
                    TextField("До, Гц", text: $allowedEnd)
                    Button("Разрешить") {
                        let body: [String: MonitoringValue] = ["name": .string(allowedName), "start_hz": .integer(Int64(allowedStart) ?? -1), "end_hz": .integer(Int64(allowedEnd) ?? -1), "watch_changes": .boolean(watchChanges)]
                        workspace.perform { await workspace.command("allowed", body: body, success: "Разрешённый сигнал сохранён.") }
                    }
                }
                Toggle("Наблюдать изменения разрешённого сигнала", isOn: $watchChanges)
            }.disabled(workspace.selected.isEmpty || workspace.pending)
        }
    }

    private var operationsSettingsControls: some View {
        DisclosureGroup("Настройки проекта · автозапуск и уведомления") {
            VStack(alignment: .leading, spacing: 8) {
                Button("Прочитать сохранённые настройки") { workspace.perform { await workspace.loadOperationsSettings() } }
                Toggle("Автоматически запускать охрану после старта ядра", isOn: $workspace.autostart)
                Text("Автозапуск по умолчанию выключен. Изменение применяется только кнопкой сохранения.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Text("Резерв свободного места, байт")
                    TextField("268435456", text: $workspace.archiveReserve).frame(width: 150)
                    Text("Минимальное хранение, секунд")
                    TextField("3600", text: $workspace.archiveRetention).frame(width: 120)
                }
                Toggle("Включить доставку webhook", isOn: $workspace.webhookEnabled)
                TextField("URL webhook, https://…", text: $workspace.webhookURL)
                SecureField("Новый секрет; пустое поле сохраняет действующий", text: $workspace.webhookSecret).privacySensitive()
                Text("Секрет в ядре: \(workspace.operationsSettings["webhook"]["secret_configured"].bool == true ? "настроен" : "не настроен"). Поле очищается после попытки сохранения; секрет не выводится в диагностике.")
                    .font(.caption).foregroundStyle(.secondary)
                Button("Сохранить настройки проекта") {
                    guard let body = workspace.operationsSettingsBody else { return }
                    workspace.perform { await workspace.saveOperationsSettings(body: body) }
                }.disabled(workspace.operationsSettings == .null || workspace.operationsSettingsBody == nil)
                if workspace.operationsSettingsBody == nil {
                    Text(workspace.operationsSettingsValidation ?? "Проверьте настройки проекта.").font(.caption).foregroundStyle(.orange)
                }
                if workspace.operationsSettings["detector"] != .null {
                    DisclosureGroup("Параметры детектора · только чтение") {
                        Text(workspace.operationsSettings["detector"].pretty).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                }
            }.disabled(workspace.selected.isEmpty || workspace.operationsPending.contains("settings"))
        }
    }

    private var detectorControls: some View {
        DisclosureGroup("Детектор · сбалансированный инженерный профиль") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    detectorField("Порог, dB", text: $workspace.detectorMargin)
                    detectorField("Импульс, dB", text: $workspace.detectorImpulse)
                    detectorField("Гистерезис, dB", text: $workspace.detectorHysteresis)
                }
                HStack {
                    detectorField("Подтверждения", text: $workspace.detectorConfirmations)
                    detectorField("Окно, свипы", text: $workspace.detectorWindow)
                    detectorField("Прослушивание, с", text: $workspace.detectorDwell)
                }
                HStack {
                    Button("Заполнить сбалансированные значения") { workspace.balancedDetectorDraft() }
                    Button("Сохранить только детектор") {
                        guard let draft = try? workspace.detectorDraft() else { return }
                        workspace.perform { await workspace.saveDetector(draft) }
                    }.disabled(workspace.operationsSettings == .null || workspace.detectorValidation != nil)
                }
                ErrorLine(text: workspace.detectorValidation)
                Text("Начальные значения: 6 / 12 / 3 dB, 3 подтверждения из 5, 10 с прослушивания. Это инженерная отправная точка, не аппаратная калибровка. Кнопка заполнения не сохраняет значения. Изменения разрешены только после снятия с охраны.")
                    .font(.caption).foregroundStyle(.secondary)
            }.disabled(!workspace.canConfigure || workspace.operationsPending.contains("settings"))
        }
    }

    private func detectorField(_ title: String, text: Binding<String>) -> some View {
        HStack { Text(title).font(.caption); TextField(title, text: text).frame(width: 80).textFieldStyle(.roundedBorder) }
    }

    private var storageControls: some View {
        DisclosureGroup("Хранение архива · готовность") {
            VStack(alignment: .leading, spacing: 6) {
                Button("Обновить хранение") { workspace.perform { await workspace.refreshStorage() } }
                    .disabled(workspace.selected.isEmpty || workspace.operationsPending.contains("storage"))
                if workspace.storage == .null { Text("Состояние хранения не получено.").foregroundStyle(.secondary) }
                else {
                    Text(workspace.storage["ready"].bool == true ? "Хранилище: проверка ядра пройдена" : "Архив не готов: проверьте резерв и требуемый срок")
                        .foregroundStyle(workspace.storage["ready"].bool == true ? Color.primary : Color.orange)
                    Text("Свободно в файловой системе: \(workspace.storage["filesystem_free_bytes"].scalar) байт")
                    Text("Резерв: \(workspace.storage["minimum_free_bytes"].scalar) байт · доступно: \(workspace.storage["available_bytes"].scalar) байт")
                    Text("Темп записи по данным ядра: \(workspace.storage["bytes_per_second"].scalar) байт/с")
                    Text("Оценка хранения: \(workspace.storage["estimated_retention_seconds"] == .null ? "нет достаточных данных" : workspace.storage["estimated_retention_seconds"].scalar + " с") · требуется: \(workspace.storage["required_retention_seconds"].scalar) с")
                    Text("Размер архива: \(workspace.storage["capacity"]["database_bytes"].scalar) байт · состояние: \(workspace.storage["capacity"]["state"].scalar)")
                }
                Text("Автоматическое удаление выключено. Состояние обновляется чтением каждые 30 секунд.").font(.caption).foregroundStyle(.secondary)
            }.font(.caption)
        }
    }

    private var alertControls: some View {
        DisclosureGroup("Оповещения · оператор и доставка") {
            VStack(alignment: .leading, spacing: 8) {
                Toggle("Звук новых оповещений в этом окне", isOn: $workspace.localSoundEnabled)
                    .disabled(workspace.selected.isEmpty)
                Text("Звук по умолчанию выключен; первая полученная страница звучать не будет. Опрос каждые 5 секунд работает и при свёрнутых разделах. При смене проекта звук снова выключается.")
                    .font(.caption).foregroundStyle(.secondary)
                HStack {
                    Button("Последние оповещения") { workspace.perform { await workspace.showLatestAlerts() } }
                    Button("Следующая страница") {
                        let cursor = workspace.alertsNext
                        workspace.perform { await workspace.alertPage(after: cursor) }
                    }.disabled(workspace.alertsNext.isEmpty)
                }.disabled(workspace.selected.isEmpty || workspace.operationsPending.contains("alerts-page"))
                Text(workspace.alertsLatest ? "Последние оповещения · обновляются автоматически" : "Архивная страница · новые оповещения продолжают отслеживаться")
                    .font(.caption)
                if workspace.operationsSettings["webhook"]["enabled"].bool != true {
                    Text("Доставка webhook выключена в сохранённых настройках.").font(.caption).foregroundStyle(.secondary)
                }
                if workspace.alerts.isEmpty { Text("Оповещений нет или страница ещё не получена.").font(.caption) }
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(workspace.alerts, id: \.exactID) { item in
                            alertRow(item)
                        }
                    }
                }.frame(maxHeight: 230)
            }
        }
    }

    private func alertRow(_ item: MonitoringValue) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(severityLabel(item["severity"].text)) · \(item["message"].text)").font(.headline)
            Text("\(item["kind"].text) · UTC \(item["occurred_at"].text.isEmpty ? item["created_at"].text : item["occurred_at"].text)").font(.caption)
            Text("Доставка: \(deliveryLabel(item["delivery"]["state"].text)) · попыток: \(item["delivery"]["attempts"].scalar) · следующий повтор UTC: \(item["delivery"]["next_attempt_at"].scalar)").font(.caption)
            ErrorLine(text: item["delivery"]["last_error"].text)
            HStack {
                if item["acknowledged_at"] == .null {
                    Button("Подтвердить оповещение") {
                        let id = item["id"].text
                        workspace.perform { await workspace.acknowledgeAlert(id) }
                    }.disabled(workspace.operationsPending.contains("alerts-ack"))
                } else { Text("Подтверждено UTC \(item["acknowledged_at"].text)").font(.caption) }
                if !item["event_id"].text.isEmpty {
                    Button("Открыть событие") {
                        let id = item["event_id"].text
                        workspace.perform { await workspace.showEvent(id) }
                    }.disabled(workspace.pending)
                }
            }
            Divider()
        }
    }

    private func severityLabel(_ value: String) -> String {
        ["info": "Информация", "warning": "Внимание", "error": "Ошибка", "critical": "Критично"][value] ?? value
    }
    private func deliveryLabel(_ value: String) -> String {
        ["disabled": "выключена", "pending": "в очереди", "retry": "ожидает повтора", "sending": "отправляется", "sent": "доставлено", "delivered": "доставлено", "failed": "ошибка", "exhausted": "попытки исчерпаны"][value] ?? (value.isEmpty ? "нет данных" : value)
    }

    private var eventVerdictControls: some View {
        GroupBox("Решение оператора") {
            VStack(alignment: .leading, spacing: 6) {
                Picker("Вердикт", selection: $verdict) {
                    Text("Не рассмотрено").tag("unreviewed")
                    Text("Разрешённый сигнал").tag("authorized")
                    Text("Ложное срабатывание").tag("false_positive")
                    Text("Исследовать").tag("investigate")
                }
                TextField("Комментарий оператора", text: $verdictComment)
                Button("Сохранить решение") {
                    let id = workspace.selectedEvent, disposition = verdict, comment = verdictComment
                    workspace.perform { await workspace.reviewEvent(eventID: id, disposition: disposition, comment: comment) }
                }.disabled(workspace.selectedEvent.isEmpty || workspace.operationsPending.contains("event-review"))
            }
        }
    }

    private var manualControls: some View {
        DisclosureGroup("Ручное управление · право на 30 секунд") {
            VStack(alignment: .leading, spacing: 8) {
                Picker("Прибор", selection: $manualRole) {
                    Text("AR-DV1 · приёмник").tag("receiver")
                    Text("tinySA · анализатор").tag("analyzer")
                }.frame(width: 280)
                TimelineView(.periodic(from: Date(), by: 1)) { context in
                    let lease = workspace.leases[manualRole]
                    let valid = lease.map { $0.expiresAt > context.date } ?? false
                    VStack(alignment: .leading, spacing: 6) {
                        Text(lease == nil ? "Ручное право не получено в этом окне." : valid ? "Осталось \(max(0, Int(lease!.expiresAt.timeIntervalSince(context.date)))) с" : "Срок права истёк: прибор остаётся на паузе.")
                        HStack {
                            Button("Взять управление") {
                                let role = manualRole
                                workspace.perform { await workspace.manual("acquire", role: role) }
                            }.disabled(!workspace.isArmed || lease != nil)
                            Button("Продлить право") {
                                let role = manualRole
                                workspace.perform { await workspace.manual("renew", role: role) }
                            }.disabled(!valid)
                            Button("Вернуть охране / восстановить обзор") {
                                let role = manualRole
                                workspace.perform { await workspace.manual("return", role: role) }
                            }.disabled(lease == nil)
                        }
                    }
                }
                HStack {
                    TextField("Частота AR-DV1, целые Гц", text: $manualFrequency)
                    Picker("Режим настройки", selection: $manualMode) {
                        ForEach(workspace.receiverModes, id: \.self) { Text($0).tag($0) }
                    }.frame(width: 150)
                    Button("Настроить вручную") {
                        let frequency = manualFrequency, mode = manualMode
                        workspace.perform { await workspace.tuneManually(frequency: frequency, mode: mode) }
                    }.disabled(workspace.leases["receiver"] == nil)
                }
                Text("Продление и возврат выполняются явно. Истечение срока оставляет прибор на паузе и не воспроизводит прежние команды. Если право потеряно, снимите проект с охраны. Права хранятся только в памяти этого окна и очищаются при смене проекта/сервера.")
                    .font(.caption).foregroundStyle(.secondary)
            }.disabled(workspace.selected.isEmpty || workspace.pending || workspace.researchPending.contains("control"))
        }
    }

    private var searchControls: some View {
        DisclosureGroup("Поиск источника · узкая полоса", isExpanded: $searchExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Полоса поиска, целые Гц", text: $searchSpan)
                    Button("Искать источник выбранного события") {
                        let id = workspace.selectedEvent, span = searchSpan
                        workspace.perform { await workspace.startSearch(eventID: id, span: span) }
                    }.disabled(workspace.selectedEvent.isEmpty || workspace.leases["analyzer"] == nil || workspace.pending || workspace.researchPending.contains("control"))
                    Button("Состояние поиска") { workspace.perform { await workspace.refreshSearch() } }
                        .disabled(workspace.selected.isEmpty || workspace.researchPending.contains("search"))
                }
                Text("Покрытие: \(coverageLabel(workspace.coverage))")
                    .font(.headline).foregroundStyle(workspace.coverage == "wide" ? Color.primary : Color.orange)
                Text("Узкий поиск приостанавливает широкий обзор. Кривая обновляется чтением каждые 5 секунд; продление права выполняется явно. Для возврата явно верните анализатор охране и дождитесь покрытия «широкий обзор».")
                    .font(.caption).foregroundStyle(.secondary)
                if workspace.search != .null {
                    ErrorLine(text: workspace.search["error"].text)
                    Text("Событие \(workspace.search["event_id"].scalar) · центр \(workspace.search["center_frequency_hz"].scalar) Гц · полоса \(workspace.search["span_hz"].scalar) Гц").font(.caption)
                    MonitoringCurveChart(curves: [.init(title: "Пик свипа · dBm", color: .primary, points: workspace.search["series"].items.compactMap { item in
                        guard let time = item["started_at"].utcDate, let level = item["peak_level"].double, level.isFinite else { return nil }
                        return PlotPoint(x: time.timeIntervalSince1970, y: level)
                    }), .init(title: "Метки обхода", color: .red, points: workspace.search["marks"].items.compactMap { item in
                        guard let time = item["created_at"].utcDate, let level = item["level"].double, level.isFinite else { return nil }
                        return PlotPoint(x: time.timeIntervalSince1970, y: level)
                    }, connectsPoints: false)], timeAxis: true)
                    Text("Максимальный уровень по данным ядра: \(workspace.search["maximum_level"].scalar) dBm")
                    HStack {
                        TextField("Подпись точки обхода", text: $markLabel)
                        Button("Поставить метку") {
                            let label = markLabel
                            workspace.perform { await workspace.markSearch(label: label) }
                        }.disabled(workspace.leases["analyzer"] == nil || workspace.pending || workspace.researchPending.contains("control"))
                    }
                    DisclosureGroup("Метки обхода (\(workspace.search["marks"].items.count))") {
                        ScrollView {
                            LazyVStack(alignment: .leading) {
                                ForEach(Array(workspace.search["marks"].items.enumerated()), id: \.offset) { _, mark in
                                    Text("\(mark["label"].text) · UTC \(mark["created_at"].text) · \(mark["level"].scalar) dBm · \(mark["frequency_hz"].scalar) Гц").font(.caption)
                                }
                            }
                        }.frame(maxHeight: 130)
                    }
                }
            }
        }
    }

    private var historyControls: some View {
        DisclosureGroup("История · временной водопад и спектр", isExpanded: $historyExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Начало UTC, ISO 8601 Z", text: $historyStart)
                    TextField("Конец UTC, ISO 8601 Z", text: $historyEnd)
                }
                HStack {
                    TextField("От, целые Гц (необязательно)", text: $historyMinHz)
                    TextField("До, целые Гц (необязательно)", text: $historyMaxHz)
                    Toggle("Только выбранное событие", isOn: $historyEventOnly)
                }
                HStack {
                    Button("Загрузить историю") {
                        let start = historyStart, end = historyEnd, minHz = historyMinHz, maxHz = historyMaxHz
                        let id: String? = historyEventOnly ? workspace.selectedEvent : nil
                        workspace.perform { await workspace.loadHistory(start: start, end: end, minHz: minHz, maxHz: maxHz, eventID: id) }
                    }.disabled(workspace.selected.isEmpty || (historyEventOnly && workspace.selectedEvent.isEmpty))
                    Button("Следующая страница") { workspace.perform { await workspace.nextHistoryPage() } }.disabled(workspace.historyNext.isEmpty)
                }.disabled(workspace.researchPending.contains("history"))
                Text(workspace.historyDescription).font(.caption)
                if !workspace.history.isEmpty {
                    MonitoringHistoryTimeline(rows: workspace.history, selected: workspace.selectedSweepID, select: workspace.selectSweep)
                    Picker("Свип истории", selection: Binding(get: { workspace.selectedSweepID }, set: workspace.selectSweep).exactID) {
                        ForEach(workspace.history, id: \.exactID) { row in Text("\(MonitoringCurveChart.utc(row.startedAt.timeIntervalSince1970)) · \(row.id)").tag(row.exactID) }
                    }
                }
                if let sweep = workspace.selectedSweep {
                    Text("\(sweep.displayOnly ? "Экранные точки ядра" : "Исходный архивный свип") · \(sweep.levels.count)/\(sweep.originalPointCount) точек · \(sweep.id)")
                    MonitoringCurveChart(curves: [.init(title: "Выбранный спектр · dBm", color: .primary,
                        points: zip(sweep.frequenciesHz, sweep.levels).map { PlotPoint(x: Double($0.0), y: $0.1) })])
                    HStack {
                        Button("Исходный свип") {
                            let id = workspace.selectedSweepID
                            workspace.perform { await workspace.loadSelectedSweep(sweepID: id, comparison: false) }
                        }
                        Button("Было / сейчас · эталон") {
                            let id = workspace.selectedSweepID
                            workspace.perform { await workspace.loadSelectedSweep(sweepID: id, comparison: true) }
                        }
                    }.disabled(workspace.researchPending.contains("sweep"))
                    if workspace.rawDetail != .null {
                        DisclosureGroup("Настройки исходного свипа") { Text(workspace.rawDetail["settings"].pretty).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }
                    }
                }
                if workspace.baselineComparison != .null { comparisonControls }
                Text("До 64 свипов × 320 экранных точек. Максимумы сохраняет ядро; экранные точки не заменяют исходные измерения. Вертикальная ось — фактическое UTC, пропуски оставлены пустыми. Для исходных значений загрузите отдельный свип.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var comparisonControls: some View {
        let value = workspace.baselineComparison
        return VStack(alignment: .leading, spacing: 6) {
            Text("Эталон \(value["baseline_id"].scalar) · детектор \(value["detector_version"].scalar)").font(.caption)
            if value["compatible"].bool == true {
                MonitoringCurveChart(curves: [
                    comparisonCurve(value, field: "baseline_p50_dbm", title: "Было · p50", color: .blue),
                    comparisonCurve(value, field: "baseline_p95_dbm", title: "Было · p95", color: .orange),
                    comparisonCurve(value, field: "current_dbm", title: "Сейчас · dBm", color: .primary)
                ])
                DisclosureGroup("Разность по данным ядра · dB") {
                    ScrollView {
                        LazyVStack(alignment: .leading) {
                            ForEach(Array(value["items"].items.enumerated()), id: \.offset) { _, point in
                                Text("\(point["frequency_hz"].scalar) Гц · Δ \(point["delta_db"].scalar) dB").font(.caption)
                            }
                        }
                    }.frame(maxHeight: 140)
                }
            } else { Text("Сравнение недоступно: \(value["reason"].text)").foregroundStyle(.orange) }
        }
    }

    private func comparisonCurve(_ value: MonitoringValue, field: String, title: String, color: Color) -> MonitoringCurve {
        .init(title: title, color: color, points: value["items"].items.compactMap { point in
            guard let frequency = point["frequency_hz"].integer, let level = point[field].double, level.isFinite else { return nil }
            return PlotPoint(x: Double(frequency), y: level)
        })
    }

    private func coverageLabel(_ value: String) -> String {
        ["wide": "широкий обзор", "unavailable": "широкое покрытие недоступно", "preparation": "сбор данных эталона — охрана ещё не включена", "stopped": "остановлено", "error": "ошибка покрытия", "narrow": "узкая полоса — широкий обзор приостановлен", "restoring": "восстанавливается широкий обзор", "paused": "пауза — требуется явный возврат или снятие с охраны"][value] ?? "не подтверждено ядром"
    }

    private var eventControls: some View {
        GroupBox("События и записи") {
            VStack(alignment: .leading, spacing: 8) {
                Button("Обновить события") { workspace.perform { await workspace.loadEvents() } }
                    .disabled(workspace.selected.isEmpty || workspace.pending)
                if workspace.events.isEmpty { Text("Список событий пуст или ещё не загружен.").font(.caption) }
                ScrollView {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(workspace.events, id: \.exactID) { item in
                            Button { workspace.perform { await workspace.showEvent(item["id"].text) } } label: {
                                VStack(alignment: .leading) {
                                    Text("\(label(item["kind"].text)) · \(item["center_frequency_hz"].scalar) Гц · \(label(item["state"].text))")
                                    Text("UTC \(item["last_seen"].text) · приёмник: \(label(item["analysis"]["state"].text))").font(.caption)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.disabled(workspace.pending)
                        }
                    }
                }.frame(maxHeight: 160)
                if workspace.event != .null { eventDetails }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var selectedAudioRange: MonitoringAudioRange? {
        guard let start = Int64(audioStartFrame), let end = Int64(audioEndFrame) else { return nil }
        return workspace.audioRange(start: start, end: end)
    }
    private var audioEvidenceControls: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Отметки и фрагменты фонограммы").font(.headline)
            Button("Загрузить сведения и отметки") { workspace.perform { await workspace.loadAudioEvidence() } }
                .disabled(workspace.researchPending.contains("audio-evidence"))
            if workspace.audioEvidenceInfo != .null {
                Text("\(workspace.audioEvidenceInfo["frame_count"].scalar) отсчётов · \(workspace.audioEvidenceInfo["sample_rate_hz"].scalar) Гц").font(.caption)
                HStack {
                    TextField("Начальный отсчёт", text: $audioStartFrame)
                    TextField("Конечный отсчёт (не включён)", text: $audioEndFrame)
                }
                if let range = selectedAudioRange, let rate = Double(workspace.audioEvidenceInfo["sample_rate_hz"].scalar) {
                    Text(String(format: "%.3f…%.3f с", Double(range.start)/rate, Double(range.end)/rate)).font(.caption).monospacedDigit()
                }
                TextField("Подпись отметки", text: $audioMarkLabel)
                HStack {
                    Button("Сохранить аудиоотметку") {
                        guard let range = selectedAudioRange else { return }
                        let label = audioMarkLabel
                        workspace.perform { await workspace.saveAudioMark(range, label: label) }
                    }.disabled(selectedAudioRange == nil || audioMarkLabel.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || workspace.researchPending.contains("audio-mark"))
                    Button("Экспортировать фрагмент WAV") {
                        guard let range = selectedAudioRange else { return }
                        let panel = NSSavePanel(); panel.allowedContentTypes = [UTType(filenameExtension: "wav") ?? .audio]
                        panel.nameFieldStringValue = "ori-audio-clip.wav"
                        if panel.runModal() == .OK, let destination = panel.url {
                            workspace.perform { await workspace.exportAudioClip(range, to: destination) }
                        }
                    }.disabled(selectedAudioRange == nil || workspace.researchPending.contains("audio-export"))
                }
                Text("Фрагмент содержит исходные отсчёты, ID, SHA-256 и контекст записи. Оригинал остаётся в архиве.").font(.caption).foregroundStyle(.secondary)
                ForEach(Array(workspace.audioMarks.enumerated()), id: \.offset) { _, mark in
                    Button("\(mark["label"].text) · \(mark["start_frame"].scalar)…\(mark["end_frame"].scalar)") {
                        audioStartFrame = mark["start_frame"].scalar; audioEndFrame = mark["end_frame"].scalar
                    }.disabled(!PlatformExactID.same(mark["source_artifact_id"].text, workspace.audioEvidenceInfo["artifact_id"].text) || mark["source_sha256"].text != workspace.audioEvidenceInfo["sha256"].text)
                }
                HStack {
                    Button("Первая страница отметок") { workspace.perform { await workspace.loadAudioEvidence() } }
                    Button("Следующая страница отметок") { let after = workspace.audioMarksNext; workspace.perform { await workspace.loadAudioEvidence(after: after) } }
                        .disabled(workspace.audioMarksNext.isEmpty)
                }.disabled(workspace.researchPending.contains("audio-evidence"))
            }
        }
        .onChange(of: workspace.audioEvidenceInfo) { _, value in
            audioStartFrame = "0"; audioEndFrame = value["frame_count"].scalar; audioMarkLabel = ""
        }
    }

    private var eventDetails: some View {
        let event = workspace.event
        let analysis = event["analysis"]
        return VStack(alignment: .leading, spacing: 6) {
            Text("Событие \(event["id"].text)").font(.headline)
            eventVerdictControls
            Text("\(label(event["kind"].text)) · \(event["start_frequency_hz"].scalar)…\(event["end_frequency_hz"].scalar) Гц")
            Text("Уровень \(event["peak_level"].scalar) dBm · превышение \(event["peak_excess_db"].scalar) dB")
            Text("UTC: \(event["first_seen"].text) — \(event["last_seen"].text)").font(.caption)
            Text("Исследование: \(label(analysis["state"].text)) · запрошенный режим: \(analysis["requested_mode"].scalar)")
            ErrorLine(text: analysis["error"].text)
            ErrorLine(text: analysis["reason"].text)
            ErrorLine(text: analysis["audio_error"].text)
            ForEach(Array(analysis["classifications"].items.enumerated()), id: \.offset) { _, item in
                Text("Декодер: \(item["mode"].text) · источник: \(item["source"].text)").font(.caption)
            }
            ForEach(Array(analysis["receiver_status"].items.enumerated()), id: \.offset) { _, item in
                Text("AR-DV1: \(item["frequency_hz"].scalar) Гц · \(item["level"].scalar) \(item["level_unit"].text) · шумодав: \(item["squelch_open"].scalar)").font(.caption)
            }
            if analysis["audio"] != .null {
                Text("Архивная фонограмма WAV: \(analysis["audio"]["filename"].text)").font(.caption)
                HStack {
                    Button("Воспроизвести WAV") { workspace.perform { await workspace.playAudio() } }.disabled(!workspace.canPlay)
                    Button("Стоп") { workspace.stopAudio() }.disabled(!workspace.hasPlayback && !workspace.audioLoading)
                    Text(workspace.audioNotice).font(.caption)
                }
                Text("Загрузка до 16 МиБ через API ядра.").font(.caption).foregroundStyle(.secondary)
                if workspace.audioEvidenceAvailable { audioEvidenceControls }
            } else { Text("Записи WAV для события нет.").font(.caption) }
            Button("Контекст события в истории") {
                historyExpanded = true
                onResearch()
                historyStart = event["first_seen"].text
                historyEnd = event["last_seen"].text
                historyEventOnly = true
                let id = workspace.selectedEvent, start = historyStart, end = historyEnd
                workspace.perform { await workspace.loadHistory(start: start, end: end, minHz: "", maxHz: "", eventID: id) }
            }.disabled(workspace.researchPending.contains("history"))
            HStack {
                Button("Источники события") {
                    let id = workspace.selectedEvent
                    workspace.perform { await workspace.loadSources(eventID: id, next: false) }
                }
                Button("Следующие источники") {
                    let id = workspace.selectedEvent
                    workspace.perform { await workspace.loadSources(eventID: id, next: true) }
                }.disabled(workspace.sourcesNext.isEmpty)
            }.disabled(workspace.researchPending.contains("sources"))
            ForEach(Array(workspace.sources.enumerated()), id: \.offset) { _, source in
                Text("Свип \(source["sweep_id"].text) · сеанс \(source["session_id"].text) · поток \(source["stream_id"].text)").font(.caption)
            }
            DisclosureGroup("JSON выбранного события") {
                ScrollView { Text(event.pretty).font(.system(.caption, design: .monospaced)).textSelection(.enabled) }.frame(maxHeight: 160)
            }
        }.textSelection(.enabled)
    }

    private func label(_ value: String) -> String {
        ["draft": "подготовка", "collecting": "сбор данных эталона", "ready": "готов", "armed": "на охране", "searching": "поиск источника", "degraded": "частичная неисправность", "error": "ошибка",
         "running": "работает", "stopped": "остановлен", "connected": "подключён",
         "persistent": "устойчивый сигнал", "impulse": "импульс", "broadband_change": "широкополосное изменение",
         "authorized_changed": "изменение разрешённого сигнала", "open": "открыто", "closed": "закрыто",
         "queued": "в очереди", "observed": "наблюдался приёмником", "not_present_at_receiver": "не обнаружен приёмником",
         "no_observations": "нет наблюдений", "expired": "исчез до перестройки", "interrupted": "прервано",
         "failed": "не выполнено"][value] ?? (value.isEmpty ? "нет данных" : value)
    }
}
