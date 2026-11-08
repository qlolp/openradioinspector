# WebUI: создание ControlTask

Форма «Новая ControlTask» в панели scheduler использует `POST /api/v1/task-scheduler/tasks`. Поля: `name`, `task_file`, `start_at`, `duration_s`, `equipment`. Локальное `datetime-local` преобразуется в ISO 8601 UTC; `duration_s` проверяется как положительное конечное число. `status` и `outcome` назначает сервер, браузер их не подставляет.

`task-create.js` отправляет JSON, показывает sanitized `name/status/start_at` ответа или ошибку API и вызывает событие `ori:tasks-changed`. Существующий `scheduler.js` немедленно обновляет список задач и продолжает независимый polling. Ошибка POST не меняет live-измерения, а TaskBook остаётся атомарной серверной книгой.

`tests.test_webui_task_create` проверяет DOM-контракт, наличие POST, отсутствие поддельных status/outcome, локальную раздачу asset и связь обновления списка. Аппаратный стенд не требуется.
