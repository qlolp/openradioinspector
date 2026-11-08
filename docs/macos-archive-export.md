# macOS: экспорт NDJSON snapshot

`PlatformClient.exportArchive()` использует `URLSessionDownloadTask` вместо JSON-запроса с лимитом 2 МБ. Redirects отклоняются, проверяются final origin, HTTP status, `application/x-ndjson`, `X-Archive-Sweeps` и `X-Archive-Observations`.

Перед заменой файла потоковый validator проверяет header `openradioinspector-archive-ndjson`, `format_version=1`, `session_id` и точное число записей. Incomplete snapshot не заменяет существующий файл. Перенос выполняется через `replaceItemAt` либо `moveItem`, временный каталог удаляется после завершения.

Панель `RemoteAnalyticsPane` содержит кнопку «Скачать NDJSON snapshot…» с `NSSavePanel`. `archiveExportPending` блокирует повтор, а generation/session guard отбрасывает поздние ответы. Видимый статус показывает `sweeps`, `observations` и имя файла либо ошибку, не останавливая live-измерения.

XCTest проверяет исходные `dBm` и `s_meter_count`, счётчики и сохранение старого файла при несовпадении headers. Тест помечен `#if !os(Linux)`: `URLSessionDownloadTask` с кастомным URLProtocol падает в corelibs-foundation на Linux, поэтому прогон выполняется на macOS. Полная Xcode-сборка AppKit-приложения проверена на macOS arm64 без подписи; аппаратный стенд не проводился.

## Нативная проверка целостности

Download delegate завершает запрос ровно один раз и переносит файл URLSession
в собственный временный каталог до возврата callback. Это сохраняет файл после
завершения загрузки и исключает повторное возобновление continuation.

Validator разбирает каждую строку как JSON, считает только верхнеуровневый
kind, проверяет структуру записи и соответствие указанного session_id.
Неизвестная или повреждённая запись, неправильная версия header, чужой сеанс
или несовпадение числа записей не заменяют существующий файл. Финальный перевод
строки проверяется, исходные байты NDJSON и единицы измерений не изменяются.

Файл читается блоками по 256 KiB, но одна строка может занимать больше блока.
Память validator пропорциональна самой длинной строке; строгий предел её длины
не вводится, чтобы не отклонять допустимые большие sweeps. Общий размер архива
не загружается целиком в память.
