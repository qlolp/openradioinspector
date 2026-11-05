# API платформы, версия 1

API предназначен для Raspberry Pi WebUI, Windows GUI, macOS и других клиентов общего ядра. Существующие маршруты `/api/*` сохраняются и не входят в этот контракт.

## Общие правила

- Префикс: `/api/v1`.
- Ответы обёрнуты в объект с `contract`, `contract_version` и `data`.
- Версия контракта текущего среза: `1`.
- Ошибки содержат `error.code` и `error.message`.
- Частоты — целые Гц, не числа с плавающей точкой в МГц.
- Время — ISO 8601 в UTC с суффиксом `Z`.
- Измерение содержит `started_at` и `ended_at`.
- Единица исходного уровня обязательна. `s_meter_count` не преобразуется в `dBm` автоматически.
- Без `--session-database` данные хранятся в памяти и удаляются при перезапуске. С этим параметром сеансы, события, измерения, кандидаты и исследования сохраняются в SQLite и восстанавливаются; фактический режим виден в `/api/v1/capabilities`.

## Маршруты

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/v1/health` | Состояние службы, число сеансов, последний ID события |
| `GET` | `/api/v1/capabilities` | Фактические возможности и режим хранения |
| `POST` | `/api/v1/sessions` | Создание сеанса с независимыми потоками |
| `GET` | `/api/v1/sessions` | Список сеансов |
| `GET` | `/api/v1/sessions/{id}` | Один сеанс |
| `POST` | `/api/v1/sessions/{id}/start` | Запуск сеанса из `draft` |
| `POST` | `/api/v1/sessions/{id}/stop` | Остановка сеанса из `running` и всех его исполнителей |
| `POST` | `/api/v1/sessions/{id}/workers/start` | Запуск поддерживаемых симуляторов всех потоков |
| `POST` | `/api/v1/sessions/{id}/workers/stop` | Остановка исполнителей без изменения жизненного цикла сеанса |
| `GET` | `/api/v1/sessions/{id}/workers` | Состояние исполнителей сеанса |
| `GET` | `/api/v1/workers` | Состояние исполнителей всех сеансов |
| `POST` | `/api/v1/sessions/{id}/streams/{stream_id}/start` | Запуск одного исполнителя |
| `POST` | `/api/v1/sessions/{id}/streams/{stream_id}/stop` | Остановка одного исполнителя |
| `POST` | `/api/v1/sessions/{id}/streams/{stream_id}/tune` | Команда перестройки приёмника через его очередь |
| `POST` | `/api/v1/sessions/{id}/sweeps` | Приём спектрального свипа |
| `GET` | `/api/v1/sessions/{id}/sweeps?stream_id=&limit=100` | Последние свипы сеанса или потока |
| `POST` | `/api/v1/sessions/{id}/observations` | Приём наблюдения приёмника |
| `GET` | `/api/v1/sessions/{id}/observations?stream_id=&limit=100` | Последние наблюдения сеанса или потока |
| `POST` | `/api/v1/sessions/{id}/candidates` | Создание кандидата в исходных единицах потока |
| `GET` | `/api/v1/sessions/{id}/candidates?state=` | Список кандидатов сеанса |
| `GET` | `/api/v1/sessions/{id}/candidates/{candidate_id}` | Один кандидат |
| `POST` | `/api/v1/sessions/{id}/candidates/{candidate_id}/investigations` | Ручная постановка в очередь исследований |
| `POST` | `/api/v1/sessions/{id}/candidates/{candidate_id}/dismiss` | Отклонение кандидата без открытого исследования |
| `GET` | `/api/v1/sessions/{id}/investigations?state=` | Очередь и история исследований |
| `GET` | `/api/v1/sessions/{id}/investigations/{investigation_id}` | Одно исследование |
| `POST` | `/api/v1/sessions/{id}/investigations/{investigation_id}/start` | Настройка выбранного receiver и начало прослушивания |
| `POST` | `/api/v1/sessions/{id}/investigations/{investigation_id}/complete` | Завершение со связыванием наблюдений результата |
| `POST` | `/api/v1/sessions/{id}/investigations/{investigation_id}/fail` | Завершение сбоем с обязательным `error` |
| `POST` | `/api/v1/sessions/{id}/investigations/{investigation_id}/cancel` | Отмена открытого исследования |
| `GET` | `/api/v1/events/history?after=0&limit=100` | История событий |
| `GET` | `/api/v1/events` | SSE-поток событий с replay |

## Создание двухприборного сеанса

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Двойной обзор",
    "streams": [
      {
        "device": "tinySA Ultra",
        "role": "spectrum_analyzer",
        "level_unit": "dBm",
        "port": "/dev/ttyACM0",
        "capabilities": ["sweep", "waterfall"]
      },
      {
        "device": "AR-DV1",
        "role": "receiver",
        "level_unit": "s_meter_count",
        "port": "/dev/ttyUSB0",
        "capabilities": ["tune", "audio", "hardware_decode"]
      }
    ],
    "metadata": {"antennas": "independent"}
  }'
```

