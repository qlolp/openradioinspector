# Полная поддержка AR-DV1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** реализовать весь подтверждаемый охват AR-DV1 в общем ядре и трёх клиентах, сохранив видимыми ограничения конкретной прошивки и аппаратуры.

**Architecture:** только Python worker владеет serial; типизированные операции проходят через его очередь. Один общий профиль описывает документированную, реализованную и проверенную поддержку. UI не генерирует wire-команды и не пишет аппаратную память самостоятельно.

**Tech Stack:** Python 3.10+, существующие serial transports/SQLite/HTTP, WebUI JavaScript, Windows Tk, macOS SwiftUI/Swift SDK.

**Spec:** [официальные возможности и ограничения](../../ardv1/README.md); [125 идентификаторов](../../ardv1/command-matrix.csv). База исследования — `aa23f1a83e98586936d06f2a41940f7de0d66fa9`. Это план, код функций ниже ещё не реализован.

## Global Constraints

- Частоты в общем контракте хранятся в целых Гц, время — в UTC с началом и окончанием измерения. Производные значения отделяются от исходных.
- Не преобразовывать отсчёты S-метра AR-DV1 в дБм без калибровочного профиля.
- Только служба ядра владеет приборами и пишет в архив. Действие с AR-DV1 не останавливает tinySA и наоборот.
- Существующие `/api/*` и `/api/v1/*` сохраняются; новые поля и маршруты добавляются совместимо.
- Документация не включает функцию автоматически: model/firmware/region/options и реализованный драйвер проверяются отдельно.
- Нет свободного raw-command endpoint, автоматического повтора мутаций, автоматического reset/restore/activation или скрытого изменения baud.
- Сохранять taint после неопределённого обмена и требование нового stop/start. Ключи опций и полные серийные номера не публикуются.
- Аппаратный PASS требует реального стенда на точном SHA/firmware. Синтетический transcript не выдаётся за ответ прибора.

## Review Focus

- Ранее включённые LC/LT/RT/DJ вклинивают `10…` между `21…20`; ожидается разделение события и ответа, а не ложное подтверждение записи. Регрессия задачи 1.
- Неизвестная прошивка, T-suffix и американская версия: профиль объясняет ограничения, сохраняет исходную строку и не включает опции догадкой. Регрессии задач 1, 3, 7.
- Потеря ACK после аппаратной записи: нельзя повторно записать EEPROM или объявить успех; возможен частичный результат. Регрессии задач 4, 6, 8.
- Смена клиента, сеанса, stream или поколения запроса во время операции: поздний ответ не меняет новую панель, stop не блокирует Tk. Регрессии задач 2, 5, 9.
- Часы прибора без timezone и прерывистая SD-запись: DST/полночь и разрывы не превращаются в непрерывную UTC-аудиодорожку. Регрессии задач 6, 8, 9.

---

## Разбиение и общий интерфейс

Каждая задача — отдельный проверяемый PR; крупные перечисления внутри задачи
можно делить по готовой функции, сохраняя интерфейсы ниже. Сначала 1 → 2,
после них 3/4/8 независимы; 5 зависит от 3/4; 6 — от 4; 7 — от 3;
9 — от 2 и выбранного аудиоисточника; 10 завершает аппаратную приёмку.
Существующая P1.1 нестабильность runner остаётся отдельной задачей, а не
поводом ослаблять проверки при реализации этого плана.

Основной gateway задачи 2: `POST /api/v1/sessions/{sid}/streams/{stream_id}/receiver/control`.
Payload: `operation`, `parameters`, `context_revision`, `request_id`.
Обычные операции возвращают `200` с `operation_id`, `state`, `confirmed`,
`profile_revision`, `data`; длительные — `202` с теми же полями и последующим
`GET …/receiver/operations/{operation_id}`. Отмена — отдельный
`POST …/receiver/operations/{operation_id}/cancel`. Operation ID принадлежит
конкретным sid/stream/generation; повторный request_id читает тот же результат,
а не повторяет аппаратную запись. После рестарта старые IDs не возобновляют мутации.

Новые payload/schema и операции описываются в `docs/ardv1/api.md` в том же PR,
который их реализует. Состояния: queued/running/succeeded/failed/cancelled/unknown.
Unknown означает возможное изменение прибора без достоверного ACK/readback;
это не success и не повод автоматически повторять действие.

### Task 1: корректный протокол, профиль и честные capabilities

