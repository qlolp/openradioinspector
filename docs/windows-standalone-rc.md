# Windows 11 x64 • персональный узел RC1

Установщик NSIS версии `0.4.0-rc.1` размещает Tauri/WebView2, `ori-core.exe` и `ori-supervisor.exe` в каталоге текущего пользователя. В комплект входит автономный установщик WebView2. На операторском ПК нужны Windows 11 x64 и драйверы подключаемых COM-устройств. Для установки и работы самого узла не требуется подключение к сети, Python, Node, Rust или повышение прав.

Подробное руководство со снимками открывается из `runtime\operator-guide\index.html` в каталоге установленного приложения. [Руководство оператора](operator-guide/index.html)

В примере первого запуска показано установленное окно Tauri/WebView2 на виртуальной Windows x64, сборка 26100. Кнопка «Подключить локальный узел» находится над полем адреса другого узла. Снимок получен с инженерного пакета `0.4.0-rc.1` со статусом Authenticode `NotSigned`. В этом прогоне проверка остановилась на задании адреса учебного ядра. [Снимок окна подключения](operator-guide/images/windows-installed-connection.png)

В следующем прогоне установленное приложение подключилось к локальному учебному ядру и открыло рабочее пространство. В проекте «Учебный эфир» видны пик tinySA на 102,5 МГц, первые строки водопада и наблюдение AR-DV1 с 22 отсчётами S-метра. [Снимок установленного пульта](operator-guide/images/windows-installed-monitoring.png)

## Работа

В окне выберите **Подключить локальный узел**, затем подтвердите работу по локальному HTTP. Служба запустит серийную сборку ядра в режиме live. В рабочем пространстве выберите приборы и выполните предпроверку. Только ядро открывает COM-порты и записывает архив. Та же сборка интерфейса доступна в браузере этого ПК по адресу `http://127.0.0.1:8770/modern/`. На ПК с несколькими активными пользователями учитывайте, что локальный HTTP доступен другим процессам на loopback. Административные команды защищает отдельный именованный канал.

Для подключения к Pi или другому узлу введите его HTTPS-адрес в разделе **Подключиться к другому узлу**. Доверие к сертификату проверяет Windows, учетные данные хранит Windows Credential Manager. Этот выбор не запускает локальное ядро. Закрытие окна, переключение на другой узел и блокировка экрана оставляют локальный сбор работающим. Выход из учётной записи и сон компьютера прерывают покрытие.

В **Настройках соединения** кнопка **Остановить локальный узел и мониторинг** закрывает ядро, журнал и архив. **Запускать локальный узел при входе в Windows** явно регистрирует задачу Task Scheduler с ограниченными правами текущего пользователя. **Отключить запуск при входе** отменяет регистрацию. Установка сама по себе не включает запуск при входе.

Супервизор регистрирует задачу через системный Windows PowerShell и его модуль ScheduledTasks. При ошибке регистрации или удаления команда сообщает этап и числовой HRESULT, если скрипт успел их определить. Эти два значения можно приложить к отчёту об установке.

Те же операции доступны в PowerShell из каталога установленного приложения.

```powershell
Set-Location "$env:LOCALAPPDATA\OpenRadioInspector RC\runtime"
.\ori-supervisor.exe windows-node start
.\ori-supervisor.exe windows-node status
.\ori-supervisor.exe windows-node register
.\ori-supervisor.exe windows-node unregister
.\ori-supervisor.exe windows-node shutdown
```

## Владение и обновление

Задания оператора сохраняются в файле `tasks.json` рядом с базой. Архив хранится отдельно от установки, в `%LOCALAPPDATA%\OpenRadioInspector-Windows-RC1\sessions.sqlite3`. Для нового каталога служба задаёт и проверяет защищённый DACL с единственной разрешающей записью для SID пользователя. Перед первым запуском под блокировками экземпляра и записи служба создаёт компактный архив v7. При последующем старте ядро открывает существующий файл в его формате, включая v6.

