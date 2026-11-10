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
| `GET` | `/api/v1/archive/capacity` | Глобальный размер SQLite, предел и предупреждение |
| `GET`, `POST` | `/api/v1/task-scheduler` | Глобальный runtime-only TaskScheduler, статус и opt-in |
| `GET`, `POST` | `/api/v1/task-scheduler/tasks` | Read-only список и создание ControlTask в текущей TaskBook |
| `POST` | `/api/v1/task-scheduler/tasks/{name}/cancel` | Отмена queued-задачи без остановки приборов |
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
| `POST` | `/api/v1/sessions/{id}/sweeps/batch` | Атомарный приём пакета свипов |
| `GET` | `/api/v1/sessions/{id}/sweeps?stream_id=&limit=100` | Последние свипы сеанса или потока |
| `GET` | `/api/v1/sessions/{id}/sweep-comparison?reference_id=&target_id=` | Read-only разность двух совместимых архивных dBm свипов в dB |
| `GET`, `POST` | `/api/v1/sessions/{id}/sweep-references` | Список именованных эталонов и идемпотентное создание ссылки на свип |
| `GET` | `/api/v1/sessions/{id}/sweep-threshold?sweep_id=&window_points=&margin_db=` | Read-only адаптивный порог и области превышения одного архивного dBm свипа |
| `POST` | `/api/v1/sessions/{id}/sweep-mask-analysis` | Read-only анализ архивного dBm свипа по переданному JSON артефакту маски |
| `POST` | `/api/v1/sessions/{id}/sweep-average-comparison` | Read-only среднее мощности 2–16 архивных свипов и разность с отдельным target |
| `POST` | `/api/v1/sessions/{id}/sweep-average-change-detection` | Read-only сравнение с усреднённым эталоном и области прироста по заданному порогу |
| `POST` | `/api/v1/sessions/{id}/sweep-three-thresholds` | Read-only три горизонтальных порога одного архивного dBm свипа |
| `POST` | `/api/v1/sessions/{id}/sweep-context` | Read-only ближайшие N1/N2 свипы вокруг точного архивного ID |
| `POST` | `/api/v1/sessions/{id}/candidate-frequency-rounding` | Read-only округление частоты кандидата по переносимой таблице |
| `POST` | `/api/v1/sessions/{id}/observations` | Приём наблюдения приёмника |
| `POST` | `/api/v1/sessions/{id}/observations/batch` | Атомарный приём пакета наблюдений |
| `GET` | `/api/v1/sessions/{id}/observations?stream_id=&limit=100` | Последние наблюдения сеанса или потока |
| `GET` | `/api/v1/sessions/{id}/archive/export.ndjson?stream_id=` | Потоковый NDJSON-экспорт неизменного snapshot архива |
| `GET` | `/api/v1/sessions/{id}/analytics/summary?stream_id=` | Read-only агрегаты всего архива сеанса или потока |
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

Порт назначается только одному потоку сеанса. Идентификаторы сеанса и потоков создаются сервером, если не переданы явно. ID потока уникален во всей службе и SQLite-архиве: повторное использование в другом сеансе возвращает HTTP 400, не изменяя существующий сеанс или его измерения. ID сеанса и `stream_id` в URL необходимо кодировать как один сегмент (например, `a/b` → `a%2Fb`); сервер декодирует каждый из них ровно один раз после разделения маршрута. Перечисленные в примере capabilities являются метаданными профиля, а не подтверждением того, что сервер уже выполняет аппаратные команды.

## Жизненный цикл

```text
draft → running → stopped
```

Создание всегда начинается с `draft`. Допускается явно передать `state: "draft"`; другое состояние и непустые `created_at`, `started_at`, `ended_at` при создании возвращают HTTP 400. Время создания назначает служба, время запуска и остановки меняется только через соответствующие операции. Восстановление из архива использует отдельный путь десериализации и сохраняет записанный жизненный цикл.

В SQLite создание, запуск, остановка и приём каждого измерения записываются вместе со своим событием в одной транзакции. История в памяти и SSE-подписчики получают событие только после успешного commit. При ошибке записи или прерывании процесса незавершённая операция не появляется после перезапуска.

