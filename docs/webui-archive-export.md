# WebUI: экспорт NDJSON snapshot

Панель «Экспорт архива» на `/platform` содержит ссылку «Скачать NDJSON snapshot». Она указывает на существующий `GET /api/v1/sessions/{id}/archive/export.ndjson` для выбранного сеанса. Браузер не собирает и не изменяет записи: snapshot и счётчики формирует сервер.

`archive-export.js` отслеживает `session-select` через `change` и `MutationObserver`, кодирует ID как один URL-сегмент и задаёт безопасное имя download. При отсутствии сеанса ссылка отключена через `aria-disabled`; click в этом состоянии отменяется. Скрипт не выполняет `fetch`, POST или запись в архив.

NDJSON содержит header `openradioinspector-archive-ndjson`, стабильные ID, целые Гц и исходные `dBm`/`s_meter_count`. `X-Archive-Sweeps` и `X-Archive-Observations` соответствуют тому же snapshot. Прерывание соединения считается ошибкой экспорта, exactly-once transfer в неопределённом сетевом окне не заявляется.

Acceptance `tests.test_webui_archive_export` проверяет DOM-контракт и фактический streaming endpoint. Аппаратный стенд не требуется.