Диспетчер связывает имя канала с SID, каталогом архива и типом запуска. Установленный узел и служба с произвольной конфигурацией используют разные каналы даже в одном каталоге. Он владеет первым экземпляром канала, отклоняет удалённые подключения и ограничивает размер и время запроса. Обе стороны проверяют владельца и единственную разрешающую запись фактического DACL. Статус возвращается после проверки идентичности запущенного процесса, предпроверки и захваченной блокировки записи. Повторный старт подключается к работающему экземпляру. Занятый порт или блокировка дают ошибку запуска.

ОС назначает ядро в закрытый Job Object в самом вызове создания процесса. Супервизор держит единственный дескриптор Job. Запрос остановки сначала выполняет аутентифицированное завершение ядра, затем диспетчер подтверждает выход дерева процессов и освобождение порта и записи. При аварии супервизора закрытие дескриптора Job завершает оставшиеся процессы дерева. Обрыв считается перерывом в мониторинге.

Перед заменой или удалением установки остановите локальный узел и сделайте резервную копию архива. При откате к версии, читающей v6, из остановленной RC выполните `ori-core.exe archive-materialize --stopped SOURCE.sqlite3 NEW.sqlite3`. Укажите новый файл для совместимой копии и проверьте её открытие предыдущим ядром. Сохраните исходный v7 и резервную копию. При удалении пакета пользовательский архив сохраняется.

## Сборка и приёмка

На Windows x64 с MSVC, закреплёнными Rust/Node и Python выполните в чистом checkout:

```powershell
npm ci --prefix clients/web
npm ci --prefix clients/windows
npm --prefix clients/windows run build -- --ci -- --locked
```

Обычный хук сборки Tauri вызывает `tools/release/prepare_windows_node.py`. Он один раз собирает production WebUI, проверяет и встраивает те же файлы в оба приложения. Затем он собирает x64 MSVC helpers с serial, проверяет их `--build-info`, SHA-256, ревизию и параметры. Манифест, лицензии, сценарий регистрации и бинарные файлы входят в `runtime`. Для сборки могут потребоваться сетевые загрузки инструментов NSIS и WebView2. Готовый установщик содержит WebView2 для автономной установки.

Регрессионные проверки упаковки запускаются `python -m unittest tests.test_windows_standalone_rc`. Нативные тесты `cargo test --locked -p ori-runtime --lib` проверяют DACL обоих концов канала и завершение потомка после выхода родителя и закрытия Job. В новой интерактивной тестовой учётной записи выполните:

```powershell
.\deploy\windows\smoke-installed.ps1 -Installer "C:\Artifacts\OpenRadioInspector RC_x64-setup.exe"
```

Сценарий устанавливает реальный пакет, вызывает запуск локального узла кнопкой WebView2, закрывает окно штатно, сверяет HTTP-ресурсы по манифесту и сохраняет сессию и синтетическую панораму через API. После остановки и перезапуска он проверяет исходный вектор частот и уровней. Затем сценарий сохраняет файл v7, устанавливает материализованную копию v6 в свой тестовый каталог и повторяет чтение той же панорамы. Для приёмки Windows 11 отдельно зафиксируйте установку при отключённой сети, блокировку и разблокировку сеанса во время записи, вход с включённой задачей, недоступность административного канала из второй учётной записи, отключение и повторное подключение каждого реального COM-прибора. Кросс-компиляция и тесты CI не измеряют эти результаты.

Проверка установки повторно запускает этот же NSIS с `/S /UPDATE`, проверяет восстановление ресурса, затем запускает реальный деинсталлятор. План оператора и исходная панорама должны пережить повторную установку. Для проверки отката передайте `-PriorInstaller`, `-PriorInstallerSha256` и `-PriorRevision`. Сценарий проверяет контрольную сумму, устанавливает предыдущий пакет в очищенный каталог приложения, сверяет ревизию установленных helpers, читает панораму и задание через предыдущее ядро, затем обновляет приложение текущим пакетом. Исходный файл v7 сохраняется рядом с тестовой копией v6.

Для проверки без подключения к сети на одноразовом ПК или в виртуальной машине с отключённым сетевым адаптером запустите сценарий с параметром `-RequireOffline`. Он проверяет состояние сети в начале каждого этапа и в конце. В `installation.json` этому результату соответствует `network_check=disconnected_at_all_phase_checks`. При обычном запуске на подключённом агенте сохраняется `not_requested`. Отчёт также связывает контрольные суммы двух установщиков с ревизиями, прочитанными из установленных программ.

