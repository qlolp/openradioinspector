# Общий HTTP API v1

Версия документа: текущий серверный контур с независимыми workers, кандидатами, ручным и подтверждаемым полуавтоматическим handoff.

Базовый URL по умолчанию: `http://127.0.0.1:8080/api/v1`. Все ответы — JSON, кроме SSE и статических страниц. UTF-8, частоты — целые Гц, время — ISO 8601 UTC с `Z`.

Старые `/api/*` не удалены. Они относятся к прежнему прототипу и не являются контрактом нового контура.

## Общий формат

Успешный ответ:

```json
{"data": {"id": "session-1"}}
```

Ошибка:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Описание ошибки",
    "details": null
  }
}
```

Коды: `400` — неверные данные или переход; `404` — объект не найден; `405` — метод не поддерживается; `500` — внутренняя ошибка без traceback в ответе. Все ошибки API, включая отсутствующий маршрут, возвращаются в JSON.

Получение списка измерений и событий:

- `limit` — целое от `1` до `10000`, по умолчанию `1000`;
- `after` — вернуть записи строго после указанного ID;
- `after` без `limit` для событий означает исторический replay без бесконечного SSE.

Порядок измерений: `started_at`, затем `id`, до применения `limit`. События имеют монотонный целочисленный `id`.

## Возможности и состояние

### `GET /health`

Возвращает состояние ядра, UTC-время, размер очереди SSE, число подписчиков и ID последнего события.

```json
{
  "data": {
    "status": "ok",
    "service": "openradioinspector",
    "api_version": "v1",
    "time": "2026-11-04T13:00:00Z",
    "event_backlog": 0,
    "event_subscribers": 1,
    "last_event_id": 12
  }
}
```

### `GET /capabilities`

```json
{
  "data": {
    "api_version": "v1",
    "schema_version": 2,
    "clients": ["raspberry-web", "windows", "macos"],
    "features": {
      "shared_session_core": true,
      "persistent_archive": true,
      "event_replay": true,
      "concurrent_device_workers": true,
      "candidate_management": true,
      "investigation_queue": true,
      "manual_investigation_handoff": true,
      "semi_automatic_handoff": true,
      "automatic_handoff": false,
      "automatic_candidate_detection": false,
      "hardware_control": false,
      "audio_streaming": false,
      "transmission": false,
      "original_radioinspector_compatibility": false
    },
    "adapters": ["tinysa-sim", "ardv1-sim"]
  }
}
```

`hardware_control=false` означает, что поставляются только программные симуляторы. `semi_automatic_handoff=true` означает только рекомендацию плюс отдельное подтверждение оператора. Автоматического выбора кандидатов и автономного запуска исследований нет.

## Сеансы

### `POST /sessions`

Обязательно только `name`. Необязательные поля: `id`, `description`, `owner`, `metadata`.

```json
{
  "name": "Двухприборный обзор",
  "description": "tinySA и AR-DV1",
  "metadata": {"site": "lab"}
}
```

Создаётся только `draft`; сервер сам назначает `created_at` и `updated_at`. Повторный ID возвращает `400` и не изменяет существующий сеанс. Ответ `201`.

### `GET /sessions`

Возвращает массив всех сеансов.

### `GET /sessions/{session_id}`

Возвращает сеанс. ID в URL декодируется один раз.

### `PATCH /sessions/{session_id}`

Разрешены `name`, `description`, `owner`, `metadata`. Пустой PATCH возвращает `400`. Состояние и служебное время через PATCH не меняются.

### `DELETE /sessions/{session_id}`

Удаляет сеанс, потоки, измерения, кандидаты, исследования и события каскадно. Ответ `204` без тела. Это необратимая операция API, а не очистка рабочего сеанса по таймеру.

### `POST /sessions/{session_id}/start`

Переводит `draft → running`, назначает `started_at`. Повторный запуск недопустим.

### `POST /sessions/{session_id}/stop`

Переводит `running → stopped`, назначает `ended_at`. Останавливает потоки сеанса и отменяет все открытые исследования с событием `investigation.cancelled`. После остановки запись измерений и запуск исследований запрещены.

## Приборные потоки

`DeviceStream` содержит:

| Поле | Описание |
| --- | --- |
| `id`, `name` | Уникальный ID и отображаемое имя |
| `role` | `scanner`, `receiver`, `signal_generator` |
| `transport` | `simulator`, `serial`, `usb`, `network` |
| `state` | `disconnected`, `connecting`, `idle`, `running`, `degraded`, `error`, `stopped` |
| `settings` | Параметры адаптера, только JSON-значения |
| `frequency_min_hz`, `frequency_max_hz` | Необязательные границы в Гц |
| `level_unit` | `dBm`, `s_meter_count` или `dBµV` |
| `capabilities` | Массив строк |
| `last_seen_at`, `last_error` | Runtime-диагностика |

### `POST /sessions/{session_id}/streams`

```json
{
  "id": "tinysa-wide",
  "name": "tinySA Ultra",
  "role": "scanner",
  "transport": "simulator",
  "settings": {"adapter": "tinysa-sim", "interval_seconds": 1.0},
  "frequency_min_hz": 100000000,
  "frequency_max_hz": 900000000,
  "level_unit": "dBm",
  "capabilities": ["sweep", "waterfall"]
}
```

Для AR-DV1:

```json
{
  "id": "ardv1-receiver",
  "name": "AOR AR-DV1",
  "role": "receiver",
  "transport": "simulator",
  "settings": {"adapter": "ardv1-sim"},
  "level_unit": "s_meter_count",
  "capabilities": ["tune", "receive", "s_meter"]
}
```

Создание возможно только для `draft`/`running`. Начальное runtime-состояние всегда `disconnected`. ID потока глобально уникален: коллизия с потоком другого сеанса отклоняется без изменения чужого сеанса.

### `GET /sessions/{session_id}/streams`

Возвращает потоки только указанного сеанса.

### `GET/PATCH/DELETE /sessions/{session_id}/streams/{stream_id}`

PATCH принимает `name`, `settings`, `frequency_min_hz`, `frequency_max_hz`, `capabilities`. PATCH не выбирает адаптер и не запускает поток. DELETE запрещён для активного runtime worker. Нельзя удалить поток, на который ссылаются сохранённые кандидаты или исследования: сначала удалите эти записи или сохраните архив без удаления.

### `POST .../streams/{stream_id}/start`

Создаёт и запускает независимый `DeviceWorker` по явному `settings.adapter`. Для `tinysa-sim` нужны границы частоты; необязательные настройки: `step_hz`, `points`, `rbw_hz`, `interval_seconds`.

Для `ardv1-sim`: `start_frequency_hz`, `mode`, `interval_seconds`. Значения по умолчанию: `145500000`, `NFM`, `1.0`.

Переход в `running` и первая запись измерения происходят асинхронно. HTTP 200 подтверждает принятие команды, а не аппаратное измерение.

### `POST .../streams/{stream_id}/stop`

Останавливает только выбранный worker. Другие потоки продолжают работу.

### `POST .../streams/{stream_id}/tune`

Только для `role=receiver`. Пример:

```json
{"frequency_hz": 145500000, "mode": "NFM", "label": "Контрольная частота"}
```

Команда попадает в очередь конкретного worker. Настройка выполняется вне глобальной блокировки ядра, поэтому sweep другого прибора продолжает записываться. Неизвестный режим отклоняется симулятором.

## Измерения

### `GET /sessions/{session_id}/sweeps?limit=1000&after=sweep-id`

Каждый `Sweep` содержит `id`, `session_id`, `stream_id`, `started_at`, `ended_at`, `frequencies_hz`, `levels_dbm`, `rbw_hz`, `reference_level_dbm`, `metadata`. Единица потока должна быть `dBm`.

### `GET /sessions/{session_id}/observations?limit=1000&after=observation-id`

`ReceiverObservation` содержит `frequency_hz`, положительный `s_meter_count`, `mode`, `demodulator`, `signal_present`, `audio_available`, `rssi_dbm`, `metadata` и общие поля измерения. Единица потока — `s_meter_count`.

`rssi_dbm`, если он появится, является отдельным необязательным измерением. Автоматического преобразования между ним и `s_meter_count` нет.

## Кандидаты и подтверждаемый handoff

Кандидат создаёт оператор или будущий отдельный анализатор. Текущее ядро не обнаруживает сигналы автоматически.

Состояния кандидата: `new`, `queued`, `investigating`, `resolved`, `dismissed`.

Состояния исследования: `queued`, `tuning`, `listening`, `decoding`, `completed`, `failed`, `cancelled`. Публичного перехода в `decoding` пока нет; это зарезервированное состояние контракта.

### `POST /sessions/{session_id}/candidates`

```json
{
  "id": "candidate-145500000",
  "source_stream_id": "tinysa-wide",
  "frequency_hz": 145500000,
  "peak_dbm": -72.5,
  "priority": 100,
  "note": "Ручной маркер"
}
```

`source_stream_id` обязан принадлежать сеансу. `peak_dbm` допускается только для потока `dBm`; `priority` — от `0` до `1000`. Необязательные `bandwidth_hz` и `note`. Кандидат создаётся в `new`, повторный ID отклоняется.

### `GET /sessions/{session_id}/candidates`

Сортировка: убывание `priority`, затем `created_at`, затем `id`.

### `PATCH /sessions/{session_id}/candidates/{candidate_id}`

Разрешены `priority`, `note`, `state`. Служебные переходы `investigating` и `queued` нельзя установить вручную через PATCH; их назначает lifecycle исследования. `resolved` и `dismissed` можно установить только для кандидата без открытого исследования.

### `DELETE /sessions/{session_id}/candidates/{candidate_id}`

Нельзя удалить кандидата с открытым исследованием. Завершённые исследования также ссылаются на кандидата в SQLite, поэтому для сохранения архивной целостности запись обычно следует пометить `resolved`/`dismissed`, а не удалять.

### `POST /sessions/{session_id}/candidates/{candidate_id}/investigations`

```json
{
  "id": "investigation-001",
  "target_stream_id": "ardv1-receiver",
  "target_frequency_hz": 145500000,
  "mode": "NFM",
  "priority": 100
}
```

Создаётся в `queued`, кандидат атомарно получает `queued`. Цель должна быть receiver того же сеанса. Если частота не указана, используется частота кандидата; если задана явно, она не обязана совпадать с исходным маркером. Для одного кандидата допускается только одно открытое исследование. `mode` необязателен: без него receiver сохраняет текущий режим.

### `GET /sessions/{session_id}/investigations`

Сортировка: убывание `priority`, затем `created_at`, затем `id`.

### `GET /sessions/{session_id}/investigations/{investigation_id}`

Возвращает одно исследование со всеми временными метками, результатом и ошибкой.

### `GET /sessions/{session_id}/investigations/next`

Возвращает рекомендованное `queued` исследование по общему порядку очереди. Предпросмотр не меняет кандидата, исследование, события или receiver. Если очередь пуста, возвращается HTTP 200 с `{"data": null}`.

### `POST /sessions/{session_id}/investigations/start-next`

Отдельное подтверждение рекомендации:

```json
{
  "investigation_id": "investigation-001",
  "confirm": true
}
```

`confirm` обязан быть JSON boolean `true`; `false`, `1` и строка не принимаются. Ядро атомарно проверяет, что подтверждаемый ID всё ещё является следующим по приоритету. При устаревшем ID, пустой очереди или другом активном исследовании на том же receiver возвращается `400`, а tune не выполняется. При успехе lifecycle совпадает с ручным `start`.

### Полуавтоматический запуск следующего

```bash
curl http://127.0.0.1:8080/api/v1/sessions/SESSION/investigations/next