Порт назначается только одному потоку сеанса. Идентификаторы сеанса и потоков создаются сервером, если не переданы явно. ID потока уникален во всей службе и SQLite-архиве: повторное использование в другом сеансе возвращает HTTP 400, не изменяя существующий сеанс или его измерения. ID сеанса в URL необходимо кодировать как один сегмент (например, `a/b` → `a%2Fb`); сервер декодирует его ровно один раз. Перечисленные в примере capabilities являются метаданными профиля, а не подтверждением того, что сервер уже выполняет аппаратные команды.

## Жизненный цикл

```text
draft → running → stopped
```

Создание всегда начинается с `draft`. Допускается явно передать `state: "draft"`; другое состояние и непустые `created_at`, `started_at`, `ended_at` при создании возвращают HTTP 400. Время создания назначает служба, время запуска и остановки меняется только через соответствующие операции. Восстановление из архива использует отдельный путь десериализации и сохраняет записанный жизненный цикл.

В SQLite создание, запуск, остановка и приём каждого измерения записываются вместе со своим событием в одной транзакции. История в памяти и SSE-подписчики получают событие только после успешного commit. При ошибке записи или прерывании процесса незавершённая операция не появляется после перезапуска.

Недопустимый переход возвращает HTTP 400. Обращение к неизвестному сеансу — HTTP 404 с кодом `session_not_found`. Остановка сеанса также отменяет все его открытые исследования и не оставляет активное состояние без работающего сеанса.

## SSE и восстановление подписки

Каждый кадр содержит монотонный `id`, тип `event` и JSON в `data`:

```text
id: 2
event: session.started
data: {"contract":"openradioinspector-platform","contract_version":1,"data":{...}}
```

Клиент сохраняет последний обработанный ID. При переподключении сервер повторяет более новые события из ограниченного журнала:

```bash
curl -N http://127.0.0.1:8770/api/v1/events -H 'Last-Event-ID: 2'
```

Также допускается `GET /api/v1/events?after=2`. Для получения истории без постоянного соединения используется `/api/v1/events/history`.

Если медленный клиент переполняет индивидуальную очередь, приборный поток не блокируется: для этого клиента отбрасывается самое старое ожидающее событие. После переподключения доступные события запрашиваются через replay.

## Независимые исполнители и симуляторы

Исполнители запускаются только для потоков с явным `settings.adapter`:

- `tinysa-sim` — периодические свипы в `dBm`, целые частоты в Гц, генератор всегда `off`;
- `ardv1-sim` — наблюдения в исходных `s_meter_count`, настройка частоты и режима через индивидуальную очередь команд.

Адаптер не выбирается автоматически по названию прибора или строке порта. Физические порты этот контур не открывает, поэтому `hardware_control` остаётся `false`; дополнительные capabilities `simulated_device_workers`, `independent_worker_commands` и `continuous_simulated_monitoring` относятся только к программному стенду.

Пример настроек двух потоков при создании сеанса:

