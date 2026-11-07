# macOS: экспорт архива в NDJSON

`PlatformClient.exportArchive(sessionID:destination:)` вызывает streaming `GET /sessions/{id}/archive/export.ndjson` через `URLSessionDownloadTask` и не собирает архив в памяти. Клиент проверяет origin, HTTP status, `Content-Type: application/x-ndjson` и counters `X-Archive-Sweeps` / `X-Archive-Observations` до замены файла.

Перед заменой файла потоковый validator проверяет header `openradioinspector-archive-ndjson`, `format_version=1`, `session_id` и точное число записей. Incomplete snapshot не заменяет существующий файл. Перенос выполняется через `replaceItemAt` либо `moveItem`, временный каталог удаляется после завершения.

Панель `RemoteAnalyticsPane` содержит кнопку «Скачать NDJSON snapshot…» с `NSSavePanel`. `archiveExportPending` блокирует повтор, а generation/session guard отбрасывает поздние ответы. Видимый статус показывает `sweeps`, `observations` и имя файла либо ошибку, не останавливая live-измерения.

XCTest проверяет исходные `dBm` и `s_meter_count`, счётчики и сохранение старого файла при несовпадении headers. Тест помечен `#if !os(Linux)`: `URLSessionDownloadTask` с кастомным URLProtocol падает в corelibs-foundation на Linux, поэтому прогон выполняется на macOS. Полное AppKit-приложение и аппаратный стенд не проводились.
