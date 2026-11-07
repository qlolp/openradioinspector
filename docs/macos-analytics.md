# macOS: сводка всего архива по явному запросу

`PlatformClient.snapshot()` больше не вызывает `/api/v1/sessions/{id}/analytics/summary` при периодическом обновлении SwiftUI. Поля `PlatformSnapshot.analytics` и `analyticsError` сохранены для совместимости модели, но routine snapshot оставляет их пустыми; workers, sweeps, observations, кандидаты, исследования, политики и TaskBook обновляются независимо.

Кнопка «Запросить сводку» в `RemoteAnalyticsPane` вызывает `RemoteDualModel.refreshArchiveAnalytics()`. Метод захватывает client, session ID и generation, блокирует повторный запрос через `archiveAnalyticsPending`, а поздний ответ прежнего сеанса или подключения отбрасывает. Смена сеанса очищает результат и состояние pending. Обычный polling больше не перезаписывает полученную сводку.

Ошибка отображается в панели и не останавливает live-измерения. Исходные `dBm` и `s_meter_count` не преобразуются друг в друга. Классификация сигнала и точное измерение занимаемой полосы не заявляются.

XCTest `testSnapshotDecodesWorkflowAndIndependentPolicies` проверяет, что snapshot выполняет ноль вызовов analytics, а прямой `analyticsSummary(sessionID:)` выполняет один и декодирует исходные единицы. Полное AppKit-приложение и аппаратный стенд этим изменением не проверялись.
