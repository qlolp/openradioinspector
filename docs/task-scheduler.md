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
