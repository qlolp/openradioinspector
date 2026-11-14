# Personal macOS RC service

`0.4.0-macos.rc.1` is an arm64 candidate for a MacBook Air with 16+ GB.
The target macOS 27.3 installation and physical acceptance are still required.
It does not promote Rust to the stable default or accept Pi/Windows hardware.

The installed app registers `org.openradioinspector.macos.rc.core.plist` with SMAppService.
Registration/approval, starting the core, connecting devices and arming guard
are separate actions. The agent starts idle at login; RC1 requires an explicit
core start. It does not automatically arm guard. Closing the window or
Command-Q disconnects the UI; it sends no shutdown command to the service.
Opening the UI again verifies the private owner and reconnects to that core.

The agent runs the bundled `ori-supervisor macos-agent`. Only its selected
`ori-core --mode live --allow-serial` owns physical ports and the SQLite writer.
The supervisor retains the core’s stdin lifeline: supervisor death closes it and requests core shutdown; the UI never owns that pipe. There is no Python/simulator fallback. Its client API is loopback-only at
`http://127.0.0.1:8772`; service start/status/shutdown use a separate owner-only
Unix socket, not the public HTTP API. The socket is in the canonical OS-provided
per-user temporary directory, under a hash of the canonical data path. This
avoids Darwin's short socket-path limit even with a long home-directory name.
Both socket mode and peer UID are checked. Requests have bounded length and
deadlines; cancellation closes only the requesting worker's own descriptor.

Data live in `~/Library/Application Support/OpenRadioInspector/macOS-RC`,
separate from the legacy installation. The initial archive quota is 64 GiB;
guard preparation must still verify actual free space and the eight-hour
storage/backup budget. No automatic original-measurement deletion is enabled.

**Stop service** first sends private Stop (the agent remains idle and recoverable if the UI disappears), then waits for the core's real shutdown, device release, writer
lease and listeners before acknowledging and unregistering the LaunchAgent.
An error is not proof of release: do not update the installed app until stop is
confirmed. RC refuses private backend cutover; update/rollback stop the service
first and use the latest archive without restoring an older copy over it.

While actual guard is armed, IOPM prevents automatic idle system sleep. It does
not override lid closure or manual sleep. Each physical transport compares
Darwin continuous time with absolute uptime before every read/write. A detected
sleep discontinuity latches that connection unusable. Independently, the
supervisor records `system_sleep_coverage_gap` in its private coverage journal,
joins the core, and remains idle with `system_sleep_preflight_required`.
Start and prepare instruments again after wake. Failure to establish idle-sleep
protection stops the core visibly. Real lid/sleep/USB tests remain physical gates.

The UI labels native audio capture and Python/DuckDB analysis unavailable in
RC1. Existing source spectra, observation IDs, integer Hz, UTC, raw S-meter
readings and receive-only guard rules remain unchanged.

Local Linux checks exercise owned backend processes, private socket framing,
idle registration, explicit/idempotent start, independent reconnect, refused
cutover and acknowledged resource release. Swift/SMAppService/IOPM integration
requires native CI and installed-package acceptance; these checks do not replace
the 30-minute Mac workload or the eight-hour two-instrument protocol.

## First connection wizard

The personal RC offers **Prepare instruments**. Select two distinct `/dev/cu.…`
ports, the object and an integer-Hz receive range. Port suggestions are not
model identification. The conservative profile accepts 0.1–800 MHz, 2–290
points and RBW 10/30 kHz. Profile creation opens no USB port. AR-DV1 uses the
explicit `ardv1-monitor-v1` profile; AUTO excludes TETRA.

Confirm the antenna, receive path and location, then explicitly start collection.
The core validates model/firmware and acknowledges generator-off/input commands.
The wizard displays actual firmware, sweep duration and the observed start-to-start
cycle from archived measurements. Command acknowledgement is not physical RF
readback. At least 60 seconds and 20 full compatible sweeps are required; the
operator separately confirms the reference environment.

Before making the baseline armable, the wizard stores a 43,200-second primary
budget (eight hours plus 50%) with `archive.reserve_backup=true`. The core reserves
the current database plus the projected 43,200-second growth for a future backup,
in addition to the 256-MiB free-space floor. It recalculates this reservation from
the measured archive rate during preflight/guard checks; an unknown rate,
overflow, failed write probe or insufficient filesystem/quota space refuses
readiness. Automatic deletion remains false. The optional policy is additive;
projects without it keep their existing storage semantics.

Saving a baseline does not arm guard. The final button requires current core
preflight and budget readiness. A new range/profile requires a new baseline.
Closing the wizard stops only its polling. Explicit stop collection, disarm
and stop service remain separate actions. The RC uses a compact initial layout
for the MacBook Air; advanced session controls remain in a disclosure panel.

