# Read-only экспорт общего архива

`GET /api/v1/sessions/{id}/archive/export` возвращает в `data` документ `openradioinspector-archive`, версия 1, совместимый с `radioinspector-archive-import`. Экспорт содержит все sweeps и observations сеанса, а не только последние 100 или 1000 записей обычных list-маршрутов. Необязательный query `stream_id` ограничивает документ одним потоком того же сеанса.

```bash
curl -sS 'http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/archive/export'
curl -sS 'http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/archive/export?stream_id=TINYSA_STREAM_ID'
```

Capabilities сообщают `session_archive_export=true` и `session_archive_export_archive_wide=true`. Неизвестный сеанс возвращает 404, чужой поток — 400. Пустой архив является допустимым документом с пустыми массивами.

## CLI

```bash
# Вывести документ в stdout.
python3 -m radioinspector.archive_export \
  --server http://127.0.0.1:8770 --session SESSION_ID

# Сохранить один поток в файл.
python3 -m radioinspector.archive_export \
  --server http://127.0.0.1:8770 --session SESSION_ID \
  --stream-id TINYSA_STREAM_ID --output archive.json
```

После установки проекта доступен entry point `radioinspector-archive-export`. Клиент сначала получает и полностью проверяет документ, включая формат, ID, целые Гц, UTC и конечность всех вложенных чисел, и только затем открывает выходной файл. Ошибка контракта не создаёт новый файл.

## Круговой перенос

Файл можно передать другому узлу через существующий импортёр:

```bash
python3 -m radioinspector.archive_ingest \
  --server http://127.0.0.1:8771 --session SESSION_ID --input archive.json
```

На принимающем узле должен уже существовать сеанс с теми же session/stream IDs и исходными `level_unit`. Повторный импорт в непустой архив не заменяет существующие записи: конфликтующий пакет отклоняется. Как и прежде, атомарен каждый пакет, а не весь много-пакетный импорт.

Экспорт только читает архив: sweeps и observations образуют один снимок под общим lock, конкурентный writer не вклинивается между ними. Он не создаёт измерения или события, не меняет workers и runtime-политики. Исходные `dBm` и `s_meter_count` не преобразуются. Полный документ материализуется в памяти сервера и клиента; фиксированного 32-МБ response-limit у экспорта нет, но потоковый экспорт неограниченного backlog и resume в этом срезе не реализованы. Это программная функция переноса, не аппаратная проверка.
