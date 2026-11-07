# Фоновый планировщик заданий

`TaskScheduler` выполняет существующие `ControlTask` из JSON `TaskBook`. Он не создаёт измерений, не открывает приборные порты и не меняет `SessionService` или device workers. Измерительные действия подключаются отдельными `on_start`/`on_complete` callbacks; по умолчанию callbacks не выполняют работы и не подтверждают аппаратный результат.

## Opt-in и жизненный цикл

Планировщик выключен по умолчанию и существует только в памяти процесса. `policy_persistent=false`, `restart_requires_opt_in=true`: после перезапуска нужен новый явный `configure(True)` или CLI `--enable`.

На каждом проходе:

- queued задача с `start_at <= now` переводится в running после проверки наличия `task_file`/`panorama_file`;
- running задача завершается только при `now >= ends_at`, после чего `completed_cycles` увеличивается;
- отсутствующий входной файл даёт `OUTCOME_FILE_NOT_FOUND`;
- ошибка одной задачи записывается в runtime и не останавливает остальные;
- `close()` останавливает thread и помечает активные задачи `OUTCOME_STOPPED_BY_OPERATOR`;
- running-статус из прежнего процесса восстанавливается как `OUTCOME_APPLICATION_CRASH`, а не как успешное выполнение.

TaskBook сохраняется атомарным replace. Счётчики runtime содержат started/completed/aborted, последнюю задачу, результат и ошибку. Старые записи не дублируются благодаря проверке имени при `enqueue`.

## HTTP API

`create_server` создаёт `control_scheduler` и связывает его с `BoundHandler`. `GET /api/v1/task-scheduler` возвращает `enabled`, `scheduler_alive`, `policy_persistent=false`, `restart_requires_opt_in=true`, runtime и счётчики задач. `POST` принимает ровно одно поле `enabled` с JSON boolean и вызывает `configure`; дополнительные поля отклоняются. Capabilities сообщают `control_task_scheduler=true`, `control_task_scheduler_opt_in_required=true`, `control_task_scheduler_policy_persistent=false`.

После `close()` серверного scheduler book lock освобождается. Другой scheduler может открыть ту же книгу повторно. HTTP-изменения не создают `SessionEvent`: TaskBook имеет собственный атомарный JSON, а не `SessionService`.

## Raspberry Pi WebUI

Панель «Планировщик заданий» на `/platform` показывает фактические `enabled`, `scheduler_alive`, runtime и счётчики total/running/completed независимо от выбранного сеанса. Отдельные кнопки отправляют только `{"enabled":true}` или `{"enabled":false}`; сохранение и включение не объединены в одну команду. Ошибка запроса отображается в панели и не блокирует live-измерения или остальные политики.

WebUI не создаёт и не редактирует `ControlTask`, не читает путь к TaskBook и не подтверждает аппаратный результат. Добавление заданий остаётся отдельной операцией через модель/CLI; polling панели выполняется каждые 2 секунды, во время POST обновление статуса не перезаписывает ответ команды более ранним снимком.

## CLI

```bash
# Один проход с явным opt-in.
python3 -m radioinspector.task_scheduler \
  --book tasks.json --enable --once

# Непрерывный запуск с ограничением по времени.
python3 -m radioinspector.task_scheduler \
  --book tasks.json --enable --interval 1 --timeout 60
```

После установки проекта доступен `radioinspector-task-scheduler`. Без `--enable` команда завершается с ошибкой и не запускает задачи. Continuous run без `--timeout` останавливается пользователем; `finally` вызывает `close()` и сохраняет operator abort активных задач.

Это программный lifecycle-планировщик, а не подтверждение аппаратных измерений или сетевой протокол удалённого контроля.
