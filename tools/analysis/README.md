# Инструменты разбора коммерческого дистрибутива

Каталог содержит программы, которыми получены факты из
[`docs/commercial-app-inventory.md`](../../docs/commercial-app-inventory.md).
Задача инструментов — сделать выводы инвентаря проверяемыми: каждый
факт можно воспроизвести командой из этого каталога, а не поверить на слово.

Зависимостей нет, только стандартная библиотека Python 3.10 и новее.

## Область применения и ограничения

Инструменты выполняют **статический** анализ: они читают файлы и не
запускают коммерческие модули, не обходят лицензионную защиту и не
декомпилируют код. Это сознательное ограничение: обход электронной защиты
Guardant и восстановление алгоритмов обработки сигналов в задачу не входят.

| Инструмент | Что делает | Чего не делает |
|---|---|---|
| `pe_resources.py` | перечень ресурсов PE и разбор форм Delphi (DFM) | не разбирает код модуля |
| `strings_extract.py` | строки ASCII, UTF-8 и UTF-16LE, фильтр по ключевым словам | не привязывает строки к коду |
| `firebird_meta.py` | лексическая выборка имён таблиц, колонок и генераторов базы | не разбирает ODS, типы и длины колонок |
| `msi_tables.py` | потоки установочного пакета и извлечённые строки | имена таблиц MSI искажены, продуктовые записи не восстанавливаются |

## Примеры команд

Путь к дистрибутиву в репозитории: `CD Ligtht 32314`.

```bash
# Перечень ресурсов и форм основного модуля
EXE="CD Ligtht 32314/Program Files/32314/data/OFFLINE/ACDF2713/1446A09E/InspectorRT.exe"
python3 tools/analysis/pe_resources.py --exe "$EXE" --forms
python3 tools/analysis/pe_resources.py --exe "$EXE" --form TFRSIGNALSLIST

# Надписи интерфейса по ключевому слову
python3 tools/analysis/strings_extract.py --exe "$EXE" --cyrillic --grep эталон
python3 tools/analysis/strings_extract.py --exe "$EXE" --count

# Сущности базы планирования и базы частотных запросов
python3 tools/analysis/firebird_meta.py --db "CD Ligtht 32314/Файлы пустых баз данных/Файлы пустых баз данных/TASKPLAN.ITASKDB"

# Строки установочного пакета
python3 tools/analysis/msi_tables.py --msi "CD Ligtht 32314/Program Files/32314/SetupRadioInspectorLight.msi" --strings --limit 40
```

Хеш-каталоги в `data/OFFLINE` меняются от сборки к сборке, поэтому путь к
`InspectorRT.exe` стоит находить поиском:

```bash
find "CD Ligtht 32314" -name InspectorRT.exe
```

## Соответствие разделам инвентаря

| Раздел инвентаря | Инструмент | Команда |
|---|---|---|
| 2. Исполняемые модули и библиотеки | — | `find "CD Ligtht 32314" -type f -size +1M` |
| 3. Формы программы | `pe_resources.py` | `--forms`, `--form <имя>` |
| 4. Функциональный периметр | `strings_extract.py` | `--cyrillic --grep <слово>` |
| 5. Реестр приёмников | `strings_extract.py` | `--grep UCScaner_` |
| 6. Модель данных | `firebird_meta.py` | `--db <файл базы>` |
| 7. Лицензирование | `strings_extract.py` | `--grep Guardant` |
| 8. Что осталось неразобранным | `msi_tables.py` | `--streams`, `--strings` |
| 9. Тексты справки `.hlp` | `hlp_reader.py` | `python3 tools/analysis/hlp_reader.py файл.hlp` |

## Проверка самих инструментов

Тесты разбора лежат в `tests/test_analysis_tools.py` и не требуют дистрибутива:
часть проверяет разбор синтетической формы, часть пропускается, если
коммерческий диск отсутствует в рабочей копии.

```bash
python3 -m unittest tests.test_analysis_tools -v
```

## Разбор справки `.hlp`

`hlp_reader.py` читает файлы справки WinHelp: заголовок файла, внутренний
каталог в виде B+-дерева, блоки тем `|TOPIC` с распаковкой HLP-LZ77 и таблицу
фраз `|Phrases`, после чего выдаёт заголовки и тексты тем.

```bash
python3 tools/analysis/hlp_reader.py "CD Ligtht 32314/Program Files/32314/data/OFFLINE"/*/*/*.hlp
python3 tools/analysis/hlp_reader.py файл.hlp --json
```

Формат описан в документации X-Ways «Windows Help File Format» (то же
описание использует программа helpdeco).