Недопустимый переход возвращает HTTP 400. Обращение к неизвестному сеансу — HTTP 404 с кодом `session_not_found`. Остановка сеанса также отменяет все его открытые исследования и не оставляет активное состояние без работающего сеанса. На время остановки исполнителей и сохранения состояния сеанса новые старты его потоков отклоняются с прежним HTTP 400 `worker_error`; окно между этими операциями закрыто. Отдельный `/workers/stop` не переводит сеанс в `stopped` и допускает последующий явный `/workers/start`.

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

Панель «Сводка всего архива» не входит в обычный polling/SSE refresh. Отдельная кнопка отправляет один GET `/analytics/summary` для захваченного сеанса и показывает число sweeps/observations, исходные единицы, долю открытого шумодава и фактический пик последнего sweep. Ошибка этого read-only запроса изолируется и отображается отдельно, не прерывая live-измерения или остальной refresh. Подробности в [`webui-analytics.md`](webui-analytics.md).

## Windows GUI-клиент

`radioinspector-windows --server http://127.0.0.1:8770` и `python3 -m radioinspector.windows --server …` запускают тонкий Tkinter-клиент. Он использует `GET /health`, `/capabilities`, `/sessions`, детали выбранного сеанса, `/workers`, `/candidates`, `/investigations`, `/investigations/next`, `/sweeps?stream_id=…&limit=40` и GET обеих автоматических политик. Команды охватывают создание сеанса и кандидата, start/stop сеанса, start/stop всех workers, start/stop выбранного stream, `tune` выбранного receiver, постановку и отклонение кандидата, ручной и подтверждённый полуавтоматический handoff, complete/fail/cancel исследования и POST обеих автоматических политик. Ответ tune содержит `data.tuned` с подтверждёнными адаптером `frequency_hz` и `mode`, а также `data.worker` с состоянием исполнителя. Для `ardv1-serial` это принятая команда, не измерение частоты и не аппаратный readback; поля `frequency_source`, `mode_source`, `frequency_readback` и `hardware_validation` уточняют происхождение.

Базовый URL может включать завершающий `/api/v1`; клиент не добавляет его дважды. Учётные данные в URL, query и fragment отклоняются. Идентификаторы в путях кодируются как один URL-сегмент; `stream_id` в query выборки sweeps кодируется как обычный query-параметр. Ответ ограничен 2 МБ и принимается только при совпадении `contract=openradioinspector-platform` и `contract_version=1`. Частота tune должна быть положительным целым числом Гц, режим — непустой строкой; преобразования МГц и единиц уровня клиент не выполняет.

Форма нового сеанса создаёт пару tinySA/AR-DV1 в `draft`. Симуляторы добавляются в `settings` только при явном opt-in; иначе сохраняются профили без поддерживаемых workers. Отдельный serial-профиль назначает `tinysa-serial` и `ardv1-serial` с проверкой непустых разных портов и запретом `sim://`; одновременный `serial+simulated` отклоняется. Он не объявляет `hardware_decode`: serial-адаптер принимает только FM/AM и не подтверждает цифровое декодирование. Кандидат из пика использует реальные частоту, исходный уровень, интервал и ID последнего sweep; ручная форма сохраняет `level_unit` выбранного потока. Постановка в очередь не запускает receiver. Вкладка исследований отдельно показывает рекомендацию `next`; кнопка подтверждения отправляет её ID с `confirm=true`, а сервер повторно проверяет актуальность очереди. Complete не синтезирует наблюдения и успешен только по правилам ядра.

Формы detector и handoff проверяют все диапазоны и типы параметров до POST, включая boolean `enabled`, конечные числа, целые лимиты и принадлежность выбранного потока нужной роли. У выключенной политики сохранение параметров и кнопка `Включить явно` являются разными действиями; включение разрешено только в running-сеансе. У включённой политики можно сохранить параметры или отправить только `{"enabled":false}`. Операции одной политики не содержат полей другой: detector не вызывает queue/handoff, scheduler не вызывает detector или complete/fail/cancel. Выключение handoff не отменяет активное исследование.

HTTP работает в одном фоновом executor, обновление выполняется polling каждые 2 секунды, SSE не используется. Sweeps запрашиваются только для выбранного `spectrum_analyzer` с `level_unit=dBm`; snapshot связывает их с `spectrum_stream_id`, чтобы ответ прежнего потока не был показан для нового выбора. Для receiver выполняется snapshot без выборки sweeps, а `s_meter_count` в `dBm` не преобразуется.

