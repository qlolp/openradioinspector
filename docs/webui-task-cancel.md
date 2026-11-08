# WebUI: отмена ControlTask

В списке TaskBook задачи со статусом `В очереди на выполнение` получают кнопку «Отменить». Она вызывает `POST /task-scheduler/tasks/{name}/cancel` с `encodeURIComponent(name)`, поэтому имя остаётся одним URL-сегментом.

`cancelTask()` блокирует повтор через `actionPending` и generation. После успешной отмены снимок `pending` сбрасывается и `refreshScheduler()` немедленно обновляет список; ошибка отображается в статусе scheduler и не останавливает live-измерения. Running-задача кнопки не имеет: сервер отклоняет её отмену, а приборные потоки не затрагиваются.

`tests.test_webui_task_cancel` проверяет ограничение кнопки queued, encoded name, отсутствие `innerHTML`/`outerHTML` и немедленный refresh. Аппаратный стенд не проводился.
