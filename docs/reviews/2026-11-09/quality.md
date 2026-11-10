# Ревью инженерного качества OpenRadioInspector

- **Дата:** 2026-11-09 (Europe/Moscow), ревью task-4.
- **SHA:** `054eb59fdd949c2c73b0be87ed4966ac86f054d9` — «Merge pull request #120 from qlolp/docs/ardv1-full-capabilities» (2026-11-09 19:18 UTC).
- **Окружение:** Windows 11, Python 3.11.9, numpy 1.26.4, ruff 0.16.8, mypy 2.3.1, Node 24.16.0.
- **Метод:** только чтение и запуск команд. Исходники репозитория не изменялись (`git status --porcelain` пуст). Полные журналы прогонов: `%TEMP%\ori_suite_full.log`.
- **Что не проверялось:** реальный стенд с tinySA/AR-DV1, живой статус GitHub Actions (сети до api.github.com нет), сборка EXE/DMG/установщиков (Windows-пакет требует Inno Setup и полного набора шагов).

## 1. Ключевые метрики

| Метрика | Команда | Результат |
|---|---|---|
| ruff | `python -m ruff check radioinspector tests` (и `--isolated` — результат идентичен) | **1221** находка; `radioinspector` — 758, `tests` — 463 |
| ruff, топ-правил | `--statistics` | UP045 345, I001 162, C408 133, **F401 88**, BLE001 75, **B023 73**, SIM117 70, TRY004 48, UP037 45, UP035 35, **PLW1510 31**, RUF100 30, S110 13, F811 1 |
| mypy | `python -m mypy radioinspector --ignore-missing-imports` | **213 ошибок в 27 файлах** из 111 проверенных; без флага — 215/28 |
| mypy, коды | `--show-error-codes` | attr-defined 115, arg-type 37, union-attr 21, assignment 15, index 10 |
| mypy, топ-файлов | то же | `serial_instruments.py` 64, `web/server.py` 38, `windows/app.py` 29, `task_scheduler.py` 12, `devices/scpi.py` 9 |
| Аннотации | `mypy --disallow-untyped-defs` | **327** определений без аннотаций типов |
| compileall | `python -m compileall -q radioinspector` и `tests` | exit 0, синтаксических/кодировочных проблем нет |
| Код | подсчёт файлов/строк | `radioinspector` — 111 файлов / **28 940** строк; `tests` — 154 файла / **29 596** строк (≈1:1) |
| Тесты | `python -m unittest discover -s tests -t .` | **1518 тестов**, 252–303 с, **62 skip**, на дефолтной локали Windows: **16 не прошли** (3 failures + 13 errors) |
| Тест-методы | статический подсчёт | **1424** `test*`-метода, **5782** assert-вызова, `assert True` — **0**, без прямых assert — 16 |
| Моки/потоки | статический подсчёт | 65 файлов с `mock/patch/Mock` (**468** упоминаний); 81 файл с `Thread/sleep/socket/HTTPServer` |
| Крупнейшие файлы | подсчёт строк | `windows/app.py` **1649**, `macos/.../ContentView.swift` **1639**, `session_service.py` **1590**, `web/server.py` **1573**, `platform.js` **1313**, `PlatformAPI.swift` **1136**, `session_store.py` 1066 |
| CI | `.github/workflows/*.yml` | 2 workflow: `windows-core.yml` (матрица 3 ОС × Python 3.10/3.12/3.14, 9 job) и `desktop-packages.yml` (wheel/sdist + Windows EXE + macOS DMG) |
| Качество-гейты CI | grep по workflow | **нет** запуска ruff, mypy, coverage/pytest; нет lock-файла; нет конфигурации ruff/mypy/coverage в `pyproject.toml` (файл 77 строк, `[tool.*]` только setuptools) |
| Упаковка | `importlib` + `pyproject.toml` | 9/9 entry points резолвятся; `__version__ == 0.4.0.dev0 == pyproject.version` |