Визуализация использует до 40 последних sweeps и не более 1000 точек спектра с сохранением первой, последней и фактической пиковой точки. Водопад применяет max-агрегацию по пиксельным столбцам и располагает старые sweeps сверху, новые снизу. Команды ручного или полуавтоматического handoff не включают scheduler/detector. Обе автоматические политики остаются runtime-only и после перезапуска сервера требуют независимых новых opt-in.

`AnalyticsWindowsApp` не включает `/analytics/summary` в routine snapshot. Отдельная кнопка вызывает `analytics_summary(session_id)` для захваченного сеанса; повтор блокируется `_action_running`, смена сеанса сбрасывает результат. Сводка и ошибка отображаются в видимом Tk `Label` и не останавливают live-измерения или scheduler. Подробности в [`windows-analytics.md`](windows-analytics.md).

## macOS SwiftUI-клиент

Существующее окно `macos/DvoynoyObzor` подключается к ядру через `SpektrKomandy/PlatformAPI.swift`. Оно использует `GET /sessions`, детали сеанса, `/workers`, `/sweeps?stream_id=…&limit=40`, `/observations?stream_id=…&limit=40`, кандидатов, очередь исследований, рекомендацию `next` и GET обеих автоматических политик. Команды охватывают создание сеанса, start/stop сеанса, всех workers и независимых потоков, `tune` только receiver, создание/отклонение кандидата и постановку в очередь, ручной и подтверждённый полуавтоматический handoff, complete/fail/cancel и изменение независимых политик. Базовый URL проверяется так же, как в Windows-клиенте; ID в путях и query кодируются, конверт и версия ответа обязательны. Ответ ограничен 2 МБ после загрузки.

Создание обычного сеанса не назначает adapter или port. Отдельная кнопка явно создает `tinysa-sim` и `ardv1-sim`; это не аппаратная проверка. Другая отдельная кнопка «Новый serial» требует два разных порта и назначает `tinysa-serial` и `ardv1-serial` с числовыми настройками спектра и receiver. Она не выбирает адаптер автоматически, не объявляет `hardware_decode` и не подтверждает аппаратный стенд. Спектр и водопад используют только архивные `dBm` sweeps, а панель AR-DV1 показывает исходные `s_meter_count`, режим, шумодав и временной интервал. Кандидат создаётся из фактического пика sweep с интервалом и ID измерения, без выдуманной полосы. Start-next отправляет актуальный ID с `confirm=true`. Выключение каждой политики отправляет только `enabled=false`, сброс фильтра receiver/analyzer кодируется явным `null`. Ошибка одного workflow-списка изолируется и не блокирует workers/sweeps/observations. Polling отбрасывает ответ прежнего `session_id`. Закрытие окна отменяет только клиентский polling и не останавливает ядро. `RemoteAnalyticsPane` запрашивает `/analytics/summary` только по кнопке; routine `PlatformClient.snapshot()` этот endpoint не вызывает. Явный запрос защищён `archiveAnalyticsPending` и generation, а polling не перезаписывает полученную сводку. Исходные `dBm` и `s_meter_count` разделены, ошибка изолирована. Подробности в [`macos-analytics.md`](macos-analytics.md). Пакет `SpektrKomandy`, native session/transport XCTest и полная Xcode-сборка проверены на macOS arm64 без подписи; аппаратный стенд не проводился.

## Независимые исполнители и симуляторы

Исполнители запускаются только для потоков с явным `settings.adapter`:

- `tinysa-sim` — периодические свипы в `dBm`, целые частоты в Гц, генератор всегда `off`;
- `ardv1-sim` — наблюдения в исходных `s_meter_count`, настройка частоты и режима через индивидуальную очередь команд.

Адаптер не выбирается автоматически по названию прибора или строки порта. Явные `tinysa-serial` и `ardv1-serial` открывают отдельный последовательный порт только после выбора через `settings.adapter`. Поэтому `hardware_control=true`, а `hardware_validation_not_run=true` сообщает, что scripted-проверки выполнены, но аппаратный стенд не проводился. Дополнительные capabilities `simulated_device_workers`, `independent_worker_commands` и `continuous_simulated_monitoring` относятся к программному стенду.

