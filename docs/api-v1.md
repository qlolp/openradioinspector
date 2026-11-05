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
| `GET` | `/api/v1/sessions/{id}/investigations/next` | Следующая рекомендация без изменения состояния |
| `POST` | `/api/v1/sessions/{id}/investigations/start-next` | Запуск рекомендации после явного подтверждения |
| `GET` | `/api/v1/sessions/{id}/automatic-handoff` | Runtime-политика и состояние автоматического планировщика |
| `POST` | `/api/v1/sessions/{id}/automatic-handoff` | Частичное изменение политики; opt-in через `enabled=true` |
| `GET` | `/api/v1/sessions/{id}/automatic-detection` | Runtime-политика и состояние обнаружения кандидатов |
| `POST` | `/api/v1/sessions/{id}/automatic-detection` | Частичное изменение политики; opt-in через `enabled=true` |
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

## Raspberry Pi WebUI

`GET /platform` и `GET /platform.html` открывают панель общего сеанса поверх этого API. Прежний интерфейс панорам остаётся на `/`. Панель выбирает уже созданный сеанс и использует только перечисленные REST/SSE-маршруты; создание сеанса выполняется через `POST /api/v1/sessions`. Внешних клиентских зависимостей нет.

Основной сценарий: запустить сеанс и исполнители, выбрать поток спектрального обзора, зафиксировать фактический пик свипа кандидатом, назначить receiver того же сеанса, поставить исследование в очередь, запустить настройку и завершить его после появления подходящих наблюдений. Доступны также ручное создание и отклонение кандидата, независимые start/stop потоков, fail/cancel исследования и фильтры записей. Для полуавтоматического handoff панель отдельно запрашивает `/investigations/next`, показывает рекомендацию и отправляет `/investigations/start-next` только после нажатия кнопки подтверждения. Отдельная форма автоматического handoff отправляет явное `enabled=true` с параметрами политики, показывает runtime-статус и позволяет выключить планировщик; до такого действия очередь не запускается автоматически. Браузер не синтезирует свипы, наблюдения или успешные результаты.

Подписка использует named SSE-события и replay по `Last-Event-ID`; измерительные события коалесцируются, резервный polling выполняется каждые 5 секунд. История событий запрашивается от `health.last_event_id − 200` с `limit=200`, чтобы при длинном архиве показывать последние, а не первые записи буфера. Повторный coalesced-refresh планируется через 1000 мс, измерительный — через 1500 мс. Один refresh делает один проход и не зацикливается при непрерывных измерениях.

Визуализация ограничена 40 свипами и не более чем 1000 отображаемыми точками спектра с сохранением первой, последней и пиковой точки. Водопад использует max-агрегацию по пиксельным столбцам. Полные частоты остаются целыми Гц, `dBm` и `s_meter_count` отображаются в исходных единицах. Динамические строки вставляются через текстовые DOM-узлы; mock-данных и автоматического пересчёта единиц нет.

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

## Кандидаты и режимы handoff

Реестр кандидатов и очередь исследований работают в memory-only и SQLite режимах. Ручной handoff запускает явно выбранное исследование; полуавтоматический сначала рекомендует следующее по приоритету, но также требует подтверждения оператора. Автоматический handoff запускает подходящие ожидающие записи только после отдельного opt-in. Автоматическое обнаружение кандидатов доступно по самостоятельной opt-in политике для новых спектральных sweeps; оно не ставит исследования в очередь. Автоматического завершения исследований нет. Capabilities сообщают `candidate_investigations=true`, `manual_investigation_handoff=true`, `semi_automatic_handoff=true`, `automatic_handoff=true`, `automatic_handoff_opt_in_required=true`, `automatic_handoff_policy_persistent=false`, `automatic_candidate_detection=true`, `automatic_candidate_detection_opt_in_required=true`, `automatic_candidate_detection_policy_persistent=false` и `automatic_investigation_completion=false`.

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

### Полуавтоматический запуск следующего

Рекомендация запрашивается отдельно и не меняет исследование, кандидата или прибор:

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/investigations/next
```

Ответ содержит первое ожидающее исследование в том же порядке приоритета либо `data: null`, если очередь пуста. Для запуска клиент передаёт именно показанный ID и явное подтверждение:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/investigations/start-next \
  -H 'Content-Type: application/json' \
  -d '{"investigation_id":"INVESTIGATION_ID","confirm":true}'
```