Примечание к ruff: в репозитории нет `[tool.ruff]`/`ruff.toml`, поэтому набор правил не зафиксирован и зависит от версии ruff; `--isolated` даёт те же 1221, т.е. это не подхваченный чужой конфиг. E-коды (pycodestyle) в выводе отсутствуют — шум это не стиль строк, а расширенные правила.

### 1.1. Выборочная проверка заявлений README и docs (10 пунктов)

| # | Заявление (файл:строка) | Проверка | Вердикт |
|---|---|---|---|
| 1 | «317 ключевых слов» `README.md:113,164,386` | `radioinspector.keywords.CATALOG` → `len == 317` | ✅ подтверждено |
| 2 | 17 команд CLI `README.md:153-171` | `cli.py`: 17 вызовов `add_parser(...)`, состав совпадает | ✅ подтверждено |
| 3 | «Веб-интерфейс не использует внешние JavaScript-библиотеки» `README.md:277` | grep внешних URL по `web/static/*.html,*.js`: единственный `http://` — `xmlns` внутри data:URI SVG (`platform.html:7`) | ✅ подтверждено |
| 4 | «1518 тестов, 0 skips» `docs/release/status.md` | на HEAD ровно **1518** тестов — число совпало; «0 skips» на Windows не воспроизводится (62 skip: POSIX/Tk), Linux-прогон не проверялся | ⚠️ число да, «0 skips» — только для Linux/xvfb |
| 5 | «1422 Python/Node/Tk теста» `docs/repository-audit.md:36` | снимок SHA `875d07d`; на HEAD исполняется 1518 (рост на 96 согласуется с добавлением tinySA-control тестов) | ⚠️ устаревший снимок, не воспроизведён без checkout |
| 6 | «Wi-Fi: разбор кадров 802.11…» `README.md:112` | `wifi.py` (234 строки) не импортируется нигде, нет CLI/API/тестов | ❌ заявление не подтверждено кодом |
| 7 | «Драйверы SCPI (GPIB/TCP)…» `README.md:121` | код есть (`devices/scpi.py` 264, `devices/serial_scpi.py` 115), тестов — 0 | ❌/⚠️ код без проверки |
| 8 | «Синтетика не выдаётся за аппаратную приёмку» `docs/release/hardware-acceptance.md` | все 9 сценариев `BLOCKED`, README/CHANGELOG содержат дисклеймеры, `cleanup-outcome.json: hardware_acceptance: BLOCKED` | ✅ подтверждено, замалчивания нет |
| 9 | 9 entry points `pyproject.toml:56-65` | все 9 импортируются, callable-функции на месте | ✅ подтверждено |
| 10 | 219 записей ревью с доказательствами `docs/release/review-disposition.json` | dispositions: fixed_with_evidence 86, fixed_with_regression 58, confirmed_bug 25 и др.; 140 ссылок на файлы — **0 отсутствующих** | ✅ подтверждено |

### 1.2. Тестовое покрытие по модулям (текстовый анализ, без coverage.py)

- В `radioinspector/` **111** `.py`-модулей; имена **5** из них не встречаются в `tests/` ни в одном импорте/упоминании: `devices/scpi.py` (264 строки), `wifi.py` (234), `session_analytics.py` (179), `devices/serial_scpi.py` (115), `serial_ports.py` (11). Для `session_analytics.py` дополнительно проверено: HTTP-маршрут `web/server.py:942-948` и клиентские панели тестами расчёта не покрыты.
- `tests/` — 154 `.py` + 20 `.cjs`; **1424** статических `def test*`, **1518** исполненных кейсов (часть создаётся наследованием/смоук-фикстурами); **17** py-файлов без тест-методов — это smoke-серверы/фикстуры (`*_smoke_server.py`, `run_release_checks.py`, `check_tinysa_*_hardware.py`).
- Реальные HTTP-серверы в тестах: ≥7 файлов (6 по grep `HTTPServer(`/`socketserver` + `tests/test_windows_client.py:243-246` через `create_server`), 20 Node-скриптов с **1253** assert-вызовами исполняют production-JS.
- Хардварные проверки: `tests/check_tinysa_full_hardware.py`, `tests/check_tinysa_occupancy_hardware.py` — вне обычного прогона, запускаются вручную на стенде; в CI исключены (правильно).
- Покрытие не измеряется проектом: `coverage.py 7.16.1` есть в окружении, но отсутствует в зависимостях и CI, конфигурации `[tool.coverage]` нет — объективный процент покрытия в репозитории не зафиксирован.