```json
{
  "name": "Непрерывный стенд",
  "streams": [
    {
      "device": "tinySA Ultra",
      "role": "spectrum_analyzer",
      "level_unit": "dBm",
      "port": "sim://tinysa",
      "settings": {
        "adapter": "tinysa-sim",
        "start_hz": 88000000,
        "stop_hz": 108000000,
        "points": 201,
        "rbw_hz": 10000,
        "period_s": 0.25,
        "seed": 7
      }
    },
    {
      "device": "AR-DV1",
      "role": "receiver",
      "level_unit": "s_meter_count",
      "port": "sim://ardv1",
      "settings": {
        "adapter": "ardv1-sim",
        "frequency_hz": 145500000,
        "mode": "FM",
        "squelch": 10,
        "period_s": 0.25,
        "seed": 17
      }
    }
  ]
}
```

После создания сеанс и его исполнители запускаются раздельно:

```bash
curl -sS -X POST http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/start
curl -sS -X POST http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/workers/start
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/workers
```

Перестройка AR-DV1 выполняется командой только его очереди:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/streams/RECEIVER_STREAM_ID/tune \
  -H 'Content-Type: application/json' \
  -d '{"frequency_hz": 433075000, "mode": "DMR"}'
```

Остановка одного потока не меняет состояние другого:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/streams/TINYSA_STREAM_ID/stop
```

Каждый исполнитель сообщает `state`, `alive`, число измерений и команд, последнюю ошибку и текущие настройки. Временный сбой переводит поток в `degraded`; после успешного измерения он возвращается в `running`. После `max_consecutive_errors` подряд только сбойный исполнитель завершается в `error`. События `device.state`, `device.error` и `device.tuned` доступны через историю и SSE.

При перезапуске сервера сеансы и измерения восстанавливаются из SQLite, но потоки операционной системы автоматически не пересоздаются: состояния приборных потоков сбрасываются в `disconnected`, повторный запуск выполняется явно через API. Остановка сеанса сначала завершает все его исполнители.

Для проверки отказоустойчивости симуляторы принимают `failures_remaining` и `max_consecutive_errors`; эти параметры предназначены только для стенда и тестов.

## Приём измерений

`session_id` можно опустить в теле: сервер подставит его из маршрута. Если поле передано и не совпадает с маршрутом, возвращается HTTP 400. `stream_id` обязан принадлежать этому сеансу, а `level_unit` — точно совпадать с единицей потока. Это исключает незаметное преобразование отсчётов S-метра в дБм и запись одного прибора в поток другого.

Пример свипа tinySA:

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/sweeps \
  -H 'Content-Type: application/json' \
  -d '{
    "stream_id": "TINYSA_STREAM_ID",
    "interval": {
      "started_at": "2026-11-05T10:00:00Z",
      "ended_at": "2026-11-05T10:00:01Z"
    },
    "frequencies_hz": [100000000, 100100000],
    "levels": [-80.5, -42.0],
    "level_unit": "dBm",
    "settings": {"rbw_hz": 10000}
  }'
```

Пример наблюдения AR-DV1:

```json
{
  "stream_id": "RECEIVER_STREAM_ID",
  "interval": {
    "started_at": "2026-11-05T10:00:00Z",
    "ended_at": "2026-11-05T10:00:01Z"
  },
  "frequency_hz": 145500000,
  "mode": "FM",
  "level": 37,
  "level_unit": "s_meter_count",
  "squelch_open": true,
  "decoded_mode": "DMR"
}
```

Успешный приём возвращает HTTP 201 и событие `sweep.received` или `observation.received` в SSE. Выборка возвращает последние `limit` записей по времени начала измерения в хронологическом порядке (при равном времени — по ID), независимо от порядка поступления; допустимы значения от 1 до 1000. SQLite и режим памяти учитывают микросекунды одинаково. Старые архивные строки без дробной части секунды сортируются корректно без переписывания измерений.

## Кандидаты и ручной handoff

Реестр кандидатов и очередь исследований работают в memory-only и SQLite режимах. Это **ручной** handoff: сервер не выделяет кандидатов автоматически и сам не выбирает следующее исследование. Capabilities раздельно сообщают `candidate_investigations=true`, `manual_investigation_handoff=true`, `automatic_candidate_detection=false`, `semi_automatic_handoff=false` и `automatic_handoff=false`.

Состояния кандидата:

```text
new → queued → investigating → resolved
  │       │            │
  │       │            ├─ после failed/cancelled → queued
  │       └────────────┘
  └──────────────→ dismissed