**Files:** Create `radioinspector/ardv1_protocol.py`, `radioinspector/ardv1_profile.py`, `tests/test_ardv1_protocol.py`, `tests/test_ardv1_profile.py`; modify `radioinspector/serial_instruments.py`, `radioinspector/device_workers.py`, `docs/devices.md`, `radioinspector/web/static/serial-session.js`, `radioinspector/windows/app.py`, `macos/SpektrKomandy/Sources/SpektrKomandy/PlatformAPI.swift`. Существующие тесты — `tests/test_serial_instruments.py`, `tests/test_serial_session_validation.py`, `tests/webui_serial_session.cjs`, Swift `ArDv1FrameTests.swift`.

**Interfaces:** `parse_line(raw: str) -> Reply` с raw/body/result_kind/continued; `ReplyCollector(command: str)` с `feed(reply: Reply)` и результатом только после terminal response. `DeviceProfile` содержит model/firmware_raw/region/options и список feature evidence; неизвестные поля явно unknown. Adapter details добавляет `ardv1_profile`, доступный в существующем worker status без I/O.

- [ ] Написать failing tests: `10` не подтверждает RF; `10LM0551` не становится результатом запроса LM; `21VI…`, `10LM…`, `20VI…` дают полный VI и отдельное событие; ошибки 30/40/50/60, malformed ASCII, усечённый ответ и отсутствие terminal помечают соединение tainted.
- [ ] Добавить тесты unknown firmware и строки с T-suffix: исходник сохранён, неподтверждённые опции не включены. Профиль не рекламирует физический audio capture или применение squelch, которых адаптер не выполняет.
- [ ] Выполнить `python -m unittest tests.test_ardv1_protocol tests.test_ardv1_profile tests.test_serial_instruments` и получить ожидаемые FAIL до реализации.
- [ ] Реализовать single-flight collector с отдельной очередью событий и пределами строк/байтов/времени. Пределы и timeout по классу ответа фиксируются тестами; при переполнении не оставлять хвост для следующей команды. Префикс RE доказан C:4; RE1 bootstrap проверяется отдельно, без слепого retry.
- [ ] Проверить parser и profile на synthetic transport, реальные worker status HTTP и profiles трёх клиентов; исправить комментарий Swift о документированном префиксе. Запустить полный release runner и оформить PR с точным перечнем пока доступных функций.

### Task 2: VFO, аналоговые настройки и общий пульт трёх клиентов

**Files:** Create `radioinspector/receiver_control.py`, `radioinspector/windows/receiver_control.py`, `radioinspector/web/static/receiver-control.js`, `macos/SpektrKomandy/Sources/SpektrKomandy/PlatformReceiver.swift`, `macos/DvoynoyObzor/ReceiverControlPane.swift`, `docs/ardv1/api.md`, соответствующие Python/Node/Swift tests; modify worker manager, HTTP server, `windows/app.py`, platform.html, `PaneChrome.swift` и Xcode source registration.

**Interfaces:** `ReceiverControlRequest` — поля gateway выше; `DeviceWorkerManager.receiver_operation(sid, stream_id, payload) -> dict` передаёт типизированную операцию в `worker.submit`. Реестр операций в `receiver_control.py` задаёт validators, допустимые режимы и destructive/read-only свойства. Общий UI-контракт состояния использует `profile_revision` и `context_revision`.

- [ ] Failing tests: A/B/Z; FM/AM/SAH/SAL/USB/LSB/CW; RF 100000 и 1300000000 Гц для подтверждённого полного профиля; отказ на bool, float и 11 Гц вне сетки; US cellular-blocked профиль отклоняет запрещённую частоту до I/O. Точные региональные интервалы берутся из подтверждённого профиля, не угадываются. ST/SH, mode-specific IF; SQ/NQ/LQ/VQ, CTCSS/DCS, AGC/RF gain, notch/NR, offset/priority. CN00 и DS000 response-only; DCS проверяется по таблице, не сплошному диапазону.
- [ ] Реализовать операции задач 1–2 из matrix: частоту читать RF/RX после настройки; хранить requested, acknowledged, readback отдельно. Не выполнять отложенный readback с новым timestamp как старое измерение.
- [ ] Реализовать gateway и три контроллера с единым JSON-контрактом, ограниченными запросами, отменой и инвалидированием поздних ответов. Минимум один реальный HTTP round-trip на каждый тип операции; чужие sid/stream/operation_id отвергаются.
- [ ] Проверить отсутствие повторной мутации после потери ответа и повторного request_id, смену stream во время ответа/диалога, stop и независимость tinySA. Неизвестные/неподдержанные настройки показывают причину, а не молча игнорируются.
- [ ] Выполнить полный runner и macOS `python -m tests.run_release_checks --swift` через CI; обычный native `swift test` без исключения physical smoke не использовать как equipment-free проверку. Зафиксировать вертикальный срез PR.

