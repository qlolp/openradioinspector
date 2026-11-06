# Потоковый NDJSON-экспорт архива

`GET /api/v1/sessions/{id}/archive/export.ndjson` возвращает версионированный NDJSON-документ, совместимый с `radioinspector-archive-stream` и его resume checkpoint. В отличие от `/archive/export`, этот маршрут не собирает JSON-документ целиком.

## HTTP

```bash
curl -sS 'http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/archive/export.ndjson' -o archive.ndjson
curl -sS 'http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/archive/export.ndjson?stream_id=TINYSA_STREAM_ID' -o tinysa.ndjson
```

Первая строка содержит `format=openradioinspector-archive-ndjson`, `format_version=1` и `session_id`. Далее идут записи `sweep` и `observation` в хронологическом порядке, затем ID при равном времени. Частоты остаются целыми Гц, время — UTC, `dBm` и `s_meter_count` не преобразуются.

Headers `X-Archive-Sweeps` и `X-Archive-Observations` содержат число записей в том же snapshot. Клиент проверяет их после полного разбора потока. Capabilities сообщают `session_archive_export_ndjson=true` и `session_archive_export_streaming=true`. Неизвестный сеанс возвращает 404, чужой поток — 400. Пустой архив состоит только из header.

## Snapshot и память

Для SQLite ядро создаёт backup в временном файле на диске под блокировкой writer, после чего освобождает её. Строки читаются пакетами от 1 до 1000; полный архив в Python-списки не материализуется. Временный snapshot удаляется после ответа. Поэтому измерения, записанные после создания snapshot, не попадают в текущий экспорт и не меняют его headers.

Memory-only режим использует неизменную копию уже находящегося в памяти архива. Это не обещание ограниченной памяти для memory-only backend.

Ответ закрывается соединением и не имеет `Content-Length`. Обрыв соединения, невалидная запись или несовпадение счётчиков не считается успешным экспортом.

## CLI

```bash
python3 -m radioinspector.archive_export_stream \
  --server http://127.0.0.1:8770 --session SESSION_ID --output archive.ndjson
```

После установки проекта доступен `radioinspector-archive-export-stream`. Клиент сначала сохраняет поток во временный файл, полностью проверяет его тем же сканером, что streaming importer, сравнивает счётчики и только затем атомарно заменяет выходной файл. Существующий файл не перезаписывается при ошибке или обрыве. `--output -` выводит проверенный документ в stdout.

## Импорт и resume

```bash
python3 -m radioinspector.archive_stream \
  --server http://127.0.0.1:8771 --session SESSION_ID \
  --input archive.ndjson --checkpoint archive.resume.json
```

На принимающем узле нужен сеанс с теми же session/stream IDs и исходными единицами. Повторный запуск с `--resume` использует SHA-256 и пропускает подтверждённые пакеты. Экспорт read-only: он не создаёт измерений, событий или команд workers и не меняет runtime-политики. Exactly-once перенос между независимыми узлами не заявляется.
