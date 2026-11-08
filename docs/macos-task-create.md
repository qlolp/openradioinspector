# macOS: создание ControlTask

`PlatformControlTaskDraft` проверяет `name`, `task_file`, положительное конечное `duration_s` и ISO 8601 `startAt`, нормализуя время через `ISO8601DateFormatter` к UTC `Z`. `createTask(draft:)` отправляет `POST /task-scheduler/tasks` и декодирует sanitized `PlatformControlTask` без локальных путей.

Панель scheduler содержит форму «Новая ControlTask…». `RemoteDualModel.createTask()` захватывает client/generation, блокирует повтор через `taskCreatePending`, показывает `name/status/startAt` ответа или ошибку и обновляет список через `refreshTaskScheduler`. `status` и `outcome` назначает сервер, а не клиент.

XCTest проверяет JSON без поддельных status/outcome, sanitized response и отклонение invalid date/duration. Полное AppKit-приложение и аппаратный стенд не проводились.
