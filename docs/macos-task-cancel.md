# macOS: отмена ControlTask

`PlatformClient.cancelTask(name:)` валидирует непустое имя, кодирует его как один URL-сегмент (`Self.segment`) и вызывает `POST /task-scheduler/tasks/{name}/cancel` с пустым телом.

В панели scheduler кнопка «Отменить» показывается только для задач со статусом `В очереди на выполнение`. Действие блокируется `taskCancelPending`, захватывает `generation` и клиента, поэтому поздний ответ не перезаписывает состояние нового сеанса. После успешной отмены список TaskBook обновляется, а `name · status · outcome` показывается в `taskCancelStatus`. Повторная отмена или running отклоняется сервером (HTTP 400/404); приборные потоки и live-измерения не затрагиваются.

`PlatformWorkflowTests.testCancelTaskUsesEncodedNameAndSanitizedResponse` проверяет encoded slash, пустое тело, sanitized ответ и отклонение пустого имени до запроса; прогон выполнен Swift 6.0.3 на Linux (12/12 suite). Полное AppKit-приложение и аппаратный стенд не проводились.
