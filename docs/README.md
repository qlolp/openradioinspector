# Документация OpenRadioInspector

Первый сеанс можно провести по офлайн-руководству. Для выбора пакета и проверки его контрольной суммы откройте состояние выпуска. [Руководство оператора со снимками](operator-guide/index.html) · [Сборки и результаты](release/status.md)

## Оператору

| Задача | С чего начать |
|---|---|
| Установить пульт, подключиться к службе, сохранить данные при обновлении | [Выбор пакета и откат](release/installation.md) · [Raspberry Pi](pi-rc1.md) · [Windows](windows-standalone-rc.md) · [macOS](macos-observation-console.md) |
| Подготовить независимые tinySA и AR-DV1, проверить покрытие | [Подготовка в пульте](operator-guide/index.html#prepare) · [Профиль tinySA](tinysa-receive-profile.md) · [Приёмник AR-DV1](ardv1-fast-scan.md) · [Поддиапазоны и сетка 10 кГц](release/pi-windows-receive-grid.md) |
| Вести охрану и расследовать событие | [Режимы работы](monitoring-modes.md) · [План обзора](scan-plans.md) · [Пульт Pi/Windows](pi-windows-operator-ui.md) · [Открыть исходник](operator-guide/index.html#research) |
| Сравнить измерения, выбрать эталон и единицы | [Контекст свипа](sweep-context.md) · [Именованные эталоны](named-sweep-references.md) · [Занятость](sweep-occupancy.md) · [Методики измерения](measurement-methods.md) |
| Сохранить результат или восстановить архив | [Аудиофрагменты и метки](audio-evidence.md) · [Экспорт измерений](archive-export.md) · [Отчёты](reporting.md) · [Дисковый бюджет](archive-capacity.md) · [Резервная копия](rust-core-backup.md) |
| Попробовать учебный архив из исходников | [Python CLI](python-cli.md) · [Примеры](../examples/README.md) |

## Разработчику

| Изменение | Владелец и ближайшая проверка |
|---|---|
| Прибор, сеанс, запись в архив или доставка события | [Путь от команды до экрана](platform-architecture.md#измерение-запись-и-отображение) · [15 крейтов](platform-architecture.md#владельцы-в-rust) · [Исходники и журнал в SQLite](compact-panorama-events.md) |
| Схема API или общий SDK | [API v1](api-v1.md) · [Источники и генерация контрактов](contracts-modernization.md) · [Паритет реальных запросов](api-parity.md) · [Web/Swift на одном ядре](client-core-interop.md) |
| WebUI, Tauri или SwiftUI | [Состояние пультов и доступ](platform-architecture.md#клиенты-контракты-и-доступ) · [Защита транспорта Web/Windows](../clients/web/SECURITY.md) · [Нативная разработка Mac](../macos/README.md) |
| Python-библиотека, CLI, Tk или исследовательский worker | [Архитектура Python](architecture.md) · [Пути данных и точки входа](platform-architecture.md#python-и-научные-процессы) · [Снимок для анализа](scoped-history-source.md) |
| Выбрать следующее изменение по конкретному отказу | [Именованные регрессии](platform-architecture.md#при-разборе-отказа) · [Очередь небольших PR](platform-architecture.md#очередь-небольших-изменений) |

## Сопровождающему

| Задача | Документы |
|---|---|
| Установить, что именно прошло проверку | [Версия установленных пакетов, интеграция в main и аппаратура](release/status.md) · [Условия 0.4.0](release/0.4.0.md) |
| Проверить изменение без приборов | [Штатные синтетические проверки](release-validation.md) · [Проверки ядра и пультов](modernization-verification.md) · [Обязательные проверки main](release/branch-protection.md) |
| Воспроизвести нагрузку и принять пакеты | [Синтетическая нагрузка](modernization-concurrent-load.md) · [Стендовые проверки](release/hardware-acceptance.md) · [Полный протокол Mac](release/macos-post-test-comprehensive-acceptance.md) |
| Подготовить установщик и публикацию | [Поставка по платформам](platform-architecture.md#сборка-и-поставка) · [Подпись и публикация](release/public-distribution.md) · [Происхождение и лицензии](release/provenance.md) |

Локальные ссылки и заголовки во всех руководствах можно проверить из корня командой `python -m tools.audit.doc_links`. Для разбора GitHub Flavored Markdown нужен Pandoc. При упаковке отдельная проверка сверяет все снимки офлайн-руководства с их SHA-256. [Проверка ссылок](../tools/audit/doc_links.py) · [Сборка руководства](../tools/release/operator_manual.py) · [Ведомость снимков](operator-guide/captures.json)

## Предметный указатель

Ссылки ниже ведут к частным методикам, устройству модулей и протоколам испытаний. Результат каждого прогона относится к указанным в нём ревизии, профилю и платформе.

<details>
<summary>Приборы, режимы приёма и качество измерений</summary>

- Выбор прибора, команды и ограничения. [Поддерживаемые устройства](devices.md) · [Матрица AR-DV1 и источники](ardv1/README.md) · [Управление tinySA](tinysa-full-control.md) · [Обмен с приглашением tinySA](tinysa-prompt-integration.md) · [Команды SCPI](serial-scpi-setters.md) · [Жизненный цикл SoapySDR](SOAPY_LIFECYCLE.md)
- Повторяемость приёма. [Непрерывный обзор](continuous-spectrum-monitoring.md) · [Проверка непрерывности](continuous-spectrum-validation.md) · [Возобновление приёма](receive-recovery.md) · [Остановка и новый запуск tinySA](tinysa-stop-restart.md) · [Сравнение прошивок](tinysa-firmware-ab.md)
- Планы и единицы. [Частотный растр](frequency-raster.md) · [Округление](frequency-rounding.md) · [Напряжённость поля по RMS](field-strength-rms-v2.md) · [Нормативные документы](standards.md) · [Ограничения](limits.md)
- Прикладные измерения. [Частотомер](frequency-counter.md) · [Параметры DVB](dvb-parameters.md) · [Карта](measurement-map.md) · [Результаты измерений](measurement-results.md) · [Список сигналов](signal-list.md) · [Фонограмма](phonogram.md)

</details>

<details>
<summary>Спектр, сравнение измерений и работа с архивом</summary>

- Показ и исследование. [Представление спектра](spectrum-presentation.md) · [Инспектор](spectrum-inspection.md) · [История частоты](frequency-history.md) · [Качество свипа](sweep-quality.md) · [Спектральная маска](spectral-mask-analysis.md)
- Порог и эталон. [Адаптивный порог](adaptive-sweep-threshold.md) · [Три свипа](three-sweep-thresholds.md) · [Усреднённый эталон](averaged-sweep-reference.md) · [Обнаружение изменений](sweep-change-detection.md) · [Сравнение свипов](sweep-comparison.md) · [Экспорт усреднения](sweep-average-export.md)
- Перенос источников. [Пакетный ввод](archive-ingest.md) · [Потоковый ввод](archive-stream.md) · [Потоковый экспорт](archive-export-stream.md) · [Поток аудио](audio-stream.md) · [Целостность SigMF](sigmf-integrity.md) · [Ключевые слова отчёта](report-keywords.md)

</details>

<details>
<summary>Ядро, владение приборами и хранение данных</summary>

- Запуск и устройства. [Ядро](rust-core.md) · [Управление инструментами](rust-control.md) · [Привязка к исполнителю](rust-core-instrument-control.md) · [Серийные адаптеры](rust-instruments.md) · [Остановленный исполнитель](core-retained-worker.md) · [Реакция на задержку сбора](rust-acquisition-pressure.md) · [Аудиозахват](rust-audio.md)
- Бизнес-правила. [Сеансы](rust-session.md) · [Детектор](rust-monitoring.md) · [Охрана](rust-guard.md) · [Разрешения](rust-guard-control-grants.md) · [Отмена ожидания покрытия](guard-coverage-cancellation.md) · [Точность кандидата](candidate-scalar-precision.md) · [Состояние сеанса при записи](rust-sampled-lifecycle.md)
- Архив SQLite. [Устройство архива](rust-archive.md) · [Запросы](rust-archive-queries.md) · [Исходные данные](rust-raw-source-compatibility.md) · [Жизненный цикл сеанса](archive-session-lifecycle.md) · [Доставка уведомлений](rust-archive-outbox.md) · [Контрольные точки WAL](rust-checkpoint-maintenance.md)
- Копии и анализ. [Параметры резервного копирования](rust-backup-options.md) · [Выгрузка снимка](rust-snapshot-export.md) · [Общий бюджет ввода-вывода](shared-background-io.md) · [Привязка фоновых операций](rust-background-io-hooks.md) · [Производная история](rust-history.md) · [Прежний формат панорам](rust-legacy.md)

</details>

<details>
<summary>API, процессы Python, автоматика и расписание</summary>

- Обмен данными. [Контракты Rust](rust-contracts.md) · [Двоичные кадры](measurement-stream.md) · [Допуск кодировщиков WebSocket](websocket-encoder-admission.md) · [Контракт истории](history-runtime-contract.md) · [Соглашения о курсорах и данных](modernization-contracts-and-history.md)
- Python и фоновые задачи. [RuntimeBridge](python-runtime-bridge.md) · [Владелец записи](python-writer-ownership.md) · [Жизненный цикл SSE](python-event-stream-lifecycle.md) · [Процессы средств проверки API](api-tool-process-ownership.md) · [Научный RPC](research-rpc.md) · [Исследовательский worker](research-worker.md) · [Диагностика завершения](worker-cleanup-diagnostics.md)
- Политики сеанса. [Автоматика Rust](rust-automation.md) · [Привязка в ядре](core-automation-binding.md) · [Настройка в работающем сеансе](automation-running-setup.md) · [Проверка политики](automation-policy-validation.md) · [Фиксация политики](automation-policy-commit.md) · [Курсор детектора](automatic-detection-cursor.md) · [Завершение исследования](automatic-completion.md)
- Задания. [Планировщик](task-scheduler.md) · [TaskBook Rust](rust-tasks.md) · [Исполнение задания](rust-task-execution.md)

</details>

<details>
<summary>Реализация пультов и проверка интерфейса</summary>

- Общий WebUI. [Веб-клиент](web-modernization.md) · [Встроенные ресурсы](rust-web-assets.md) · [Отрисовка спектра](web-renderer-display.md) · [Выбор рендерера](web-renderer-policy.md) · [Состояние узла](web-health-receipts.md) · [Контекст исследования](web-research-context.md)
- Операции WebUI. [Аналитика](webui-analytics.md) · [Выгрузка архива](webui-archive-export.md) · [Создание](webui-task-create.md) и [отмена задания](webui-task-cancel.md) · [Исходники для HTTP-фикстуры](context-http-fixture-seeding.md) · [Очистка тестового проекта](web-e2e-owned-guard-cleanup.md)
- Windows и Tk. [Порты на Windows](windows-core-serial.md) · [Жизненный цикл Tk](windows-tk-lifecycle.md) · [Аналитика](windows-analytics.md) · [Выгрузка](windows-archive-export.md) · [Создание](windows-task-create.md) и [отмена задания](windows-task-cancel.md)
- Приложение Mac. [Нативный клиент](macos-modernization.md) · [Рабочее место](macos-operator-workbench.md) · [Узел для разработки](macos-development-core.md) · [Производительность](macos-native-performance.md) · [Значок приложения](macos-app-icon.md) · [Макет пульта](ui/macos-observation-console.html)
- Операции Mac. [Аналитика](macos-analytics.md) · [Выгрузка](macos-archive-export.md) · [Создание](macos-task-create.md) и [отмена задания](macos-task-cancel.md) · [Отдельный профиль с аудио](macos-audio-candidate.md)
- Проверка интерфейса. [Замысел пульта](operator-console-design.md) · [Удобство нативной работы](native-monitoring-usability.md) · [Сценарии workbench](macos-workbench-validation.md) · [Восстановление истории на экране](spectrum-history-recovery-validation.md) · [Условия и хеши снимков](operator-guide/captures.json)

</details>

<details>
<summary>Сборка, программные проверки и аппаратная приёмка</summary>

- Общие процедуры. [Сборка офлайн](offline-core-release.md) · [Безопасность выпуска](release-safety.md) · [Проверки переносимости](ci-portability.md) · [Надёжность испытаний](audit-reliability.md) · [Владение тестовыми процессами](release-process-ownership.md) · [Диагностика зависания](release-diagnostic-watchdog.md)
- Профили платформ. [Mac RC1](macos-personal-rc.md) · [Протокол Mac RC1](release/macos-0.4.0-rc1-acceptance.md) · [Pi/Windows RC1](release/pi-windows-rc1.md) · [Подготовка ARM64](arm64-linux-readiness.md) · [Сборка Windows LLVM](windows-llvm-build.md) · [Диагностика Windows Job](windows-job-diagnostics.md) · [Завершение дочерних процессов Darwin](runtime-darwin-zombie-cleanup.md)
- Результаты и планы испытаний. [Бенчмарки](modernization-benchmarks.md) · [Аппаратный план платформы](modernization-hardware-acceptance.md) · [Приёмка охраны](monitoring-hardware-acceptance.md) · [Приёмка tinySA](tinysa-hardware-acceptance.md) · [Процедура продвижения пакета](release/public-distribution.md)

</details>

## Исторические материалы

Датированные аудиты сохраняют состояние **на момент указанного коммита**. Старые списки веток и результаты испытаний помогают восстановить причину решения. Для текущего состояния используйте [реестр сборок](release/status.md) и [архитектуру платформы](platform-architecture.md).

- Ревизии репозитория и кода. [Исходная уборка](repository-audit.md) · [Ветки на 9 ноября](branch-audit-2026-11-09.md) · [Ревью 9 ноября по направлениям](reviews/2026-11-09/) · [Аудит 13 ноября](code-audit-2026-11-13.md) · [Сверка 15 ноября](repo-state-audit-2026-11-15.md) · [Отчёт 16 ноября](deep-audit-2026-11-16.md)
- Решения по развитию. [Дорожная карта](roadmap.md) · [Дневник выполненных этапов](roadmap-progress.md) · [Замысел модернизации](modernization.md) · [Модернизация runtime](runtime-modernization.md) · [План поставки](plans/) · [Планы отдельных изменений](superpowers/plans/) · [Проектные решения](superpowers/specs/)
- Протоколы прошлых выпусков и интеграций. [Прежний указатель готовности](release-readiness.md) · [Результаты прежних кандидатов](release/status.md#история-испытаний) · [Первоначальная интеграция веток](release/integration.md) · [История публичного дерева](release/history-cleanup.md)
- Справочные материалы по RadioInspectorRT. [Матрица на 15 ноября](rt-comparison.md) · [Карта возможностей](feature-map.md) · [План функционального покрытия](rt-functional-parity-plan.md) · [Разбор исходной архитектуры](original-architecture.md) · [Обзор материалов по радиомониторингу](radio-monitoring-reference-review.md)
- Исследования и атрибуция. [Инвентаризация приложений](commercial-app-inventory.md) · [Решение по диску](commercial-disk-decision.md) · [Наблюдения](commercial-observation.md) · [Light 3.2.3.14](capabilities-light-32314.md) · [Инвентаризация runtime](rinspectorrt-light-runtime-inventory.md) · [Исходный отчёт сравнения](radioinspectorrt-comparison-report.md)
- Сверки сравнения. [Возможности на 11 ноября](research-rt-capabilities-2026-11-11.md) · [Ревью исследования](research-rt-review-2026-11-11.md) · [Поправки](rt-comparison-corrections-2026-11-11.md) · [Перекрёстная проверка](rt-comparison-review-crosscheck-2026-11-11.md) · [Открытые вопросы на 12 ноября](rt-comparison-gaps-2026-11-12.md)
