# Примеры

| Файл | Назначение |
|---|---|
| `assignments_sample.csv` | Пример базы данных частотных присвоений для импорта |
| `protocol_template.md` | Шаблон протокола измерений |
| `example_scan.py` | Программное сканирование диапазона с записью в базу панорам |

## Импорт базы присвоений

```bash
python3 -m radioinspector assignments --database assignments.db \
    --import-csv examples/assignments_sample.csv --list
```

## Формирование протокола по собственному шаблону

```bash
python3 -m radioinspector report --database /tmp/oridemo/<файл>.pan \
    --template examples/protocol_template.md \
    --operator "Иванов И. И." --place "Москва, ул. Примерная, 1" \
    --output protocol.md
```

## Программное сканирование

```bash
python3 examples/example_scan.py --output /tmp/example-scan --cycles 30
```

Если пакет не установлен в окружение, укажите путь к исходникам:

```bash
PYTHONPATH=. python3 examples/example_scan.py --output /tmp/example-scan --cycles 30
```