При ручном запуске Windows workflow поля `prior_run_id`, `prior_installer_sha256` и `prior_revision` указывают на архив сборки предыдущего совместимого пакета. Без него отчёт сохраняет `pending_missing_prior_package`. Артефакт `windows-x64-offline-rc-candidate` публикуется с SHA-256 сразу после сборки. Отдельный `windows-x64-rc-diagnostics` содержит результат проверки пакета и этап ошибки даже при отсутствии установщика.

### Снимок учебного окна Windows

В чистой интерактивной тестовой учётной записи добавьте к команде репетиции `-WindowEvidenceDirectory` с путём к новому каталогу. Для этой опции на испытательной машине нужен Python 3. В задание CI для установленного пакета она уже включена. Окно Tauri запускается с временным профилем WebView2. Второй экземпляр установленного ядра открывает отдельный архив в режиме replay на локальной петле. Спектр и наблюдения поступают от встроенных симуляторов tinySA и AR-DV1 с настройками из `deploy/windows/operator-window-fixture.json`. После захвата репетиция закрывает оба процесса и удаляет архив и профиль.

Артефакт `windows-installed-operator-window` содержит `windows-installed-tauri.json`. При успешном захвате рядом находится `windows-installed-tauri.png`. Для сценария `monitoring` репетиция проверяет принадлежность HWND установленному EXE, обе диаграммы и сохранённые строки водопада. Если доступен только экран подключения, результат содержит `fresh_install_connection` и причину недоступности мониторинга. Если интерактивный рабочий стол или отрисованное окно недоступны, причина записывается в JSON. Там же указаны ревизия, версия, сборка Windows, хеши, размеры, DPI и отдельный результат Authenticode для установщика и EXE. Статус `NotSigned` обозначает инженерный артефакт без подписи.

## Concurrent full-PCM release fixture

The native concurrent reference workflow runs on Windows x64 and hosted Ubuntu ARM64. Each host uses the shared native replay core, a fresh production compact v7 archive and three Chromium operator clients. Pull requests exercise 60 seconds. Adding the exact `full-reference-load` label to the pull request, or dispatching the workflow with `full` selected, schedules the original 1,800 seconds at 10 sweeps and 10 observations per second, 4,096 points per sweep and all 86,400,000 mono 48 kHz PCM frames. The release installer and physical-radio tests have separate receipts.

To run locally from the repository with the pinned Rust, Python and Node versions, install the Web lock and Chromium, build and embed the production frontend, and build the native fixture. Choose a fresh path outside every Git checkout.

```powershell
npm ci --prefix clients/web
Push-Location clients/web
npx playwright install chromium
Pop-Location
npm run build --prefix clients/web
python tools/build_modern_web.py
python tools/build_modern_web.py --check
cargo build --release --locked --features serial -p ori-core --example benchmark_background
$env:ORI_PLAYWRIGHT_MODULE = (Resolve-Path clients/web/node_modules/playwright/index.mjs).Path
$exe = (Resolve-Path target/release/examples/benchmark_background.exe).Path
$out = Join-Path $env:TEMP ("ori-reference-" + [Guid]::NewGuid())
python -m benchmarks.ori_bench.concurrent --candidate $exe --output-dir $out --history-profile paged-history
```

`--smoke-seconds 60` selects the short run. Each Core, restore and observer process tree stays in its own retained Windows Job. Graceful release requires zero live members. The browser driver reports after-draw receipts for its three independent pages.

`paged-history` explicitly chooses the production sweep-summary API. The controller follows a snapshot cursor, compares every returned summary against its original sweep and requires a subsequent page to start and finish while the live NDJSON export and online backup are both active. Verified producer offers must bracket the page. The report contains these observations, complete export counts and digest, the restored backup manifest with verified schema 7 and the Core process's lifetime peak working set. Scientific research retains its original containment requirement. Windows currently has no scientific-worker profile with that containment. The paged run records `scientific_worker=not_requested`. A successful short run leaves full-duration acceptance open.