## 2. Находки

### P1-1. CI не гейтит ни линтер, ни типы, ни покрытие

- **Где:** `.github/workflows/windows-core.yml:1-58`, `.github/workflows/desktop-packages.yml:1-117`, `pyproject.toml:72-77`.
- **Факт:** оба workflow запускают только тесты (`python -m tests.run_release_checks`, отдельные unittest-классы) и сборку пакетов. Ни один job не вызывает `ruff`, `mypy`, `coverage`; конфигурации для них в `pyproject.toml` нет.
- **Последствие:** 1221 ruff-находка и 213 ошибок mypy в 27 файлах не видны в PR; 120 PR за 6 дней проходят при BLE001 в `device_workers.py:503,532,546`, PLW1510 в release-tooling, B023 в колбэках. Качество держится на ревью, а не на автоматике; при таком темпе регресс гарантирован.
- **Рекомендация:** добавить job `lint` (`ruff check` с зафиксированным `select` в `pyproject.toml`, блокирующий хотя бы F/E9/B023/PLW1510/S110), job `types` (mypy на новые модули, baseline-allowlist для 27 файлов), job `coverage` (`coverage.py` уже установлен в окружении как 7.16.1, в зависимости проекта не входит) с порогом и публикацией отчёта.

### P1-2. На Windows (первоклассная платформа) 16 тестов падают без `PYTHONUTF8=1`; CI это маскирует

- **Где:** прогон `python -m unittest discover -s tests -t .` → `Ran 1518 tests in 252.6s ... FAILED (failures=4, errors=12, skipped=62)`; повторный прогон — `Ran 1518 tests in 303.1s ... FAILED (failures=3, errors=13, skipped=62)` (тот же набор 16 тестов, разошлась классификация failure/error); CI: `.github/workflows/windows-core.yml:27-28` (`PYTHONUTF8: '1'`, `PYTHONIOENCODING: utf-8`).
- **Классификация из журнала:** 8 × `UnicodeDecodeError` (`test_panel_assets_and_existing_charts` в 8 файлах `tests/test_webui_*.py`), 2 падения `tests/test_windows_sweep_occupancy.py:81,104` (сравнение JSON с кириллицей), 4 `subprocess.TimeoutExpired` + 2 `AssertionError` в `tests/test_release_checks.py` (`OwnedProcessTests`).
- **Доказательство причины:** `python -m unittest tests.test_webui_signal_list` → `FAILED (errors=1)`; `PYTHONUTF8=1 python -m unittest tests.test_webui_signal_list` → `OK`. То же для `tests.test_windows_sweep_occupancy`: 2 failures → OK.
- **Последствие:** контрибьютор/агент на Windows видит красный прогон по причинам, которые CI не воспроизводит; часть ошибок (`read_text()`/`open()` без `encoding="utf-8"` в тестах и в экспорте) — реальный дефект переносимости, а не «локальная особенность». Хуже: если CI-переменную когда-нибудь уберут, зелёный статус исчезнет.
- **Рекомендация:** везде явно `encoding="utf-8"` в тестах и в путях экспорта; добавить отдельный CI-job на Windows **без** `PYTHONUTF8`/`PYTHONIOENCODING`; в `run_tests.sh`/README явно документировать `PYTHONUTF8=1` для Windows; разобрать 6 падений `OwnedProcessTests` отдельно (это не кодировка).

### P1-3. Новая функция «аналитика сеансов» не имеет тестов расчёта

