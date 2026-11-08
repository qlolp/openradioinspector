# Дорожная карта первого общего выпуска

Актуальное состояние и блокеры: [release/status.md](release/status.md).
Состав и решения по PR: [release/integration.md](release/integration.md).

## Объём v0.4.0

Ядро, `/api/v1` и legacy API, общий архив, независимые tinySA/AR-DV1,
обнаружение и handoff, WebUI/Pi, Windows и macOS. Задания, DOCX, аналитика
и экспорт включаются в объёме зафиксированного #87. Новые функции поверх
этого среза остаются в следующих выпусках.

| Критерий | Реализация | Проверка | Аппаратное подтверждение |
|---|---|---|---|
| API, UTC, исходные единицы | `contracts.py`, `web/server.py` | API/контрактные тесты, `test_release_v040.py` | Требуется для реальных значений |
| Независимые приборы и порты | `device_workers.py`, `serial_instruments.py` | lifecycle, PTY, mocked COM, isolation regressions | Два прибора одновременно: BLOCKED |
| tinySA → AR-DV1 | automatic detection/handoff/completion | Симуляторы, retry/concurrency/происхождение | Handoff без остановки обзора: BLOCKED |
| Архив, восстановление, лимиты | `session_store.py`, archive import/export | Транзакции, rollback, capacity, перенос байтов | Перезапуск/потери USB/нехватка места: BLOCKED |
| Задания | `task_scheduler.py` | Планирование, отмена, recovery, безопасная запись | Реальное задание и перезапуск: BLOCKED |
| DOCX/аналитика | report/measurement modules | Эталонные данные, атомарность, единицы | Межплатформенный сценарий: BLOCKED |
| WebUI | `web/static/` | Node и HTTP smoke | Два клиента на Pi: BLOCKED |
| Windows/macOS | Python UI/SwiftUI, `launcher.py` | CI, bootstrap HTTP/SQLite и EOF | Чистая установка/USB: BLOCKED |
| Pi | `packaging/pi/` | Сценарий venv/systemd, HTTP readiness | Перезагрузка/USB/длительный прогон: BLOCKED |
| Юридическая чистота | Проверка происхождения/состава, guard | Известные blob/path, пакеты и репетиция истории | Требуются документы и внешний архив |

Исторические #5–#19 и записи `roadmap-progress.md` отражают разработку в ветках;
они не означают слияние в main, готовую поставку или аппаратную приёмку.
Отсутствие отчёта PASS означает незавершённую работу.