Пример настроек двух потоков при создании сеанса:

На Windows общий transport выбирается ядром через pySerial (`pip install ".[serial]"`);
поле `port` содержит `COM3`, `COM12` или `\\.\COM12`. На Linux/macOS сохраняется
POSIX transport и путь `/dev/…`. ОС клиента значения не имеет. WebUI, Windows
и macOS создают явный serial-профиль через существующий POST `/sessions`;
создание не запускает исполнителей. Подробности и пределы проверки:
[windows-core-serial.md](windows-core-serial.md).

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

### Атомарная пакетная запись

`POST /api/v1/sessions/{id}/sweeps/batch` и `POST /api/v1/sessions/{id}/observations/batch` принимают от 1 до 100 элементов одного типа в поле `items`. Каждый элемент имеет тот же контракт, что одиночный POST. Поле `session_id` внутри элемента можно опустить; если оно задано, то обязано совпадать с маршрутом. Один поток и исходная единица проверяются для каждого элемента независимо, автоматического пересчёта уровней нет.

```bash
curl -sS http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/sweeps/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "items": [
      {
        "id": "sweep-1",
        "stream_id": "TINYSA_STREAM_ID",
        "interval": {"started_at":"2026-11-05T10:00:00Z","ended_at":"2026-11-05T10:00:01Z"},
        "frequencies_hz": [100000000,100100000],
        "levels": [-80.5,-42.0],
        "level_unit": "dBm"
      },
      {
        "id": "sweep-2",
        "stream_id": "TINYSA_STREAM_ID",
        "interval": {"started_at":"2026-11-05T10:00:01Z","ended_at":"2026-11-05T10:00:02Z"},
        "frequencies_hz": [100000000,100100000],
        "levels": [-81.0,-43.0],
        "level_unit": "dBm"
      }
    ]
  }'
```

Успешный ответ имеет HTTP 201 и `data={"items":[...],"count":2}`. Порядок элементов сохраняется. Для каждого измерения создаётся своё обычное событие `sweep.received` или `observation.received` с монотонной последовательностью. В SQLite весь пакет и все его события записываются одной транзакцией; replay и SSE обновляются только после commit. Ошибка любого элемента, дубликат ID внутри пакета или конфликт с архивом отклоняет весь пакет: частичные измерения и события не сохраняются. Memory-only режим применяет ту же проверку «всё или ничего» до изменения списков. Capabilities сообщают `measurement_batch_ingest=true`, `measurement_batch_atomic=true` и `measurement_batch_limit=100`.

### Внешний импорт архива

Команда `archive-import` позволяет внешнему сборщику передать подготовленные sweeps и observations в существующий сеанс. Она использует только capabilities, чтение сеанса и перечисленные выше batch-маршруты: прямой доступ к SQLite и приборным портам не нужен. Единственным писателем архива остаётся серверный `SessionService`.

Вход — строгий JSON версии 1. Поля `format` и `format_version` обязательны, неизвестные поля отклоняются. Каждое измерение должно иметь стабильный непустой `id`; автоматическая генерация ID при импорте не используется. Поле `session_id` внутри записи можно опустить, но если оно задано, то должно совпадать с `--session`.

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

```bash
# Полная проверка документа, capabilities, сеанса, потоков и единиц без POST.
python3 -m radioinspector.archive_ingest \
  --server http://127.0.0.1:8770 \
  --session SESSION_ID --input archive.json --dry-run

# Импорт пакетами по 50 записей; вместо файла можно указать - для stdin.
python3 -m radioinspector.archive_ingest \
  --server http://127.0.0.1:8770 \
  --session SESSION_ID --input archive.json --batch-size 50
```

До первого POST проверяются весь документ, `measurement_batch_ingest=true`, `measurement_batch_atomic=true`, серверный `measurement_batch_limit`, существование сеанса, принадлежность каждого потока и совпадение исходной `level_unit`. Поэтому ошибка даже в последней записи или чужой поток не позволяет записать предыдущие пакеты. `--batch-size` может только уменьшить серверный лимит. Порядок записей сохраняется: сначала sweeps, затем observations; ответ каждого пакета сверяется по числу и ID.