- **Где:** `radioinspector/session_analytics.py` (179 строк, `analytics_summary`), маршрут `radioinspector/web/server.py:942-948`, capability `server.py:728-729`; клиенты `windows/client.py` + `web/static/analytics.js` + Swift-панель.
- **Факт:** поиск по `tests/` не находит ни `session_analytics`, ни `analytics_summary`, ни `/analytics/summary`-вызова. Единственные проверки — `tests/test_webui_analytics.py:33-42` (наличие строк в HTML/JS: `assertIn('/analytics/summary', analytics)`) и `tests/test_windows_analytics.py:38-69`, где метод клиента **подменяется** (`self.client.analytics_summary = counted`). Ни один тест не исполняет SQLite- и memory-ветку `_sqlite_analytics`/`_memory_analytics`, расчёт пика, разделение единиц потоков.
- **Последствие:** 179 строк read-only агрегатов в трёх клиентах и HTTP-маршрут не покрыты; ошибки единиц/пика/пустого сеанса пройдут незамеченными, а `capabilities` уже объявляет `session_analytics: true`.
- **Рекомендация:** unit-тесты `analytics_summary` для memory и SQLite, пустого сеанса, нескольких потоков с разными единицами (dBm vs s_meter_count), отсутствия свипов; HTTP-тест маршрута реальным сервером (как в `tests/test_windows_client.py:243-246`).

### P1-4. README заявляет Wi-Fi, модуль не подключён ни к чему и не тестируется

- **Где:** `README.md:112` («**Wi-Fi** | Разбор кадров 802.11, оценка загрузки канала, инвентаризация точек доступа»), `radioinspector/wifi.py` (234 строки).
- **Факт:** `wifi.py` не импортируется ни в одном модуле пакета (grep по `radioinspector/**/*.py`: только определения внутри самого файла), нет ни команды CLI (17 подкоманд `cli.py` без wifi), ни маршрута web, ни упоминания в `tests/` (grep по `wifi|WiFi` — 0 совпадений). Модуль фактически мёртвый код при заявленной возможности.
- **Последствие:** пользователь и следующий ИИ-агент считают функцию реализованной; feature-map/README расходятся с кодом, а 234 строки не проверяются.
- **Рекомендация:** либо подключить (CLI/API + тесты с синтетическими сетями, `synthetic_networks` уже есть), либо явно перенести строку в раздел «не подключено/не реализовано».

### P1-5. Сборка не воспроизводима: нет lock-файла, зависимости не ограничены, пакетный билд тянет choco из сети

- **Где:** `pyproject.toml:33` (`dependencies = ["numpy>=1.21"]`, верхней границы нет), `pyproject.toml:35-54` (extras без пинов), `packaging/windows/build.ps1:11-16` (`choco install innosetup --yes` при отсутствии ISCC), отсутствуют `requirements*.txt`/`uv.lock`/`poetry.lock` (поиск по репозиторию — 0 файлов).
- **Последствие:** 9 CI-комбинаций (3 ОС × 3 Python) ставят разные версии numpy (`>=1.21` покрывает и 1.x, и 2.x) и pySerial (`>=3.5,<4`); поведение численных тестов может отличаться между прогонами. Windows-пакетный билд зависит от доступности chocolatey и непинованной версии Inno Setup — вероятная причина «красного» `desktop-packages` без изменения кода.
- **Рекомендация:** ввести `constraints.txt` (или `uv lock`) и использовать его в CI; ограничить numpy `<3` с реально проверенными версиями; для Inno Setup — отдельный setup-action/кэш вместо `choco install` в момент сборки.

### P2-6. Драйверы SCPI (два модуля, 379 строк) не тестируются

- **Где:** `radioinspector/devices/scpi.py` (264 строки), `radioinspector/devices/serial_scpi.py` (115 строк).
- **Факт:** grep по `tests/` не находит ни `scpi`, ни `SCPI`-импортов (единственные совпадения — строки в `tests/test_taskplan.py:149,179`). При этом `README.md:121,397` заявляет «Драйверы SCPI (GPIB/TCP), SCPI через последовательный порт» и `radioinspector devices` их перечисляет.
- **Последствие:** SCPI — самый вероятный путь к реальному оборудованию, а он не проверен ни разу, даже на симуляторе; мой ревью не может подтвердить работоспособность заявленной возможности.
- **Рекомендация:** добавить transport-loopback/socket-симулятор SCPI и тесты команд (как уже сделано для serial-транспорта в `tests/test_serial_transport.py`).