### Task 3: цифровые режимы, metadata и фильтры

**Files:** Create `radioinspector/ardv1_digital.py`, `tests/test_ardv1_digital.py`, общие синтетические fixtures; modify profile, operation registry, observation metadata и три receiver-контроллера задачи 2.

**Interfaces:** `decode_status(reply: Reply) -> DigitalStatus`; status хранит selected_mode, detected_mode, raw message, configured и received CC/RAN/NAC, busy flags и UTC интервал. Добавочные metadata совместимы с существующим `ReceiverObservation`.

- [ ] Failing tests: AUTO и явные 1–7; текущие filters OT0–3, CC0–16, NC0–64, PC000–FFF; config и received не подменяют друг друга. MDB bits не интерпретируются как speech при data-only; неизвестные bits/raw messages сохраняются.
- [ ] Реализовать MD/MDB/DJ/DK и фильтры по C/D/S, включая события DJ. Не декодировать неизвестные поля догадкой; добавить отсутствующие wire-примеры только с явной пометкой synthetic.
- [ ] В профиле/трёх UI показать совместимость из Spec: Yaesu VW от 2412A; P25 Phase1; NXDN6.25; dPMR Tier1; DMR Tier1/2. WC и запуск auto-descramble без подтверждённого setter отображаются manual-only, не отправляются как WC-команда.
- [ ] Проверить реальные HTTP handlers и три контроллера: поздний digital status не относится новому RF/stream; raw S-meter не превращается в dBm. Полный runner, Swift/native CI, PR.

### Task 4: аппаратная память и редактор банков

**Files:** Create `radioinspector/ardv1_memory.py`, `tests/test_ardv1_memory.py`; extend typed gateway, controller sections и API-документ.

**Interfaces:** `MemorySnapshot` содержит банки, каналы, raw values и snapshot revision; `MemoryChangeSet` — source revision, explicit writes/deletes и результат по каждому элементу. `preview_memory_changes(snapshot, changes)` ничего не пишет; `apply_memory_changes` выполняется только worker и возвращает confirmed/failed/unknown по элементам.

- [ ] Failing tests: 40×50 nominal, теги ≤12 документированных символов, пустые каналы, защищённый банк/канал, MA continuation, MM21→20. Физические номера/специальные банки не выводить только из nominal count: неизвестные адреса сохраняются и не используются для записи до fixture.
- [ ] Реализовать MA/MX/MW/MR/MB/MQ/MG/MP/PT/MM; copy/move — через preview и явные read/write/readback/delete. При неизвестном исходе остановить оставшиеся записи и сохранить recovery journal, не заявлять атомарность.
- [ ] Тестировать потерю ACK после фактической записи, остановку в середине группы, задержанное сохранение S *1–*5, повтор request_id, существующее назначение. Источник move удаляется только после подтверждения назначения.
- [ ] Добавить UI памяти трёх клиентов и совместимое отображение неполного результата; полный runner/CI, PR.

### Task 5: scan/search и наблюдение во время автономной работы приёмника

**Files:** Create `radioinspector/ardv1_search.py`, `tests/test_ardv1_search.py`; extend gateway, observation readback и три контроллера.

**Interfaces:** `SearchConfiguration` содержит VFO/bank/group, limits_hz, step, modes, delay/free/pass и auto_store; `ReceiverOperatingState` явно различает vfo/vfo_search/program_search/memory/memory_scan. Данные берутся из RX/MD, не из последней ручной tune-команды.

- [ ] Failing tests: VS/VE; SE/SR/SS/SX и 40 nominal search banks; SG/BK; PR multi-line, PW/PD; DL hold100 и FR0; no banks/unregistered/protected; switch search→VFO и stop без остановки tinySA.
- [ ] Реализовать старт/остановку и readback состояния; при изменении RF в scan/search правильно обновлять источник частоты и UTC наблюдения. AS включается явно и пишет только аппаратную память.
- [ ] Предотвратить совместное управление scan/host handoff одним receiver: операции упорядочены и конфликт виден. TETRA/non-TETRA mix отвергается до записи; TETRA доступна после задачи 7.
- [ ] Проверить поздние RX/events после остановки и смены UI-контекста; реальные HTTP/клиентские тесты, полный runner, PR.