```

`dismiss` допускается для `new` или для `queued` без открытого исследования. Состояния `resolved` и `investigating` нельзя перевести в `dismissed`.

Создание кандидата:

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/candidates \
  -H 'Content-Type: application/json' \
  -d '{
    "source_stream_id": "TINYSA_STREAM_ID",
    "interval": {
      "started_at": "2026-11-05T10:00:00Z",
      "ended_at": "2026-11-05T10:00:01Z"
    },
    "center_frequency_hz": 102500000,
    "bandwidth_hz": 200000,
    "peak_level": -42.5,
    "level_unit": "dBm",
    "score": 0.9
  }'
```

`source_stream_id` обязан принадлежать сеансу, а `level_unit` — точно совпадать с потоком. Уровень кандидата сохраняется в исходных `dBm` или `s_meter_count`; преобразование между ними не выполняется. Новый кандидат всегда имеет `state=new`.

Постановка в очередь:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/candidates/CANDIDATE_ID/investigations \
  -H 'Content-Type: application/json' \
  -d '{"target_stream_id":"RECEIVER_STREAM_ID","mode":"DMR","priority":5}'
```

Целью может быть только `receiver` того же сеанса. `target_frequency_hz` по умолчанию равен `candidate.center_frequency_hz`; `priority` — неотрицательное целое, по умолчанию `0`. В списке ожидающие исследования сортируются по убыванию приоритета, затем по `created_at` и ID. Для одного кандидата допускается только одно открытое исследование (`queued`, `tuning`, `listening` или `decoding`). После сбоя или отмены кандидат возвращается в `queued` и может быть поставлен повторно новым ID исследования.

Запуск и завершение:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/investigations/INVESTIGATION_ID/start

curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/investigations/INVESTIGATION_ID/complete

curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/investigations/INVESTIGATION_ID/fail \
  -H 'Content-Type: application/json' \
  -d '{"error":"оператор зафиксировал сбой"}'
```

Основной путь исследования:

```text
queued → tuning → listening → completed
           │          │
           └──────────┴→ failed
queued / tuning / listening → cancelled
```

Перед командой служба сохраняет `tuning`, затем освобождает общий lock и вызывает только очередь целевого receiver. Поэтому свипы tinySA и операции других потоков продолжаются во время перестройки. При успехе исследование становится `listening`, кандидат — `investigating`. Если команда завершилась ошибкой, ответ содержит исследование в `failed` с текстом `error`, а кандидат снова доступен для постановки. Отмена во время команды не перезаписывается последующим `listening`; уже выполненная перестройка прибора не откатывается. Автоматического восстановления прежней частоты нет.

`complete` принимает только `listening`/`decoding` и связывает все наблюдения целевого потока, у которых `frequency_hz=target_frequency_hz`, а `started_at` не раньше начала исследования. Если таких наблюдений нет, возвращается HTTP 400, состояние остаётся `listening` и ложный результат не создаётся. При успехе ID наблюдений попадают в `result_observation_ids`, исследование — в `completed`, кандидат — в `resolved`. Состояние `decoding` зарезервировано контрактом; отдельный HTTP-переход в него в этом срезе не добавлен.

События `candidate.created`, `candidate.dismissed`, `investigation.queued`, `investigation.tuning`, `investigation.listening`, `investigation.completed`, `investigation.failed`, `investigation.cancelled` и `investigation.interrupted` доступны через history/SSE. В SQLite изменение кандидата, исследования и соответствующего события выполняется одной транзакцией; подписчики уведомляются только после commit.

После перезапуска ожидающие `queued` сохраняются. Активные `tuning`/`listening`/`decoding` переводятся в `failed` с ошибкой прерывания и событием `investigation.interrupted`, поскольку поток команды не переживает процесс; кандидат возвращается в `queued`. Приборные workers при этом не перезапускаются автоматически.

ID кандидата и исследования в URL кодируются как один сегмент и декодируются ровно один раз, как ID сеанса. Неизвестные объекты возвращают HTTP 404 с кодами `candidate_not_found` и `investigation_not_found`.

`hardware_control` остаётся `false`: ручной handoff и модели архива проверены на симуляторах, но не означают подключение или аппаратную проверку физических tinySA Ultra/AR-DV1.