### P2-7. Монолиты и связанность GUI/сервиса; один HTTP-исполнитель на все запросы Windows GUI

- **Где:** `radioinspector/windows/app.py` 1649 строк, `radioinspector/web/server.py` 1573, `radioinspector/session_service.py` 1590, `macos/DvoynoyObzor/ContentView.swift` **1639**, `radioinspector/web/static/platform.js` 1313, Swift `PlatformAPI.swift` 1136.
- **Факт:** `windows/app.py:112` — `ThreadPoolExecutor(max_workers=1, thread_name_prefix="windows-api")`; все HTTP-запросы, включая потоковые (`client.py:94-130` `read_block`/`cancel_read` с `settimeout(None)`), идут через единственного исполнителя. Документ `docs/repository-audit.md:84` сам ставит P2.2 «снизить связанность GUI и сервиса».
- **Последствие:** долгий запрос (стрим аудио, большой архив) занимает единственный поток и задерживает `_poll` снапшота и действия оператора; файлы по 1,5–1,6 тыс. строк повышают конфликтность правок (в репозитории они уже менялись параллельно: конфликт `web/server.py` упомянут в `docs/repository-audit.md:62`).
- **Рекомендация:** как минимум разделить исполнителей «команды» и «стримы»; вынести панели/маршруты по образцу `radioinspector/windows/spectrum_view.py` (уже вынесен и покрыт общим fixture).

### P2-8. HTTP-контракт /api/v1 дублируется в трёх клиентах; общий fixture есть только для представления спектра

- **Где:** `windows/client.py` 746 строк (ручной urllib-клиент, `validate_tune` `:137`, bounded JSON `:174-253`), `macos/SpektrKomandy/Sources/SpektrKomandy/PlatformAPI.swift` 1136 строк (URLSession + Decodable), `web/static/platform.js` 1313 строк (`api()` `:90`).
- **Факт:** межинструментальное дублирование частично закрыто общим артефактом `tests/fixtures/spectrum-presentation-v1.json`, который читают и Python (`tests/test_windows_spectrum_view.py:12`), и Node (`tests/webui_spectrum_view.cjs:5`); этот контракт даже проверяется в CI из распакованного sdist (`desktop-packages.yml:32-40`). Но для остальных ресурсов `/api/v1` (сеансы, свипы→кандидаты, исследования, batch, политики, аналитика) единого контрактного fixture нет — каждый клиент тестируется своим набором заглушек/серверов.
- **Последствие:** дрейф поведения клиентов (порядок полей, единицы, коды ошибок) обнаруживается только вручную; новых клиентских тестов становится всё больше при неизменной цене поддержки (154 тест-файла). Нарушение правила AGENTS.md «одно ядро, три клиента» пока не в измерительной логике, а в валидации контракта.
- **Рекомендация:** распространить подход `spectrum-presentation-v1.json` на остальные ресурсы (JSON-Schema/fixtures + кросс-клиентские тесты в CI), либо генерировать клиентские DTO/валидаторы из одного описания.

### P2-9. Находки ruff с функциональным, а не стилевым риском

- **Где/факт:** B023 «function uses loop variable» — **73**, в т.ч. 17 в `tests/test_windows_sweep_average.py`, 6+ в `tests/test_archive_regressions.py:139-144`; классический источник «замыкание видит последний элемент» в Tk-callback'ах и `functools.partial`-циклах. BLE001 «blind except» — **75** в ядре: `device_workers.py:503,532,546`, `automatic_completion.py:283`, `automatic_detection.py:462`, `automatic_handoff.py:341`; S110 `try/except/pass` — 13. PLW1510 `subprocess.run` без `check` — 31, в т.ч. в `tests/run_release_checks.py:173` (release-гейт).
- **Последствие:** B023 в продакшен-колбэках и тестах — потенциальные «плавающие» баги; слепые `except Exception` скрывают отказ прибора и пишутся в события как успех/ошибка без типа.
- **Рекомендация:** включить B023/BLE001/S110 в CI, для B023 — `functools.partial`/`default arg`; для BLE001 — сузить до `Exception` только на границах worker-потока с логированием типа.

