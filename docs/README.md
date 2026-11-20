# Документация OpenRadioInspector

Для первого сеанса установите пакет своей платформы и откройте руководство оператора. В нём показаны подготовка, обзор, очередь приёмника и история измерений. [Руководство со снимками](operator-guide/index.html) · [Инженерные сборки и состояние испытаний](release/status.md)

| Задача | Документы |
|---|---|
| Установить службу и подключить пульт | [Raspberry Pi](pi-rc1.md) · [Windows](windows-standalone-rc.md) · [macOS](macos-observation-console.md) |
| Настроить приборы и период обзора | [Профиль приёма tinySA](tinysa-receive-profile.md) · [AR-DV1](ardv1-fast-scan.md) · [Поддиапазоны и сетка 10 кГц](release/pi-windows-receive-grid.md) |
| Работать с проектом, охраной и событиями | [Режимы работы](monitoring-modes.md) · [Планы обзора](scan-plans.md) · [Пульт Pi и Windows](pi-windows-operator-ui.md) |
| Сравнить наблюдения и оценить сигнал | [Контекст свипа](sweep-context.md) · [Эталоны](named-sweep-references.md) · [Занятость](sweep-occupancy.md) · [Методики и единицы](measurement-methods.md) |
| Исследовать аудиозапись | [Метки и экспорт фрагментов](audio-evidence.md) · [Отдельный аудиокандидат macOS](macos-audio-candidate.md) |
| Использовать Python CLI | [Панорамы, измерения и экспорт](python-cli.md) |
| Выгрузить результаты и сохранить архив | [Экспорт измерений](archive-export.md) · [Отчёты](reporting.md) · [Дисковый бюджет](archive-capacity.md) · [Backup и restore](rust-core-backup.md) |
| Разрабатывать клиенты и адаптеры | [Платформа](platform-architecture.md) · [HTTP API](api-v1.md) · [Совместимость API](api-parity.md) · [Python-библиотека](architecture.md) |
| Воспроизвести проверки выпуска | [Синтетические проверки](release-validation.md) · [Аппаратные испытания](release/hardware-acceptance.md) · [Подпись и публикация](release/public-distribution.md) |

Для сверки результатов используйте коммит, платформу и профиль из конкретного протокола. Датированные аудиты и сравнения описывают состояние на указанную в них дату.

Датированные материалы включают [аудит кода](code-audit-2026-11-13.md) и [сверку репозитория на 9154cf5](deep-audit-2026-11-16.md).
