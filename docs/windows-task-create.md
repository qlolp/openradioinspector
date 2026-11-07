# Windows: создание ControlTask

`SchedulerWindowsApp` содержит кнопку «Новая ControlTask…». Форма принимает `name`, `task_file`, `start_at`, `duration_s` и `equipment`. `task_create_payload()` проверяет обязательные поля, положительное конечное `duration_s` и ISO 8601 `start_at`; naive-время трактуется как UTC, offset нормализуется к `Z`.

`PlatformClient.create_task()` отправляет `POST /api/v1/task-scheduler/tasks` через общий background action. `status` и `outcome` назначает сервер; `task_file`/`panorama_file` в ответ не включаются. Видимый `task_create_label` показывает `name/status/start_at` созданной задачи или ошибку валидации.

`tests.test_windows_task_create` проверяет нормализацию offset, отклонение invalid, реальный POST, sanitized ответ и вызов UI action. Аппаратный стенд не проводился.