### P2-10. Стабильность: известное зависание выпускного runner, 62 skip и 81 файл с потоками/таймерами

- **Где:** `docs/repository-audit.md:76` — macOS/Python 3.14 job выполнил 1371 тест за 210 с с OK, но дочерний процесс не вышел до 600 с (P1.1); мой Windows-прогон: 252–303 с, 62 skip; 81 тест-файл использует потоки/sleep/сокеты; `tests/run_release_checks.py:45-52,109-117` содержит allowlist допустимых skip-причин, и любые другие пропуски — фатальны.
- **Последствие:** нестабильность уже задокументирована и не замалчивается, но причина не устранена; в CI это job-таймаут, локально — «повисший» прогон. Систематических таймаутов на каждый тест нет.
- **Рекомендация:** per-test watchdog с диагностикой (частично есть `DiagnosticTestResult`, `run_release_checks.py:120-152`), запрет необъявленных `time.sleep` в тестах, вынос сетевых/процессных тестов в маркированную группу с жёстким таймаутом.

### P3-11. `MANIFEST.in` содержит мусорную директиву и отдаёт в sdist лишнее

- **Где:** `MANIFEST.in:7` — `prune CD Ligtht 32314` (в имени каталога пробелы → инструкция не выполняет заявленную очистку), `:10` `recursive-include macos ...`, `:5` `recursive-include tests`, `:8` `exclude docs/commercial-help-texts.md`.
- **Последствие:** sdist содержит тесты, macOS-исходники и `third_party` (нужно для проверки контракта из sdist, но раздувает пакет); строка-артефакт вводит в заблуждение.
- **Рекомендация:** удалить/исправить директиву, осознанно зафиксировать состав sdist (тесты — только нужные fixture-файлы).

### P3-12. Расхождение версий macOS и пакета; счётчики тестов в docs дрейфуют

- **Где:** `packaging/macos/build.sh:20` — `CFBundleShortVersionString 0.4.0`, тогда как `pyproject.toml:7` и `radioinspector.__version__` — `0.4.0.dev0`; `docs/repository-audit.md:36` — «1422 Python/Node/Tk теста», `docs/branch-audit-2026-11-09.md:43` — «1430», `docs/release/status.md` — «1518».
- **Факт проверки:** мой прогон на HEAD даёт **ровно 1518** тестов — последнее число в `status.md` совпадает; 1422/1430 — снимки более ранних SHA, воспроизвести их без checkout этих коммитов нельзя. Windows-инсталлятор берёт версию из метаданных (`packaging/windows/build.ps1:9`) — там консистентно.
- **Последствие:** macOS-сборка отчитается версией 0.4.0 для dev-сборки; счётчики тестов в трёх документах создают видимость расхождений.
- **Рекомендация:** версию macOS брать из `importlib.metadata.version("openradioinspector")`; в docs указывать SHA рядом с числом (как уже сделано в audit) и не дублировать счётчик в трёх местах.

## 3. Главные находки (8–12, по убыванию)

