# Измерения и экспорт из Python CLI

Демонстрационный сканер создаёт локальную базу панорам и выводит её путь. В терминале с активным окружением Python выполните из корня проекта следующие команды.

```sh
python -m pip install -e .
python -m radioinspector demo --output ./demo-data --cycles 60
```

Для примеров ниже присвойте переменной `PAN` выведенный путь к файлу `.pan`. В POSIX-оболочке при единственном учебном файле в каталоге можно использовать такие команды.

```sh
set -- ./demo-data/*.pan
PAN="$1"
python -m radioinspector occupancy --database "$PAN" --threshold -80
python -m radioinspector measure --database "$PAN" --start 101.8e6 --stop 103.2e6 --json
python -m radioinspector export --database "$PAN" --format cef --output ./demo-data/out.cef
python -m radioinspector serve --database "$PAN"
```

Веб-сервер открывается по адресу `http://127.0.0.1:8770/`. Завершите его сочетанием Ctrl+C. Параметр `--database` выбирает библиотечную базу панорам. Для сеансов, потоков и событий Python-сервера предназначен отдельный параметр `--session-database`. [Модель данных Python](architecture.md) · [Архив API](archive-ingest.md)

Команда `python -m radioinspector --help` перечисляет также работу с реестром сигналов, частотными присвоениями, географией, заданиями и протоколами измерений. Для параметров конкретной операции добавьте `--help` после её имени. [Методики измерения](measurement-methods.md) · [Формирование отчётов](reporting.md)