### Task 6: SD, backup/restore и файлы памяти

**Files:** Create `radioinspector/ardv1_sd.py`, `radioinspector/ardv1_backup.py`, `tests/test_ardv1_sd.py`, `tests/test_ardv1_backup.py`; extend gateway и три контроллера.

**Interfaces:** `SDStatus` различает no_card/busy/full/unusable/recording/playing/idle; `BackupArtifact` содержит model/firmware/schema/raw bytes/checksum и provenance. Existing phonogram storage не заменяется файлом аппаратного backup.

- [ ] Failing tests: SD DIR21→20, INF/PST, NOCARD/CARDBUSY/CARDFULL/FAT12, REC/PLY stop; никакая тишина/пустой ответ сама по себе не подтверждает запись — проверять PST. Неоднозначный stop format C сначала подтвердить transcript.
- [ ] Реализовать SD status/record/playback/skip и backup/restore через typed jobs. Restore — только после preview и проверенной копии текущего состояния, с per-item/unknown результатом при прерывании.
- [ ] Для MY…/MZ сначала получить полный формат/терминатор и эталонные файлы. Пока их нет — capability с причиной unsupported_protocol; не выдавать название команды из S за готовый transfer.
- [ ] Добавить offline memch.csv/system.csv import/export: model/schema validation, точные Гц, теги, unknown fields, 8-character file names. Проверить round-trip и повреждения без прибора. Прикладной signal-list import остаётся отдельным форматом.
- [ ] Проверить отмену длительного transfer, доступ других клиентов, read-only неизменность, DST/UTC и разрывы squelch skip. Не обещать получение WAV-байтов через USB. Полный runner/CI, PR.

### Task 7: TETRA и GSSI PRO

**Files:** Create `radioinspector/ardv1_tetra.py`, `tests/test_ardv1_tetra.py`; extend profile, digital/search validators и три контроллера.

**Interfaces:** `TetraState` — inactive/vfo_ttc/gssi_running; `GSSIEntry` хранит ID как точное число/строку, mute, name и source; списки bookmarks и active GSSI различаются. Activation key — transient secret input, не часть snapshot или event.

- [ ] Failing tests: T-DM1609D, T-TC1812A, TS1903A, GSSI2101C с опцией и без; AUTO не выбирает TETRA. Неизвестная лицензия не считается активной по догадке из firmware suffix.
- [ ] Реализовать MD8/9, TS0–4; TK STR только VFO/T-TC, TK STP перед сменой режима. FAA/GSA собираются до terminal; 50 bookmarks и 200 active entries, без принятия terminator как дополнительной ячейки.
- [ ] До отправки TK GSS/TK GS и FAX TT/TTT получить проверенный формат. HOM setter не включать для AR-DV1. Не делать activation во время подключения; SK/TK KEY — отдельная явно инициированная операция, ключ не логируется, power cycle подтверждается на стенде. После отключения DC часы и таймеры считаются неподтверждёнными: тест доказывает инвалидирование, последующую синхронизацию/readback и отсутствие скрытого повторного включения расписаний.
- [ ] Тестировать удаление одного/всех, пределы 00–49/000–199, потерю ACK, disabled option, смену режима при GSSI. Реальные HTTP/контроллеры, full runner/CI, PR. TETRA hardware проверяет задача 10.

### Task 8: системные функции, таймеры и scope

**Files:** Create `radioinspector/ardv1_system.py`, `radioinspector/ardv1_scope.py`, соответствующие tests; extend typed jobs, profile и UI.

**Interfaces:** `ReceiverClock` хранит raw wall-clock и явно выбранный timezone; `ReceiverTimer` не смешивается с host control_scheduler. `ScopeFrame` хранит raw counts, grid provenance и interval; никакой выдуманной частотной сетки.