curl -X POST http://127.0.0.1:8080/api/v1/sessions/SESSION/investigations/start-next \
  -H 'Content-Type: application/json' \
  -d '{"investigation_id":"investigation-001","confirm":true}'
```

Первый запрос только показывает рекомендацию. Второй повторно проверяет актуальность ID и занятость receiver, после чего использует общий lifecycle `tuning → listening`.

### `POST /sessions/{session_id}/investigations/{investigation_id}/start`

Допускается только для `queued` в запущенном сеансе. Выбранный receiver не должен одновременно выполнять другое исследование в `tuning`, `listening` или `decoding`; независимые receiver и scanner при этом не блокируются. Состояние `tuning`, `started_at` и кандидат `investigating` сохраняются до команды. Затем вызывается `tune` целевого worker вне общей блокировки.

При успехе состояние меняется на `listening`. При ошибке — на `failed`, кандидат возвращается в `queued`, ошибка доступна в `error` и событии `investigation.failed`. Ответом является объект со состоянием `failed`, а не потерянный HTTP-результат.

Если оператор отменил исследование во время настройки, поздний успешный `tune` не перезаписывает `cancelled`. Прежняя частота receiver автоматически не восстанавливается.

### `POST .../investigations/{investigation_id}/complete`

Допускается из `listening` или `decoding`. Обязателен `result_observation_id`:

```json
{"result_observation_id": "observation-001", "note": "Сигнал подтверждён"}
```

Наблюдение должно принадлежать тому же сеансу и receiver, а его частота — целевой частоте исследования. Если задан `mode`, он также должен совпасть. Иначе возвращается `400`. При успехе исследование получает `completed`, кандидат — `resolved`.

### `POST .../investigations/{investigation_id}/fail`

```json
{"reason": "Нет устойчивого сигнала"}
```

Допускается из `tuning`, `listening`, `decoding`. Исследование получает `failed`, кандидат возвращается в `queued`.

### `POST .../investigations/{investigation_id}/cancel`

Допускается для любого открытого состояния. Исследование получает `cancelled`, кандидат — `queued`.

После перезапуска процесса `tuning`, `listening`, `decoding` автоматически переводятся в `failed` с `investigation.interrupted`; ожидающие исследования остаются `queued`. Старые runtime workers не воскрешаются.

## События и SSE

### `GET /sessions/{session_id}/events?after=0&limit=1000`

Возвращает исторические события сеанса из SQLite. `after=0` означает начало архива.

### `GET /api/v1/events?after=0&limit=1000`

Без `after` открывает долгоживущий SSE и отправляет новые события.

С `after` сначала выполняется replay сохранённых событий, затем соединение остаётся live-подпиской. Для конечного HTTP-чтения истории используйте session events без `text/event-stream` либо общий endpoint с `after`, `limit` и обычным `Accept`.

Каждый SSE-кадр:

```text
id: 12
event: session.started
data: {"id":12,"session_id":"...","sequence":12,"type":"session.started","time":"2026-11-04T13:00:00Z","payload":{}}

