# Автоматическое завершение исследований

`AutomaticInvestigationCompletion` — независимый runtime-планировщик для активных исследований. Он не связан с automatic detection и automatic handoff: каждая политика включается отдельно. По умолчанию завершение выключено и после перезапуска процесса требует нового явного opt-in.

## Политика

```bash
curl -sS -X POST \
  http://127.0.0.1:8770/api/v1/sessions/SESSION_ID/automatic-completion \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "interval_seconds": 1.0,
    "minimum_listening_seconds": 10.0,
    "minimum_observations": 2,
    "receiver_stream_id": null
  }'
```

Поля:

- `enabled` — обязательный JSON boolean для включения; начальное значение `false`;
- `interval_seconds` — период прохода, 0,1…3600;
- `minimum_listening_seconds` — минимальное время от `started_at`, 0…86400;
- `minimum_observations` — необходимое число подходящих наблюдений, 1…1000;
- `receiver_stream_id` — `null` для любого receiver либо ID receiver того же сеанса.

`GET` того же маршрута возвращает policy, runtime, `scheduler_alive`, `policy_persistent=false`, `restart_requires_opt_in=true` и `automatic_fail_or_cancel=false`. Выключение отправляет только `{"enabled":false}` и не меняет остальные параметры или другие политики.

## Условия завершения

Планировщик рассматривает только `listening` и `decoding` в running-сеансе. Наблюдения должны принадлежать целевому receiver, иметь точную `target_frequency_hz` и время начала не раньше `started_at` исследования. Предварительный read-only подсчёт не меняет состояние и в SQLite ограничен `LIMIT minimum_observations`, поэтому вся история не материализуется на каждом проходе. После прохождения фильтров вызывается тот же `SessionService.complete_investigation`, что и для ручного завершения, поэтому отсутствие подходящих наблюдений не создаёт ложный результат. Полный результат содержит все подходящие ID, независимо от ограниченного preflight.

Изменение политики увеличивает per-session generation. Решение о завершении и `configure` сериализуются: отключение или изменение политики во время in-flight pass не позволяет завершить исследование. Такой проход получает `policy_changed` и пропускается.

Ручной complete/fail/cancel после предварительного выбора считается гонкой оператора и получает `skipped`, а не сбой прибора. Неожиданная ошибка изолируется в `runtime.scheduler_error` и не останавливает dispatch других сеансов. Проходы сеансов имеют отдельные dispatch locks.

События `automatic_completion.policy_changed` и `automatic_completion.completed` доступны через history/SSE. Runtime содержит время последнего прохода, ID исследования, результат, ошибку и счётчики completed/skipped.

Capabilities сообщают `automatic_investigation_completion=true`, `automatic_investigation_completion_opt_in_required=true` и `automatic_investigation_completion_policy_persistent=false`.

## Windows GUI

`CompletionWindowsApp` добавляет форму «Настроить завершение…» поверх существующего `WindowsApp`: interval, minimum listening time/observations и receiver. «Сохранить выключенной» передаёт параметры без `enabled`, «Включить явно» разрешено только в running-сеансе, а «Выключить» отправляет только `enabled=false`. Snapshot показывает фактические policy/runtime. Detector и handoff не изменяются.

## Границы

Планировщик не выполняет automatic fail/cancel, не переводит исследование в `decoding`, не классифицирует сигнал, не измеряет точную занимаемую полосу, не останавливает workers и не восстанавливает прежнюю частоту receiver. `s_meter_count` не преобразуется в `dBm`. Активные исследования после перезапуска по-прежнему получают `failed` с `investigation.interrupted`, а политика остаётся выключенной. Аппаратная проверка не заявляется.