- [ ] Реализовать AG/BP/LB/LD/LN/LU/KL, ZI, статус питания, sleep и часы. Для QP/ZP/SB/PT/RS — preview контекста/последствий и отдельное действие, без auto-retry. Не использовать full reset как recovery по умолчанию.
- [ ] TR реализовать только после разрешения TY/XE и времени/номеров групп по transcript; тесты на 3 группы, полночь, недопустимую дату, DST, timer-active command restrictions. Неизвестный timezone не превращать в UTC.
- [ ] FD/GL включать только после проверки входа в scope, числа bins и сетки; тесты на 21/20, `/`, malformed count и taint. До подтверждения доступна лишь объясняющая capability, а не синтетический «живой спектр».
- [ ] IF direction, dial/key-lock, DCR WC и firmware update показать как manual-only, если remote setter не установлен. Firmware helper выбирает .DV1/.DV2 по официальной таблице, но не прошивает автоматически; USA 09560xxx не угадывается.
- [ ] Реальные HTTP и UI tests, full runner/CI, PR.

### Task 9: физический аудиоисточник и воспроизведение

**Files:** Create `radioinspector/receiver_audio.py`, `tests/test_receiver_audio.py`; modify существующие `audio.py`, `audio_stream.py`, `phonogram.py` и клиентские audio controllers, дополняя платформенными output backends.

**Interfaces:** `AudioCaptureSource` выдаёт bounded blocks с sample_rate/channels/PCM format/source/stream_id/UTC; adapter выбирается пользователем по физическому аудиовходу ядра, а не по serial-порту AR-DV1. Playback sink клиента независим от capture и архива.

- [ ] Сначала определить доступный input/output backend по платформам, используемый выход AR-DV1 и согласование уровней. Без этого профиль сообщает capture unavailable; serial PCM не симулируется как аппаратный источник.
- [ ] Failing tests: генератор известных PCM-блоков, несовпадающие sample rates, WAV header с фактической частотой, пропуски, cancel, slow subscriber, disconnect. Принятые архивные буферы не перезаписываются producer.
- [ ] Реализовать capture на стороне ядра и привязку к stream, затем playback WebUI/Windows/macOS с ограниченной очередью. Смена контекста и отмена освобождают input/output/subscription без блокировки UI и без остановки соседнего прибора.
- [ ] Проверить реальные HTTP/audio controllers и loopback отдельно от RF; full runner/CI, PR. Физическая приёмка и уровни — задача 10.

### Task 10: аппаратное закрытие матрицы и подтверждение поставок

**Files:** Update `docs/ardv1/command-matrix.csv`, `docs/ardv1/README.md`, `docs/release/hardware-acceptance.md`, `docs/release/status.md`; add отдельный явно запускаемый AR-DV1 bench runner и synthetic tests его lifecycle.

**Interfaces:** `AcceptanceRecord` связывает SHA, firmware/model/region/options, transport/OS, feature ID, expected/observed, artifact checksum и результат. Raw паспорт, лицензионный ключ и частные измерения остаются вне Git.

- [ ] Пройти всю матрицу: на каждой строке работающий проверенный путь либо ограничение с доказательством. Непроверенные аппаратные строки остаются NOT_RUN, не PASS. Спорные протоколы не открываются общим флагом experimental.
- [ ] Проверить USB/COM Linux/Windows/macOS, штатный EX/local, смену baud, отключение/подключение, taint, stop/start и независимость tinySA; убедиться, что read-only запросы не меняют память.
- [ ] Проверить все поддержанные режимы/фильтры, memory/search/SD round-trip, GSSI только при наличии опции, capture/playback и запись с gap metadata. Напряжение/ослабление RF и audio bench задаются перед физическим прогоном владельцем стенда.
- [ ] Выполнить восьмичасовой Pi + tinySA/AR-DV1 + два удалённых клиента; сверить исходные Hz/UTC, sequence, gaps, RSS, SQLite и recovery. Непроверенную RF accuracy не объявлять калиброванной.
- [ ] Проверить wheel/sdist/Windows/macOS packages и чистые install/update/rollback вне checkout, сохранность архива, full release matrix Python3.10/3.12/3.14 и Swift/native/unsigned Xcode. Обновить итоговую матрицу/план; релиз отдельным решением.

## Самопроверка охвата

Все 125 command identifiers назначены задачам 1–8 в CSV. Аудио без serial-команды
покрывает задача 9, manual-only и firmware — задача 8, отсутствующие форматы —
явные capability blockers задач 6–8. Аппаратное доказательство для всех задач
даёт задача 10. Каждый UI-срез обязан иметь HTTP и три controller проверки;
generic command console не заменяет функциональную интеграцию.