1. **P1** — CI не запускает ruff/mypy/coverage: 1221 ruff-находка (89 F-ошибок, 75 blind-except, 73 B023) и 213 ошибок mypy не блокируют PR (`.github/workflows/*.yml`, `pyproject.toml:72-77`).
2. **P1** — 16 тестов падают на Windows без `PYTHONUTF8=1` (8 `UnicodeDecodeError`, 2 сравнения кириллицы, 6 process-тестов); CI скрывает это env-переменной (`windows-core.yml:27-28`).
3. **P1** — `session_analytics.py` (179 строк) + HTTP-маршрут + 3 клиента не имеют тестов расчёта; в тестах подменяется клиент и проверяются строки в JS.
4. **P1** — `wifi.py` (234 строки) не подключён ни к CLI, ни к API, ни к тестам, но README:112 заявляет Wi-Fi как реализованную возможность.
5. **P1** — воспроизводимость: нет lock-файла, `numpy>=1.21` без верхней границы, `choco install innosetup` в сборочном скрипте (`packaging/windows/build.ps1:11-16`).
6. **P2** — SCPI-драйверы (379 строк) не покрыты ни одним тестом при заявлении в README:121.
7. **P2** — монолиты 1,5–1,6 тыс. строк и один HTTP-исполнитель на весь Windows GUI (`windows/app.py:112`).
8. **P2** — HTTP-контракт продублирован в `windows/client.py` (746), `PlatformAPI.swift` (1136), `platform.js` (1313); общий fixture есть только для представления спектра.
9. **P2** — 73 B023 и 75 BLE001 — риск функциональных багов, а не стиля (`device_workers.py:503,532,546` и др.).
10. **P2** — задокументированное зависание выпускного runner (600 с при OK), 62 skip на Windows и отсутствие per-test таймаутов.
11. **P3** — мусорная директива `prune CD Ligtht 32314` в `MANIFEST.in:7`; состав sdist включает тесты и macOS-исходники.
12. **P3** — `CFBundleShortVersionString 0.4.0` против `0.4.0.dev0` пакета (`packaging/macos/build.sh:20`).

P0-находок нет: проверками не выявлено потери данных, утечки секретов или необратимо разрушающего поведения.

## 4. Сильные стороны

1. **Объём и структура тестов при дисциплине против «зелени любой ценой».** 1424 тест-метода и 5782 assert на 28 940 строк исходников (≈1:1); `assert True` — 0; из 16 «без прямых assert» проверенные вручную делегируют проверку helper'ам или mock-assertions (`window.withdraw.assert_called_once_with()`). Release runner (`tests/run_release_checks.py:45-52,109-117,235-241`) **запрещает** неожиданные skip и валит прогон при `testsRun == 0` — это редкая для pet-проекта строгость.
2. **Межклиентский контрактный тест.** Один JSON-fixture `tests/fixtures/spectrum-presentation-v1.json` проверяется и Python-клиентом, и Node (`tests/test_windows_spectrum_view.py:12`, `tests/webui_spectrum_view.cjs:5`), включая лимиты рендера и фракции; в CI он дополнительно прогоняется из распакованного sdist (`desktop-packages.yml:32-40`).
3. **Честная документация о зрелости.** `docs/release/hardware-acceptance.md` — все 9 строк `BLOCKED`; README содержит дисклеймер «не является аттестованным средством измерений»; `docs/release/status.md` фиксирует P1.1-нестабильность и «повторный зелёный job не считается устранением». Проверка трассируемости: 219 записей `docs/release/review-disposition.json`, 140 ссылок на файлы-доказательства — **0 отсутствующих**; заявленные 317 ключевых слов = `keywords.CATALOG` (317), 17 команд README = 17 подкоманд `cli.py`, 1518 тестов = факт прогона.
4. **Упаковка и CI-гигиена.** 9/9 entry points резолвятся; версия пакета и Windows-инсталлятора из единого источника метаданных; actions закреплены по SHA, `permissions: contents: read`, `persist-credentials: false`; установка wheel проверяется **вне checkout** (`desktop-packages.yml:48-55`), sdist проверяется `verify_sdist.py` и `check_public_tree.py`.
5. **Интеграционные тесты на реальных сокетах.** 6 тест-модулей поднимают настоящий `create_server(...)` + `serve_forever` в потоке (например, `tests/test_windows_client.py:243-246`), а не только моки; 20 Node-скриптов (`tests/webui_*.cjs`) исполняют production-JS с 1253 assert-вызовами.

## 5. Гипотезы к проверке

