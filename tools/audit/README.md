# Инструменты аудита исходного кода

Статический анализатор на стандартной библиотеке Python: внешние линтеры
(ruff, pyflakes, pylint, mypy, bandit) в окружении проекта недоступны, поэтому
правила реализованы поверх `ast`. Инструмент не требует установки пакетов и
работает одинаково в песочнице, в CI и на рабочей машине.

## Запуск

```bash
python3 tools/audit/code_checks.py radioinspector
python3 tools/audit/code_checks.py radioinspector --rule ASSERT-IN-PROD
python3 tools/audit/code_checks.py radioinspector tools/release
```

Вывод: по одной строке на находку — правило, файл, строка, пояснение — и
сводка по числу находок в конце.

## Правила

| Правило | Что означает |
|---|---|
| `ASSERT-IN-PROD` | `assert` в рабочем коде: снимается флагом `-O` |
| `BARE-EXCEPT` | `except` без типа: глушит любые ошибки |
| `SWALLOW-EXC` | `except Exception` без логирования или с `pass` |
| `MUTABLE-DEFAULT` | изменяемый аргумент по умолчанию |
| `DYNAMIC-CODE` | `eval`, `exec`, `compile` (ложное срабатывание на `re.compile`) |
| `UNSAFE-PICKLE` | `pickle`, `marshal`, `shelve`: десериализация без проверки |
| `SHELL` | `os.system` и `subprocess` с `shell=True` |
| `SECRET` | строковый литерал, похожий на пароль, токен или ключ |
| `UNUSED-IMPORT` | импорт, на который нет ссылок в модуле |
| `COMPLEXITY` | функция с 12 и более ветвлениями |
| `LONG-FUNC` | функция длиннее 150 строк |
| `GLOBAL` | объявление `global` |

## Границы применения

Правила `COMPLEXITY`, `LONG-FUNC` и `SWALLOW-EXC` — эвристики, а не дефекты:
часть находок осмысленна (подавление исключения в пути очистки ресурсов).
Инструмент предназначен для повторяемого контроля регрессий, а не для
автоматической оценки качества кода.

Аудит, выполненный этим инструментом 13 ноября 2026 года, зафиксирован в
[`docs/code-audit-2026-11-13.md`](../../docs/code-audit-2026-11-13.md).

## Проверки

```bash
python3 -m unittest tests.test_code_quality -v
```
