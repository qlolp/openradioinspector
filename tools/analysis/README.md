# Инструменты исследования внешних материалов

Статические анализаторы PE-ресурсов, строк, Firebird-метаданных, MSI и WinHelp.
Используют стандартную библиотеку Python. Они не запускают оригинальную
программу и не обходят лицензионную защиту.

Коммерческий дистрибутив не является зависимостью проекта и не должен
находиться в checkout. Полные результаты извлечения и подписи чужого UI
не публикуются. Владелец лицензии отвечает за разрешённый объём исследования.

## Запуск

Передавайте явный путь к законно доступному файлу вне репозитория:

```bash
python3 tools/analysis/pe_resources.py --exe /external/licensed/InspectorRT.exe --forms
python3 tools/analysis/strings_extract.py --exe /external/licensed/InspectorRT.exe --count
python3 tools/analysis/firebird_meta.py --db /external/licensed/TASKPLAN.ITASKDB
python3 tools/analysis/msi_tables.py --msi /external/licensed/SetupRadioInspectorLight.msi --streams
python3 tools/analysis/hlp_reader.py /external/licensed/InspectorRT.hlp --json
```

Не сохраняйте полные извлечённые тексты или ресурсы в публичном репозитории.

## Проверки

Обычный запуск не ищет оригинал и проверяет синтетические фикстуры:

```bash
python3 -m unittest tests.test_analysis_tools -v
```

Необязательные проверки владельца лицензии включаются только внешним каталогом:

```bash
ORI_RESEARCH_DISTRIBUTION=/external/licensed python3 -m unittest tests.research_checks -v
```

Несуществующий путь и каталог внутри checkout считаются ошибкой конфигурации.
Пропуски исследовательских тестов не означают подтверждение поведения оригинала.
