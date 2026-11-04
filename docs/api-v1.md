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
- Текущий срез хранит сеансы только в памяти. Перезапуск сервера их удаляет; это явно отражено в `/api/v1/capabilities`.

## Маршруты

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/api/v1/health` | Состояние службы, число сеансов, последний ID события |
| `GET` | `/api/v1/capabilities` | Фактические возможности и режим хранения |
| `POST` | `/api/v1/sessions` | Создание сеанса с независимыми потоками |
| `GET` | `/api/v1/sessions` | Список сеансов |
| `GET` | `/api/v1/sessions/{id}` | Один сеанс |
| `POST` | `/api/v1/sessions/{id}/start` | Запуск сеанса из `draft` |
| `POST` | `/api/v1/sessions/{id}/stop` | Остановка сеанса из `running` |
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

Порт назначается только одному потоку сеанса. Идентификаторы сеанса и потоков создаются сервером, если не переданы явно. Перечисленные в примере capabilities являются метаданными профиля, а не подтверждением того, что сервер уже выполняет аппаратные команды.

## Жизненный цикл

```text
draft → running → stopped
```

Недопустимый переход возвращает HTTP 400. Обращение к неизвестному сеансу — HTTP 404 с кодом `session_not_found`.

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

## Модели измерений в контракте

`Sweep`, `ReceiverObservation`, `Candidate` и `Investigation` уже определены в `radioinspector.contracts` и сериализуются в JSON, но HTTP-endpoints их приёма и постоянного хранения относятся к следующему срезу. Capabilities возвращают для них `false`, чтобы клиент не принимал модель за работающий аппаратный контур.
