# Открытая реализация RadioInspector

Проект создаёт общий контур широкого спектрального обзора **tinySA Ultra** и частотного исследования **AOR AR-DV1** для Raspberry Pi, Windows и macOS.

**Это независимая разработка на основе публичной документации, а не исходный код или совместимый бинарный протокол RadioInspector.**

## Границы текущей версии

Реализованы ручной и подтверждаемый оператором полуавтоматический handoff с независимыми симуляторами приборов, SQLite-архивом и Raspberry Pi WebUI. Ручной handoff перестраивает AR-DV1 по выбранному кандидату и исследованию. Полуавтоматический режим показывает следующее по приоритету исследование и перестраивает receiver только после повторного HTTP-запроса с актуальным `investigation_id` и `confirm=true`. Для реальных приборов нужны отдельные адаптеры и проверка на оборудовании.

Работают: общие модели сеансов и измерений, валидация частот/единиц/времени, независимые `DeviceWorker`, локальный HTTP API, SSE и SQLite WAL. WebUI показывает реальные сеансы, независимые приборы, спектр/водопад, события, кандидатов, очередь исследований и подтверждаемый handoff; браузер ничего не измеряет и не управляет портами напрямую.

Для каждого измерения сохраняются исходные `dBm` tinySA и `s_meter_count` AR-DV1. Они не смешиваются и автоматически не конвертируются друг в друга.

Автоматическое обнаружение кандидатов и автономный handoff ещё не реализованы. `automatic_handoff=false`: сервер не запускает исследования по таймеру или изменению очереди. Аудио, аппаратная цифровая демодуляция и передача сигнала не выдаются за готовые функции. Подробнее: [документация](docs/README.md), [архитектура](docs/platform-architecture.md), [API](docs/api-v1.md) и [ограничения](docs/limits.md).

## Запуск

Требуется Python 3.11 или новее. Для общего ядра, HTTP API, симуляторов, SQLite и WebUI внешние зависимости не нужны.

```bash
python3 -m radioinspector
```

Откройте `http://127.0.0.1:8080/platform` для многоприборного WebUI. Корневой `/` сохраняет прежнюю панорамную страницу. Для Raspberry Pi или другой машины в доверенной локальной сети укажите `--host 0.0.0.0`.

```text
usage: python -m radioinspector [-h] [--host HOST] [--port PORT] [--db PATH] [--log-level LEVEL]
```

По умолчанию архив создаётся в `./data/radioinspector.sqlite3`. Каталог базы создаётся автоматически. Сервер слушает только `127.0.0.1`; при публикации в сети нужны внешние TLS, аутентификация и контроль доступа.

## Минимальный двухприборный сеанс

Ниже используются только явные адаптеры `tinysa-sim` и `ardv1-sim`. Пример создаёт запись в архиве и не открывает физические порты.

```python
from radioinspector import (
    DeviceRole, DeviceStream, DeviceTransport, MonitoringSession,
    ReceiverObservation, SessionService, Sweep, utc_now,
)

service = SessionService("./data/radioinspector.sqlite3")
session = MonitoringSession(name="Два независимых канала")
service.create(session)
service.add_stream(session.id, DeviceStream(
    id="tinysa-wide", name="tinySA Ultra", role=DeviceRole.SCANNER,
    transport=DeviceTransport.SIMULATOR,
    settings={"adapter": "tinysa-sim", "interval_seconds": 1.0},
    frequency_min_hz=100_000_000, frequency_max_hz=900_000_000,
    level_unit="dBm", capabilities=("sweep", "waterfall"),
))
service.add_stream(session.id, DeviceStream(
    id="ardv1-receiver", name="AOR AR-DV1", role=DeviceRole.RECEIVER,
    transport=DeviceTransport.SIMULATOR,
    settings={"adapter": "ardv1-sim"},
    level_unit="s_meter_count", capabilities=("tune", "receive", "s_meter"),
))
service.start(session.id)

started = utc_now()
service.record_sweep(Sweep(
    id="sweep-001", session_id=session.id, stream_id="tinysa-wide",
    started_at=started, ended_at=utc_now(), rbw_hz=10_000,
    frequencies_hz=(100_000_000, 100_010_000), levels_dbm=(-105.0, -72.0),
))
service.record_observation(ReceiverObservation(
    id="observation-001", session_id=session.id, stream_id="ardv1-receiver",
    started_at=started, ended_at=utc_now(), frequency_hz=145_500_000,
    mode="NFM", s_meter_count=512,
))
service.stop(session.id)
```

`Sweep` принимает частоты только в целых Гц и требует равное число частот и уровней. Для receiver нужен положительный `s_meter_count`; поле `rssi_dbm` остаётся необязательным. События и состояния сохраняются в той же SQLite-транзакции, что и соответствующее изменение.

### Ручной и подтверждаемый полуавтоматический handoff

WebUI создаёт кандидата и исследование только по команде оператора. Для ручного запуска используется:

```http
POST /api/v1/sessions/{session_id}/investigations/{investigation_id}/start
```