Без `confirm=true` возвращается HTTP 400. Выбор рекомендации и перевод подтверждённого исследования в `tuning` выполняются под одним lock: если более приоритетная запись появилась после предпросмотра либо подтверждён другой ID, команда отклоняется с сообщением об изменении очереди и не вызывает tune. Повторный предпросмотр возвращает актуальную рекомендацию. Автоматический планировщик не вызывает этот маршрут: он использует отдельную runtime-политику и общий жизненный цикл исследования.

Один receiver может иметь только одно исследование в `tuning`, `listening` или `decoding`; это ограничение действует и для ручного, и для полуавтоматического запуска. Ожидающие записи того же receiver остаются в очереди. Другие приборные потоки и независимые receiver не блокируются.

### Автоматическое обнаружение кандидатов по явной политике

Детектор имеет собственную runtime-политику для каждого сеанса и независим от политики handoff. Начальное состояние всегда `enabled=false`; политика, курсор обработанных sweeps и cooldown не сохраняются в SQLite. Текущее состояние возвращается без изменений:

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-detection
```

Ответ содержит `policy`, `runtime`, `scheduler_alive`, `policy_persistent=false`, `restart_requires_opt_in=true`, `processes_existing_archive=false` и `automatically_enqueues_investigations=false`. В `runtime` доступны время и ID последнего обработанного sweep, `last_result`, `last_error`, числа обработанных sweeps, созданных кандидатов и подавленных дублей.

Включение с полным набором параметров:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-detection \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "interval_seconds": 1.0,
    "stream_id": "TINYSA_STREAM_ID",
    "threshold_dbm": -70.0,
    "dedup_bandwidth_hz": 100000,
    "cooldown_seconds": 60.0,
    "max_candidates_per_sweep": 10
  }'
```

POST частично обновляет политику. Неизвестные поля отклоняются. `enabled` должен быть JSON boolean. `interval_seconds` находится в диапазоне 0,1…3600, `threshold_dbm` — −300…100, `cooldown_seconds` — 0…86400. `dedup_bandwidth_hz` — целое 1…10000000000, `max_candidates_per_sweep` — целое 1…100. `stream_id` может быть `null` либо ID потока того же сеанса с ролью `spectrum_analyzer` и исходной единицей `dBm`.

При переходе из выключенного состояния во включенное детектор фиксирует текущий конец архива и обрабатывает только ещё не виденные новые sweeps. Массового анализа старого архива нет. В каждом подходящем sweep выделяются непрерывные участки с уровнями `>= threshold_dbm`; кандидат получает частоту точки максимума, исходный пик `dBm`, `score=peak_level-threshold_dbm` и интервал самого sweep. Полоса — расстояние между крайними точками участка; для одиночной точки используется локальный шаг частоты, минимум 1 Гц. Это оценка по выборке, а не точное измерение занимаемой полосы. Участки сортируются по убыванию пика, затем по частоте; применяется лимит на sweep.

Повторная обработка одного sweep не создаёт дубли. Активные `new`/`queued`/`investigating` и отклонённые `dismissed` кандидаты того же потока подавляют новый кандидат в пределах частотного допуска; для завершённых действует runtime cooldown. Допуск учитывает `dedup_bandwidth_hz` и оценённые полосы. Создание выполняется через общий `SessionService.create_candidate()` с состоянием `new`; detector не вызывает `enqueue`, `start`, `complete`, `fail` или `cancel`. Связь с исходным измерением фиксируется событием `automatic_detection.candidate_created`, содержащим `candidate_id` и `sweep_id`. Изменения политики публикуют `automatic_detection.policy_changed`.

Явное выключение сохраняет остальные параметры до перезапуска процесса и запрещает новые проходы:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-detection \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
```

Фоновый цикл создаётся лениво при первом включении. Проходы сеансов выполняются в отдельных dispatch-потоках, поэтому блокировка чтения архива одного сеанса не сериализует обнаружение другого. Один проход читает не более 1000 последних sweeps; при большем не обработанном backlog гарантированного разбора всей истории нет. Отсчёты `s_meter_count` не преобразуются в `dBm` и этим детектором не обрабатываются.

### Автоматический handoff по явной политике

Для каждого сеанса существует независимая runtime-политика. Начальное состояние всегда `enabled=false`; политика не записывается в SQLite и после перезапуска процесса требует нового явного включения. Текущее состояние возвращается без изменений:

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-handoff
```