## Отдельный arm64-пакет

`bash packaging/macos/build.sh --personal-rc` собирает `0.4.0-macos.rc.1`.
Сборщику нужны macOS/Xcode и закреплённый Rust 1.99.1; установленному приложению
не нужны Python, Homebrew, Xcode, Node или интернет. Обычная ветка сборщика
сохраняет прежний Python-пакет для совместимости. RC использует только SwiftUI,
`ori-core` с `serial` без `audio` и `ori-supervisor`, все — arm64 с минимальной
системой macOS 14. Целевая аппаратная приёмка остаётся macOS 27.3.

Исходники должны быть закоммичены. Сборщик отказывается перезаписывать каталог
результатов; другой каталог задаётся `--output-dir`. Сначала подписываются
вложенные Rust-программы, затем записывается ограниченный манифест состава и
подписывается приложение. Применяется ad-hoc-подпись; Developer ID и notarization
не заявлены. Финальный список SHA-256 хранится **снаружи** подписанного `.app`,
чтобы избежать самоссылки подписи и контрольной суммы. Проверяются архитектуры,
динамические зависимости, лицензии Cargo/Rust и отсутствие исключённых runtime.

Результат: `.app`, DMG, ZIP приложения, внешний манифест и `SHA256SUMS`.
Идентификатор приложения `org.openradioinspector.macos.rc` отличен от прежней
установки. Внутренний build-info связывает исходный SHA, оба подписанных helper,
архитектуру и параметры сборки. Приватный ответ службы содержит идентификатор
этого build-info. UI отказывается подключаться к другой сборке; уже запущенную
службу можно явно остановить. Перед новым Start агент повторно проверяет файлы.
Замена работающего пакета не поддерживается.

`OpenRadioInspectorRC --self-check` проверяет подпись и состав без регистрации
службы, USB и записи архива. Установка службы с read-only DMG отклоняется:
сначала скопируйте приложение в «Программы». Для личного ad-hoc RC используйте
штатное «Конфиденциальность и безопасность → Всё равно открыть», проверив
источник и контрольные суммы. Отключать Gatekeeper не требуется.

В CI отдельное задание создаёт пакет, переносит `.app` за пределы checkout,
проверяет его с системным PATH, запускает оба `--build-info` и native self-check.
Затем пакетное ядро сохраняет синтетические исходники в отдельный каталог,
корректно завершается и читает холодную восстановленную копию. Online backup
с манифестом и реальный restore проверяются общим Rust gate отдельно. Проверка
пакета не регистрирует SMAppService и не выдаётся за установочную приёмку
macOS 27.3 или физический прогон. Артефакты и диагностика сохраняются также при
ошибке. Публичный GitHub Release и tag автоматически не создаются.

Полный отдельный протокол RC1 и команды нагрузки без аудио: [приёмка macOS RC1](release/macos-0.4.0-rc1-acceptance.md). Headless-успех не закрывает нативную и физическую приёмку.

### Привязка UI к проверенному экземпляру

Приватный статус службы определяет ID конкретного Rust-процесса. RC-клиент
проверяет тот же ID через HTTP и отправляет **каждый** API/SSE/WebSocket-запрос
через добавочный namespace `/api/v1/instances/{runtime_instance_id}/…`.
Другой экземпляр отклоняет такой запрос до изменения состояния. Это сохраняет
защиту и между двумя проверками приватного статуса; простая проверка адреса
127.0.0.1 или новый заголовок, который старый сервер мог бы проигнорировать,
для этого недостаточны. Исходные `/api/v1/*` маршруты сохранены для прежних
клиентов. Namespace не предоставляет команды управления службой и наследует
аутентификацию, строгий framing/JSON и специальные ограничения размера тела.
При любой смене владельца UI сбрасывает клиент, потоки и поколение запросов;
запоздалый результат предыдущего экземпляра не получает новое подключение.
Подключение RC через неподтверждённый legacy-клиент запрещено.

При постановке на охрану измеренная оценка потока preflight кратко переносится
в новый сеанс с тем же подтверждённым эталоном. Она заменяется потоком новых
свипов. Стартовое окно — минимум 30 секунд, до двух измеренных периодов +10
секунд (не более 7230 секунд для предельно медленного профиля). После него
отсутствие измеренного потока снова блокирует бюджет; рост служебных страниц
SQLite сам по себе не считается скоростью свипов. Основа не сохраняется на
диск и не делает неизмеренный проект готовым. Резерв backup и свободное место
продолжают проверяться во время этого окна.