Результат содержит `sweeps`, `observations`, `batches`, `batch_size`, `dry_run`, `atomic_per_batch=true` и `whole_file_atomic=false`. **Атомарен каждый HTTP-пакет, а не весь много-пакетный файл.** При сетевом сбое или отказе сервера после уже подтверждённых пакетов ранее принятые записи остаются в архиве. Повторный импорт не пропускает дубликаты молча: сервер отклоняет конфликтующий пакет. Частоты остаются целыми Гц, время приводится к UTC, `dBm` и `s_meter_count` не пересчитываются друг в друга.

### Read-only аналитика архива

`GET /api/v1/sessions/{id}/analytics/summary` возвращает агрегаты всего архива сеанса, а не только последние 40 или 100 измерений HTTP-выборки. Необязательный `stream_id` ограничивает расчёт одним потоком того же сеанса; неизвестный сеанс возвращает 404, чужой поток — 400.

```bash
curl -sS 'http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/analytics/summary?stream_id=TINYSA_STREAM_ID'
```

Отдельные объекты `sweeps` и `observations` содержат `count`, `first_started_at`, `last_started_at`, `level_units` и полное последнее измерение `latest`. Для последнего sweep дополнительно возвращается `latest_peak` с фактическими `sweep_id`, `frequency_hz`, `level` и `level_unit`. Для observations публикуются `squelch_known`, `squelch_open` и `squelch_open_ratio`; если состояние шумодава неизвестно у всех записей, ratio равно `null`.

Поле `archive_wide=true` подчёркивает область расчёта. Memory-only и SQLite возвращают одинаковую структуру; после перезапуска SQLite-архив сохраняет агрегаты. Расчёт выполняется под блокировкой службы и read-only: он не создаёт измерений, событий или преобразования `s_meter_count` в `dBm`. Capabilities сообщают `session_analytics=true` и `session_analytics_archive_wide=true`. Это сводка архива, а не автоматическая классификация сигнала или точное измерение занимаемой полосы.

### Потоковый NDJSON-экспорт snapshot

`GET /api/v1/sessions/{id}/archive/export.ndjson` возвращает `application/x-ndjson` с header `openradioinspector-archive-ndjson`, версия 1, и записями `sweep`/`observation`, совместимыми с `radioinspector-archive-stream`. Необязательный `stream_id` ограничивает экспорт одним потоком. Headers `X-Archive-Sweeps` и `X-Archive-Observations` содержат точные счётчики того же snapshot, который сериализуется в ответ.

SQLite сначала копируется во временный файл на диске, после чего writer освобождается, а строки читаются пакетами без полной материализации архива в Python. Записи после создания snapshot не попадают в ответ и не меняют счётчики. Ответ закрывается соединением и не имеет `Content-Length`; обрыв или несовпадение счётчиков считается ошибкой экспорта. Capabilities сообщают `session_archive_export_ndjson=true` и `session_archive_export_streaming=true`. Частоты остаются целыми Гц, `dBm` и `s_meter_count` не преобразуются. Подробности и CLI описаны в [`archive-export-stream.md`](archive-export-stream.md).

### Глобальный TaskScheduler

`GET /api/v1/task-scheduler` возвращает фактические `enabled`, `scheduler_alive`, `policy_persistent=false`, `restart_requires_opt_in=true`, runtime и счётчики задач `total`, `running`, `completed`. Scheduler выключен по умолчанию и после перезапуска требует нового явного включения.

`POST /api/v1/task-scheduler` принимает ровно одно поле `enabled` с JSON boolean. Дополнительные поля и не-boolean значения отклоняются. Включение и выключение — отдельные команды оператора, а не побочный эффект выбора сеанса или сохранения параметров.

`GET /api/v1/task-scheduler/tasks` возвращает `items` и `count` в порядке TaskBook без перечитывания или изменения файла. Это read-only снимок под scheduler lock: он не запускает worker и не создаёт `SessionEvent`. Значение `start_at` публикуется в UTC с суффиксом `Z`; пустые optional paths не считаются файлами. Локальные пути `task_file` и `panorama_file` в ответ не включаются.

