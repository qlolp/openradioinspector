# Windows: отмена ControlTask

`PlatformClient.cancel_task(name)` кодирует имя как один URL-сегмент и вызывает `POST /task-scheduler/tasks/{name}/cancel`. Форма `SchedulerWindowsApp` предлагает только задачи со статусом `В очереди на выполнение` из текущего sanitized списка.

Отмена выполняется через background action и показывает `name/status/outcome` в видимом `task_cancel_label`. Повторная отмена или running отклоняется сервером; приборные потоки и live-измерения не затрагиваются.

`tests.test_windows_task_cancel` проверяет encoded slash, sanitized response и UI action. Аппаратный стенд не проводился.