1. **Legacy-путь macOS открывает порт из основного приложения.** `macos/DvoynoyObzor/ArModel.swift:182` (`opened.open(path: baud:)`), панель `ArPane.swift:5` использует `ArModel`. README:348,364 утверждает, что основной remote UI «не открывает `/dev/cu.*`». Нужно проверить навигацию: доступна ли `ArPane` из основного окна или это отключённая legacy-вкладка.
2. **Единственный HTTP-исполнитель Windows GUI блокируется на стримах.** `windows/app.py:112` (`max_workers=1`) + `windows/client.py:94-130` (`settimeout(None)` для потока) — гипотеза, что длинный стрим задерживает `_poll`/кнопки. Нужен тайминговый тест «стрим + snapshot одновременно».
3. **6 падений `OwnedProcessTests` — среда или код.** TimeoutExpired в `tests/test_release_checks.py` может зависеть от вложенности Job Object (агентская песочница/терминал) или быть реальным дефектом Windows-очистки. Нужен прогон в обычной консоли и на CI-раннере Windows.
4. **62 skip на Windows частично скрывают непроверенное.** Часть — POSIX-only и Tk; но утверждение docs «0 skips» относится к Linux/xvfb и локально не воспроизведено. Нужен прогон на Linux/Python 3.12 с `xvfb-run` и сверка allowlist.
5. **`desktop-packages` может быть красным из-за Inno Setup.** `choco install innosetup` в `build.ps1:15` и `smoke-install.ps1` — сеть/права на windows-latest; без доступа к GitHub Actions это не проверить (web_fetch к api.github.com недоступен).
6. **Аналитика могла быть покрыта в ветке #115, но не в `main`.** Модуль появился в срезе #115; возможно, тесты остались в PR-ветке. Проверить историю `git log -- radioinspector/session_analytics.py`.
7. **Матрица CI не покрывает Python 3.11**, на котором работает локальное окружение и `run_tests.sh`-документация; совместимость 3.11 проверяется только локально.

## 6. Резюме (10 строк)

1. Проект зрелее, чем типичный «ИИ-сгенерированный» репозиторий: 154 тест-файла, 1518 тестов, 5782 assert, общий межклиентский fixture и release runner, запрещающий неожиданные skip.
2. Главный инженерный пробел — **отсутствие автоматических гейтов качества**: ruff/mypy/coverage не запускаются в CI, поэтому 1221 ruff-находка и 213 ошибок mypy накапливаются безнаказанно.
3. Второй критичный риск — **переносимость тестов**: на Windows без `PYTHONUTF8=1` 16 тестов не проходят (8 из них — `UnicodeDecodeError`), а CI это маскирует.
4. Заявленные в README функции не равны проверенным: Wi-Fi-модуль не подключён (234 строки мёртвого кода), SCPI-драйверы (379 строк) и новая аналитика (179 строк) не имеют тестов расчёта.
5. Документация о зрелости при этом честная: аппаратная приёмка целиком `BLOCKED`, 219 review-записей с 140 существующими ссылками-доказательствами, счётчики тестов/ключевых слов/команд сверены.
6. Упаковка аккуратна (9/9 entry points, версия из метаданных, SHA-pinned actions), но невоспроизводима: нет lock-файла, `numpy>=1.21` без верхней границы, сборка Windows тянет Inno Setup из сети.
7. Архитектура «одно ядро, три клиента» соблюдается в измерительной логике, но HTTP/контрактная валидация продублирована в трёх клиентах (746 + 1136 + 1313 строк) без общего контракта, кроме представления спектра.
8. Монолиты (`app.py` 1649, `ContentView.swift` 1639, `session_service.py` 1590, `server.py` 1573) и один HTTP-исполнитель Windows GUI повышают риск регрессий и уже приводили к конфликтам слияния.
9. 73 B023 и 75 BLE001 — это не косметика: замыкания на переменную цикла и слепые `except Exception` в worker-коде могут давать «плавающие» баги и скрытые отказы приборов.
10. Первые три шага с максимальной отдачей: lint/type/coverage job в CI; `encoding="utf-8"` + Windows-job без `PYTHONUTF8`; тесты для `session_analytics`, SCPI и подключение/маркировка Wi-Fi.