`POST /api/v1/task-scheduler/tasks` создаёт queued-задачу и возвращает HTTP 201 с её sanitized представлением. Обязательны `name`, ровно один `task_file` или `panorama_file`, `start_at` и положительное `duration_s`. Дополнительные поля отклоняются; дубликат имени и ошибка валидации не изменяют файл и не создают `SessionEvent`.

`POST /api/v1/task-scheduler/tasks/{name}/cancel` переводит только queued-задачу в aborted с `OUTCOME_STOPPED_BY_OPERATOR`. Повторная отмена и отмена running возвращают HTTP 400, неизвестное имя — HTTP 404 `task_not_found`.

```bash
curl -sS -X POST http://127.0.0.1:8770/api/v1/task-scheduler \
  -H 'Content-Type: application/json' \
  -d '{"enabled":true}'
```

Capabilities сообщают `control_task_scheduler=true`, `control_task_scheduler_opt_in_required=true`, `control_task_scheduler_policy_persistent=false`, `control_task_scheduler_task_list=true`, `control_task_scheduler_task_create=true` и `control_task_scheduler_task_cancel=true`. Клиенты Raspberry Pi, Windows и macOS показывают фактические runtime, счётчики и публичный список `ControlTask` независимо от выбранного сеанса, а также создают queued-задачи и отменяют только задачи в статусе queued через соответствующие POST. Запросы status/list имеют раздельную изоляцию ошибок. Клиенты не редактируют существующие `ControlTask`, не читают путь к TaskBook и не подтверждают аппаратный результат. Изменения scheduler не создают `SessionEvent`; книга имеет собственный атомарный JSON. Подробности в [`task-scheduler.md`](task-scheduler.md).

## Кандидаты и режимы handoff

Реестр кандидатов и очередь исследований работают в memory-only и SQLite режимах. Ручной handoff запускает явно выбранное исследование; полуавтоматический сначала рекомендует следующее по приоритету, но также требует подтверждения оператора. Автоматический handoff запускает подходящие ожидающие записи только после отдельного opt-in. Автоматическое обнаружение кандидатов доступно по самостоятельной opt-in политике для новых спектральных sweeps; оно не ставит исследования в очередь. Автоматическое завершение доступно по независимой runtime-политике для `listening`/`decoding` после заданных времени и числа подходящих observations и использует общий `SessionService.complete_investigation`; automatic fail/cancel отсутствует. Capabilities сообщают `candidate_investigations=true`, `manual_investigation_handoff=true`, `semi_automatic_handoff=true`, `automatic_handoff=true`, `automatic_handoff_opt_in_required=true`, `automatic_handoff_policy_persistent=false`, `automatic_candidate_detection=true`, `automatic_candidate_detection_opt_in_required=true`, `automatic_candidate_detection_policy_persistent=false`, `automatic_investigation_completion=true`, `automatic_investigation_completion_opt_in_required=true` и `automatic_investigation_completion_policy_persistent=false`.

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

ID кандидата и исследования в URL кодируются как один сегмент и декодируются ровно один раз, как ID сеанса и потока. Неизвестные объекты возвращают HTTP 404 с кодами `candidate_not_found` и `investigation_not_found`.

`hardware_control=true` и `hardware_validation_not_run=true`: автоматическое обнаружение, ручной, полуавтоматический и автоматический handoff и модели архива проверены на синтетических sweeps и симуляторах, а явные serial-адаптеры — на scripted transport. Это не означает подключение или аппаратную проверку физических tinySA Ultra/AR-DV1.

## Ограничение роста SQLite-архива

`--archive-max-bytes` вместе с `--session-database` включает проверку логического
размера SQLite до и после записи каждого пакета измерений. Превышение возвращает
HTTP 507 `archive_capacity_exceeded`; весь пакет и его события откатываются,
старые данные не удаляются. `/api/v1/archive/capacity` доступен без выбранного
сеанса, показывает предупреждение от 90% и полный предел от 100%. Три клиента
показывают этот статус независимо от live-измерений.

Это ограничитель приёма измерений, а не жёсткая квота файловой системы: WAL и
временные копии не учитываются, служебные записи остаются разрешены. Поля,
команда запуска и границы описаны в [archive-capacity.md](archive-capacity.md).