Для полуавтоматического выбора сначала запрашивается рекомендация без изменения состояния, затем отправляется отдельное подтверждение:

```http
GET /api/v1/sessions/{session_id}/investigations/next
POST /api/v1/sessions/{session_id}/investigations/start-next
{"investigation_id": "...", "confirm": true}
```

Если очередь изменилась или выбранный receiver уже выполняет другое исследование, подтверждение отклоняется до перестройки. На одном receiver одновременно допускается только одно активное исследование.

При успешной настройке receiver исследование получает `listening`. Результат исследования должен ссылаться на подходящее наблюдение; пустой `completed` не допускается. Остановка сеанса отменяет открытые исследования. После перезапуска сервера активные исследования получают `failed` и событие `investigation.interrupted`, а кандидаты возвращаются в `queued`.

## Независимые исполнители

В WebUI сеанс и потоки создаются через формы либо через API. Для программного запуска используйте `DeviceWorkerManager`:

```python
from radioinspector import DeviceWorkerManager

workers = DeviceWorkerManager(service)
for stream in service.list_streams(session.id):
    workers.add(stream)
    workers.start(stream.id)

# Перестройка одного receiver не блокирует sweep-доставку другого worker.
workers.tune(session.id, "ardv1-receiver", {
    "frequency_hz": 145_500_000, "mode": "NFM",
})
```

Поддерживаются только явные адаптеры `tinysa-sim` и `ardv1-sim`; физический порт не выбирается автоматически. Каждый worker имеет собственный поток ОС и очередь команд. Ошибка одного устройства меняет только его состояние и не останавливает другой поток. После перезапуска процесса архив сохраняется, а потоки получают `disconnected`: исполнители нужно запустить заново.

## HTTP API v1

Базовый путь: `/api/v1`. Ответы имеют вид `{"data": ...}` или `{"error": {"code", "message", "details"}}`. Старые `/api/*` сохранены.

| Метод и путь | Назначение |
| --- | --- |
| `GET /health`, `GET /capabilities` | Состояние и честные возможности ядра |
| `GET/POST /sessions`, `GET/PATCH/DELETE /sessions/{id}` | Сеансы мониторинга |
| `POST /sessions/{id}/start`, `/stop` | Жизненный цикл сеанса |
| `GET/POST /sessions/{id}/streams`, `GET/PATCH/DELETE .../streams/{stream_id}` | Независимые приборные потоки |
| `POST .../streams/{stream_id}/start`, `/stop`, `/tune` | Команды выбранному worker |
| `GET .../sweeps`, `.../observations` | Архив измерений с `limit` и `after` |
| `GET .../events`, `/api/v1/events` | История и live/replay SSE |
| `GET/POST .../candidates`, `GET/PATCH/DELETE .../candidates/{candidate_id}` | Ручные кандидаты и приоритет |
| `POST .../candidates/{candidate_id}/investigations` | Поставить исследование в очередь |
| `GET .../investigations`, `GET .../investigations/{investigation_id}` | Очередь и состояние исследований |
| `GET .../investigations/next` | Следующая рекомендация без запуска или изменения очереди |
| `POST .../investigations/start-next` | Подтверждённый запуск актуальной рекомендации |
| `POST .../investigations/{investigation_id}/start` | Ручная перестройка и начало прослушивания |
| `POST .../investigations/{investigation_id}/complete`, `/fail`, `/cancel` | Завершение, сбой или отмена |

Подтверждённый handoff требует JSON `{"investigation_id": "...", "confirm": true}`. Запрос `next` ничего не меняет. При устаревшем `investigation_id`, отсутствии явного подтверждения или занятом receiver возвращается `400` без команды перестройки.

Все маршруты, схемы JSON, события и примеры описаны в [API v1](docs/api-v1.md). Повторный GET возвращает данные из SQLite; live-доставка SSE не является гарантированной очередью.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

Тесты используют только временные каталоги и локальные HTTP-серверы на случайных портах. Они не открывают последовательные порты реальных приборов.

## macOS

Нативный SwiftUI-клиент и локальные симуляторы находятся в `macos/RadioInspector`. Он остаётся отдельным клиентом существующего приложения; общий серверный контур не заменяет его автоматически. См. [инструкцию macOS](macos/README.md).

## Исходники и исследование

- `radioinspector/domain.py` — модели и строгая валидация.
- `radioinspector/session_service.py` — транзакции, кандидаты, исследования и координация.
- `radioinspector/store.py` — SQLite WAL и восстановление.
- `radioinspector/workers/` — независимые исполнители и симуляторы.
- `radioinspector/web/` — HTTP/SSE и статический WebUI без JS-зависимостей.
- `docs/legacy/` — сохранённая документация прежнего прототипа.
- `docs/reference/` — только публичные ссылки и выводы исследования, без чужих PDF.

Правила для разработчиков и AI-агентов: [AGENTS.md](AGENTS.md). Лицензия: [MIT](LICENSE).
