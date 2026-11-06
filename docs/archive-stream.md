# Потоковый NDJSON-импорт с resume

`radioinspector.archive_stream` предназначен для больших архивов, которые не следует загружать в память целиком. Перед проверкой создаётся временный snapshot входного файла, после чего используются два аналитических прохода только по нему: полный preflight с SHA-256 и импорт порциями через существующий атомарный batch API. Изменение исходного файла после preflight не влияет на импорт. В памяти остаются только текущий пакет и множества ID для поиска дубликатов.

## Формат

Первая непустая строка — обязательный header. Далее каждая строка содержит ровно один `sweep` или `observation` с постоянным ID.

```json
{"format":"openradioinspector-archive-ndjson","format_version":1,"session_id":"SESSION_ID"}
{"kind":"sweep","measurement":{"id":"sweep-1","stream_id":"TINYSA_STREAM_ID","interval":{"started_at":"2026-11-05T10:00:00Z","ended_at":"2026-11-05T10:00:01Z"},"frequencies_hz":[100000000,100100000],"levels":[-80.5,-42.0],"level_unit":"dBm"}}
{"kind":"observation","measurement":{"id":"observation-1","stream_id":"RECEIVER_STREAM_ID","interval":{"started_at":"2026-11-05T10:00:00Z","ended_at":"2026-11-05T10:00:01Z"},"frequency_hz":145500000,"mode":"FM","level":37,"level_unit":"s_meter_count"}}
```

`session_id` внутри measurement можно опустить. Частоты остаются целыми Гц, время — UTC, исходные единицы не преобразуются. Неизвестные поля header/record, `1e999`, NaN и Infinity отклоняются.

## Запуск

```bash
# Полная проверка без POST и checkpoint.
python3 -m radioinspector.archive_stream \
  --server http://127.0.0.1:8770 --session SESSION_ID \
  --input archive.ndjson --dry-run

# Импорт с checkpoint после каждого подтверждённого пакета.
python3 -m radioinspector.archive_stream \
  --server http://127.0.0.1:8770 --session SESSION_ID \
  --input archive.ndjson --batch-size 100 --checkpoint archive.resume.json

# Явное продолжение после перезапуска.
python3 -m radioinspector.archive_stream \
  --server http://127.0.0.1:8770 --session SESSION_ID \
  --input archive.ndjson --checkpoint archive.resume.json --resume
```

После установки проекта доступен entry point `radioinspector-archive-stream`. Обычный запуск при уже существующем checkpoint останавливается и требует явное `--resume`. Checkpoint содержит session_id, `destination` (нормализованный base_url узла), SHA-256 snapshot, `next_line`, счётчики записей/пакетов и `complete`. Отсутствующий checkpoint при `--resume` является ошибкой `checkpoint_not_found`, а не командой начать сначала. Изменение файла, сеанса или узла назначения отклоняется: другой узел с теми же session/stream IDs не может использовать чужой прогресс. Полное resume не отправляет POST.

## Гарантии и окно сбоя

Первый проход проверяет весь файл, capabilities, сеанс, потоки и исходные единицы до первого POST. Поэтому ошибка в последней строке не создаёт частичный архив. Второй проход сохраняет порядок записей, формирует пакеты одного типа не больше серверного лимита и сверяет количество и ID ответа.

Checkpoint перезаписывается атомарным replace **только после подтверждённого пакета**. Если процесс остановлен между подтверждёнными пакетами, resume пропускает их. Как и раньше, атомарен каждый пакет, а не весь файл.

Если сервер подтвердил пакет, но процесс завершился до записи checkpoint, следующий resume может повторно отправить этот пакет и получить duplicate. Это окно не маскируется как exactly-once: ошибка останавливает импорт и требует разбора, а не молчаливого пропуска записей. Полный документ в память не загружается, но SHA-256 и preflight всё равно требуют двух аналитических проходов по snapshot и набора ID каждого типа. Это программная проверка, не аппаратный стенд.