## Эталон и разность архивных свипов

`features.sweep_comparison` включает сравнение точной пары записей без изменения
архива. Расчёт `target − reference` допускается для одного потока, одинаковой
частотной сетки и записанных настроек (кроме `sequence`), только из `dBm` в `dB`.
Срез ограничен 16 384 точками, не интерполирует данные и не запускает detector.
Контракт, коды ошибок и сценарии трёх клиентов: [sweep-comparison.md](sweep-comparison.md).

## Именованные эталоны

`features.named_sweep_references` включает каталог ссылок на архивные sweeps.
Он сохраняется в файловом SQLite schema v3, в memory-режиме и SQLite `:memory:`
временный (`persistent: false`). POST создаёт
запись и событие `sweep_reference.created`; точный повтор возвращает прежнюю
запись без нового события. Нет удаления/перезаписи. Контракт и миграция:
[named-sweep-references.md](named-sweep-references.md).

## Адаптивный порог архивного свипа

`features.adaptive_sweep_threshold` включает явный read-only расчёт локальной
медианы плюс запас для одного записанного свипа. Исходный уровень и линия
порога имеют единицу `dBm`, превышение — `dB`. Новая функция не включает
`automatic-detection`, не создаёт кандидатов и не меняет схему архива.
Контракт, пример и ограничения: [adaptive-sweep-threshold.md](adaptive-sweep-threshold.md).

## Табличная спектральная маска

`features.spectral_mask_analysis` включает явный анализ одного архивного
свипа по переносимому JSON артефакту. POST передаёт `{sweep_id, mask_json}`,
где `mask_json` — исходный текст для центральной строгой проверки.
Операция не пишет в архив, не создаёт событий/кандидатов и не меняет политики.
Формат файла, интерполяция в dBm и пределы:
[spectral-mask-analysis.md](spectral-mask-analysis.md).

## Усреднённый эталон

`features.averaged_sweep_reference` сообщает поддержку явного read-only POST
с `{reference_ids, target_id}`. Ядро усредняет линейную мощность 2–16 различных
совместимых архивных свипов и возвращает производную панораму в dBm,
разность в dB и происхождение всех источников. Каталог и архив не меняются.
Контракт, численная устойчивость и пределы: [averaged-sweep-reference.md](averaged-sweep-reference.md).

## Три горизонтальных порога

`features.three_sweep_thresholds` включает read-only POST с `{sweep_id, levels_dbm}`.
Три уровня сохраняют порядок и идентичность даже при совпадении; каждое
превышение и область вычисляются ядром отдельно. Общего OR-детектора нет.
Контракт и пределы: [three-sweep-thresholds.md](three-sweep-thresholds.md).

## Архивное окно N1/N2

`features.sweep_context` включает read-only POST с `{sweep_id, before, after}`.
Ядро выбирает ближайшие записи того же потока по UTC started_at и точному ID;
ответ содержит полное окно, доступные и недостающие количества. Неподходящий
сосед отклоняет всё окно и не заменяется более далёким. Формат, лимиты и
синтетический пример: [sweep-context.md](sweep-context.md).

## Занятость архивного окна

`features.sweep_occupancy` включает read-only
`POST /api/v1/sessions/{id}/sweep-occupancy` с
`{sweep_id, before, after, threshold_dbm}`. Ответ содержит снимок N1/N2 и
долю измерений строго выше порога по каждой точке, счётчики и разрывы
интервалов сканирования. Это не RF airtime. Полный контракт:
[sweep-occupancy.md](sweep-occupancy.md).

## Таблица округления частот

`features.candidate_frequency_rounding` включает read-only POST с
`{candidate_id, profile_json}`. Ядро возвращает отдельную производную частоту,
исходное значение не меняется. Целочисленный метод, допуски, переносимый профиль,
статусы и пределы: [frequency-rounding.md](frequency-rounding.md).

## Обнаружение изменений по усреднённому эталону