Ответ содержит `policy`, `runtime`, `scheduler_alive`, `policy_persistent=false` и `restart_requires_opt_in=true`. В `runtime` доступны время последнего прохода, ID последнего выбранного исследования, `last_result`, `last_error`, число успешных и неуспешных запусков и текущая серия последовательных сбоев.

Включение с полным набором ограничений:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-handoff \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "interval_seconds": 1.0,
    "minimum_priority": 5,
    "receiver_stream_id": "RECEIVER_STREAM_ID",
    "max_consecutive_failures": 3,
    "retry_delay_seconds": 30.0
  }'
```

POST частично обновляет политику. Неизвестные поля отклоняются. `enabled` должен быть JSON boolean, а не `0`/`1`. `interval_seconds` находится в диапазоне 0,1…3600, `retry_delay_seconds` — 0,1…86400, `minimum_priority` — целое 0…1000000, `max_consecutive_failures` — целое 1…100. `receiver_stream_id` может быть `null` (любой receiver сеанса) либо ID потока того же сеанса с ролью `receiver`.

На каждом проходе планировщик рассматривает только `queued` с приоритетом не ниже заданного и подходящим receiver, в общем порядке очереди. Занятый receiver пропускается, но не блокирует независимый свободный receiver. Если ручной запуск занял receiver после предварительного выбора, проход помечается `skipped` и не считается приборным сбоем. Запуск выполняется через тот же `start_investigation`: автоматизируются только переходы `queued → tuning → listening`, без автоматического `complete`, `fail` или `cancel`.

После сбоя настройки следующий фоновый проход откладывается на `retry_delay_seconds`. При достижении `max_consecutive_failures` политика автоматически получает `enabled=false`, runtime — `last_result=circuit_breaker`. Явное выключение сохраняет остальные параметры до перезапуска процесса:

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-handoff \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
```

Фоновый цикл создаётся лениво при первом включении. Проходы сеансов выполняются в отдельных dispatch-потоках с раздельными блокировками, поэтому перестройка одного сеанса не сериализует dispatch другого. Изменения политики, успешные запуски и circuit breaker публикуют события `automatic_handoff.policy_changed`, `automatic_handoff.started` и `automatic_handoff.disabled`. Явное выключение запрещает новые проходы, но не отменяет уже активное исследование; для этого используется отдельный `cancel`.

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

События `candidate.created`, `candidate.dismissed`, `investigation.queued`, `investigation.tuning`, `investigation.listening`, `investigation.completed`, `investigation.failed`, `investigation.cancelled`, `investigation.interrupted`, `automatic_detection.policy_changed`, `automatic_detection.candidate_created`, `automatic_handoff.policy_changed`, `automatic_handoff.started` и `automatic_handoff.disabled` доступны через history/SSE. В SQLite изменение кандидата, исследования и соответствующего события выполняется одной транзакцией; подписчики уведомляются только после commit. Audit-события планировщика публикуются через ту же службу; его runtime-политика и счётчики в SQLite не сохраняются.

После перезапуска ожидающие `queued` сохраняются. Активные `tuning`/`listening`/`decoding` переводятся в `failed` с ошибкой прерывания и событием `investigation.interrupted`, поскольку поток команды не переживает процесс; кандидат возвращается в `queued`. Приборные workers при этом не перезапускаются автоматически. Обе автоматические политики не восстанавливаются и остаются выключенными до новых независимых opt-in.

ID кандидата и исследования в URL кодируются как один сегмент и декодируются ровно один раз, как ID сеанса. Неизвестные объекты возвращают HTTP 404 с кодами `candidate_not_found` и `investigation_not_found`.

`hardware_control` остаётся `false`: автоматическое обнаружение, ручной, полуавтоматический и автоматический handoff и модели архива проверены на синтетических sweeps и симуляторах, но не означают подключение или аппаратную проверку физических tinySA Ultra/AR-DV1.
