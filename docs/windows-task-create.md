# Windows: создание ControlTask

`SchedulerWindowsApp` содержит кнопку «Новая ControlTask…». Форма принимает `name`, `task_file`, `start_at`, `duration_s` и `equipment`. Текстовый `StringVar` длительности (включая исходное `"60"`) преобразуется в число на границе формы; пустая строка, нечисловое, NaN, Infinity, ноль и отрицательные значения отклоняются. Общий HTTP API по-прежнему требует числовой JSON `duration_s`, строковый тип в API не разрешён. `task_create_payload()` проверяет обязательные поля, положительное конечное `duration_s` и ISO 8601 `start_at`; naive-время трактуется как UTC, offset нормализуется к `Z`.

`PlatformClient.create_task()` отправляет `POST /api/v1/task-scheduler/tasks` через общий background action. `status` и `outcome` назначает сервер; `task_file`/`panorama_file` в ответ не включаются. Видимый `task_create_label` показывает `name/status/start_at` созданной задачи или ошибку валидации.

`tests.test_windows_task_create` проверяет нормализацию offset, отклонение invalid, реальный POST, sanitized ответ и вызов UI action. Аппаратный стенд не проводился.

Повторное нажатие во время запроса не создаёт ещё одну задачу. Закрытая,
заменённая или оставшаяся от прежнего подключения форма не отправляется;
ответ старого контекста не перезаписывает текущий статус.
`ORI_WINDOWS_RELEASE_TK=1 python -m unittest tests.test_windows_release_flows`
проверяет настоящие Tk Entry/StringVar/Button callbacks на собственных скрытых
окнах с синтетическими ответами. Это не запуск на Windows OS и не аппаратный стенд.