`features.averaged_sweep_change_detection` сообщает поддержку явного read-only
POST с `{reference_ids, target_id, minimum_increase_db, minimum_bins}`.
Ответ содержит обычное сравнение и непрерывные области, где `target −
mean_reference` не меньше `minimum_increase_db` на `minimum_bins` соседних
bins. Query string, включая пустой `?`, запрещён, тело ограничено 8192 байтами.
Запрос не создаёт кандидатов, событий или архивных записей и не меняет политику
`automatic_candidate_detection`. Пределы и форма ответа:
[sweep-change-detection.md](sweep-change-detection.md).
Клиенты могут сохранить проверенный результат в локальный версионированный
JSON-артефакт; это не отдельный серверный маршрут и не запись в архив.
Формат файла: [sweep-average-export.md](sweep-average-export.md).

## Уточнения подготовки v0.4.0

Все POST требуют `Content-Type: application/json`. При наличии `Origin` он
должен совпадать с origin ядра; чужой Origin получает403, простой тип тела415
до чтения и применения запроса. Native-клиенты могут не передавать Origin.
Это защита от браузерных CSRF-запросов, она не заменяет аутентификацию.
Удалённый API до проверки доступа ограничен доверенной сетью.

`GET /api/v1/sessions/{id}/investigations/by-id/{investigation_id}` однозначно
читает любую существующую запись, включая ID `next`. Прежний
`.../investigations/next` сохраняет значение рекомендации очереди.
Текущие клиенты получают списки и рекомендацию раздельно; новый Python
`get_investigation` использует однозначный путь.

В ответах Investigation добавлено необязательное поле UTC `listening_started_at`: момент успешного перехода в `LISTENING`, сохраняемый при декодировании и завершении. `started_at` по-прежнему означает начало перестройки. У старых записей и при ошибке или отмене перестройки отметка прослушивания равна null. Старые payload без поля принимаются. Положительный минимум длительности автоматического завершения требует этой отметки; нулевой минимум отключает только проверку времени.

### Ограниченный приёмный профиль tinySA

Новые serial-черновики трёх клиентов явно выбирают
`settings.receive_profile = "tinysa-ultra-zs405-receive-v1"`.
Если поле отсутствует, общий адаптер использует тот же профиль. Пределы
приложения, firmware allowlist, новые метаданные worker/sweep и совместимость
старых архивов описаны в [tinySA receive profile](tinysa-receive-profile.md).
Запрошенный `rbw_hz` не является readback фактически применённого фильтра.

## Ограничения тела HTTP-запросов

Все POST проверяют framing и размер до чтения: один Content-Length, без
Transfer-Encoding. Для прежних действий без тела допускается пустое тело;
непустое тело должно быть JSON-объектом UTF-8 без повторяющихся ключей,
NaN/Infinity и переполненных чисел. Обычный предел — 65 536 байт; для
одиночных sweeps/observations, их batch и phonogram/frames — 16 МиБ.
Существующие специальные лимиты анализа и импорта signal-list сохранены.
Превышение размера возвращает 413/request_too_large, некорректное framing,
неполное тело или JSON — 400/invalid_request, истечение общего срока чтения
тела (10 с) — 408/request_timeout. После ошибки соединение закрывается;
состояние и архив не меняются. Срок чтения тела не ограничивает выполнение
приборной команды или потоковую выдачу SSE/аудио.

Capabilities дополнены features.measurement_batch_max_request_bytes=16777216.
Пакет ограничен одновременно числом записей и размером сериализованного
UTF-8 JSON, включая оболочку items и разделители. Контракт версии 1 и
формат архива не изменены. Инструментальные POST, включая отмену операции
и управление арендой, требуют явного JSON-объекта (например, {}); пустое
тело отклоняется до dispatch, как и до введения общего reader.

## Режим наблюдений при завершении исследования

Ручное и автоматическое завершение используют один отбор: целевой поток,
частота, начало наблюдения не раньше начала исследования и, если задан
Investigation.mode, совпадение observation.mode с запрошенным режимом после
strip().upper(), как в адаптерах AR-DV1. Нормализация применяется при отборе
и к ранее сохранённым запросам; исходное значение режима не переписывается.
decoded_mode не заменяет режим приёмника. При mode=null режим не ограничивается.
Неподходящие наблюдения не завершают исследование и не разрешают кандидата.
Условия одинаковы в памяти и SQLite; исторические результаты не
пересчитываются, изменения схемы БД нет. Проверено синтетически в обоих
режимах хранения, включая автоматическое завершение.
