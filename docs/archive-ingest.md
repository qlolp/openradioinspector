# Внешний импорт измерений в общий архив

`radioinspector.archive_ingest` передаёт подготовленные sweeps и observations в существующий сеанс только через `/api/v1`. Сборщик не открывает SQLite или приборные порты: единственным писателем остаётся серверный `SessionService`.

После установки проекта доступна команда `radioinspector-archive-import`. Без установки entry point используется эквивалент `python3 -m radioinspector.archive_ingest`.

## Формат версии 1

Вход — строгий JSON с обязательными `format` и `format_version`; неизвестные поля отклоняются. Каждое измерение должно иметь стабильный непустой `id`. Поле `session_id` внутри записи можно опустить, но если оно задано, то должно совпадать с `--session`.

```json
{
  "format": "openradioinspector-archive",
  "format_version": 1,
  "sweeps": [
    {
      "id": "external-sweep-0001",
      "stream_id": "TINYSA_STREAM_ID",
      "interval": {
        "started_at": "2026-11-05T10:00:00Z",
        "ended_at": "2026-11-05T10:00:01Z"
      },
      "frequencies_hz": [100000000, 100100000],
      "levels": [-80.5, -42.0],
      "level_unit": "dBm"
    }
  ],
  "observations": [
    {
      "id": "external-observation-0001",
      "stream_id": "RECEIVER_STREAM_ID",
      "interval": {
        "started_at": "2026-11-05T10:00:00Z",
        "ended_at": "2026-11-05T10:00:01Z"
      },
      "frequency_hz": 145500000,
      "mode": "FM",
      "level": 37,
      "level_unit": "s_meter_count",
      "squelch_open": true
    }
  ]
}
```

Частоты задаются целыми Гц, время — с часовым поясом и нормализуется к UTC. `dBm` и `s_meter_count` сохраняются без взаимного пересчёта. NaN и Infinity запрещены во всём документе, включая metadata.

## Запуск

```bash
# Проверить документ, capabilities, сеанс, потоки и единицы без POST.
python3 -m radioinspector.archive_ingest \
  --server http://127.0.0.1:8770 \
  --session SESSION_ID --input archive.json --dry-run

# Импортировать пакетами по 50 записей; вместо файла можно указать - для stdin.
python3 -m radioinspector.archive_ingest \
  --server http://127.0.0.1:8770 \
  --session SESSION_ID --input archive.json --batch-size 50
```

До первого POST проверяются весь документ, `measurement_batch_ingest=true`, `measurement_batch_atomic=true`, серверный `measurement_batch_limit`, существование сеанса, принадлежность каждого потока и точное совпадение `level_unit`. Поэтому ошибка даже в последней записи или чужой поток не позволяет записать предыдущие пакеты. `--batch-size` может только уменьшить серверный лимит.

Используются только `GET /api/v1/capabilities`, `GET /api/v1/sessions/{id}` и существующие `POST .../sweeps/batch` или `observations/batch`. Порядок записей: сначала sweeps, затем observations; порядок внутри каждого массива сохраняется. Ответ каждого пакета сверяется по числу и ID.

## Атомарность и повторный запуск

Результат содержит `sweeps`, `observations`, `batches`, `batch_size`, `dry_run`, `atomic_per_batch=true` и `whole_file_atomic=false`.

**Атомарен каждый HTTP-пакет, а не весь много-пакетный файл.** В SQLite пакет и его measurement-события сохраняются одной транзакцией. При сетевом сбое после уже подтверждённых пакетов их записи остаются в архиве. Повторный импорт не пропускает дубликаты молча и не перезаписывает измерения: сервер отклоняет конфликтующий пакет.

Текущий формат загружается в память целиком; это не потоковый импорт неограниченного backlog и не возобновляемый checkpoint. Один HTTP-ответ сборщика ограничен 32 МБ. Для больших архивов нужен отдельный потоковый формат и протокол resume.

## Проверка

`tests/test_archive_ingest.py` проверяет строгий формат, стабильные ID, целые Гц, исходные единицы, chunking, пределы сервера, отсутствие POST при любой preflight-ошибке, dry-run, ответы с другими ID и CLI. Сквозной сценарий использует реальный HTTP-сервер и SQLite, затем перезапускает службу и проверяет восстановление sweeps, observations и событий. Это программная проверка, не аппаратный стенд.