```

Раз в 15 секунд отправляется SSE-комментарий `: keepalive`. Медленные клиенты могут пропускать live-события при переполнении ограниченной очереди; пропуски восстанавливаются по `Last-Event-ID`/`after` из SQLite. Replay и уведомления подписчиков происходят только после успешного commit.

Типы событий: `session.created`, `session.updated`, `session.started`, `session.stopped`, `stream.added`, `stream.updated`, `stream.state_changed`, `sweep.recorded`, `observation.recorded`, `candidate.created`, `candidate.updated`, `investigation.queued`, `investigation.tuning`, `investigation.listening`, `investigation.completed`, `investigation.failed`, `investigation.cancelled`, `investigation.interrupted`.

## Архив и восстановление

SQLite включает `foreign_keys=ON`, `journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`. Таблицы: `metadata`, `sessions`, `device_streams`, `sweeps`, `receiver_observations`, `candidates`, `investigations`, `session_events`.

Состояние сеанса, потока, кандидата, исследования, измерение и соответствующее событие сохраняются в одной транзакции. После открытия существующей базы схема проверяется и при необходимости мигрируется с версии 1 до 2; восстанавливаются сеансы, измерения, кандидаты, исследования и события. Старые активные investigations получают `failed`, а их кандидаты — `queued`. Runtime-состояния потоков сбрасываются в `disconnected`; worker threads и последовательные порты автоматически не открываются.
