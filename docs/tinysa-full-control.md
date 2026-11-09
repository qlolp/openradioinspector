# tinySA Ultra ZS405: управление общим ядром

Реализован выбранный гибрид: типизированные операции плюс живой удалённый
экран. WebUI/Pi открывает **«Управление tinySA»**, Windows — вкладку
**«tinySA · lab / экран»**, macOS — одноимённую кнопку в основном окне.
macOS встраивает общую панель в WKWebView; измерительная логика и USB остаются
в ядре. Сначала создайте и запустите serial-сеанс с tinySA, затем подключите панель.

## Профиль и охват

`tinysa-ultra-zs405-control-v1` проверяет ту же точную identity, что и
[приёмная основа](tinysa-receive-profile.md): ZS405, HW0.4.5.1,
`tinySA4_v1.4-235-gea71dfd`. Другой firmware требует нового проверенного
профиля. Начальный безопасный диапазон/число точек старого serial-сеанса
сохраняются. После подключения управления настройки расширяются явно.

| Группа | Реализованный путь |
|---|---|
| Частоты, center/span/zero span, RBW 200 Гц–850 кГц/auto, 2–450 точек, время, repeat, accuracy | Типизированные настройки; бинарный scanraw отдельно до 65 536 точек |
| Attenuator/LNA, Ultra, spur, external gain, trigger | Типизированные настройки с проверкой сочетаний и диапазона до отправки |
| Calc/min/max/average/quasi, display units/ref/scale, четыре traces, восемь markers | Настройки и формы trace/marker; обработанные значения архивируются отдельно |
| VBW, multiband/alternating, waterfall, остальные DISPLAY и MARKER пункты | Настоящий интерактивный экран в lab; без придуманных shell-команд |
| Встроенные MEASURE: гармоники, AM/FM, IMD/OIP3, phase noise, SNR, NF, channel power и другие | Live screen + сохранение результата/SD; набор проверяется на фактической прошивке |
| Измерения ядра: sampled peak, occupied bandwidth, −3 dB bandwidth, centroid, channel power estimate | По исходному scanraw, с исходными массивами, UTC-интервалом, единицами и предупреждениями |
| SD/CSV/PNG/LOAD CMD, пресеты | Native SD list/read/delete, slots0–4, переносимый JSON приёмных настроек; CSV/командные файлы/SD preset импортируются штатным меню в lab |
| Calibration/touch/level correction/firmware/expert/USB disk | Пошаговые процедуры + live menu; физические подключения, касания и DFU выполняются оператором |
| Sine generator, AM/FM, уровень, частота, sweep, CAL | Отдельный lab, типизированные формы; LO/PULSE/square/RF test/extended — штатный экран по доступности ZS405 |
| LISTEN | Штатное меню + аналоговый audio input ядра → WAV-артефакт → воспроизведение клиентом |

Каталог содержит пункты официального [MenuTree](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.MenuTree),
но эта страница описывает **1.3507+ и обновления Ultra+**. Каждый пункт имеет
`availability=check_live_screen`, а не ложный признак аппаратной поддержки FW235.
Проверяйте наличие по живому экрану. Команды дополнительно сверяются с `help`.
Basic, ZS406 и ZS407 не наследуются. Все видимые пункты доступны через touch,
удержание, release и numeric keypad; текстовые имена — через экранную клавиатуру.
Нестабильные номера меню не зашиты в код.

Основной generator sine: 100 кГц–800 МГц, −115…−19 dBm. Ultra разрешает
явные запросы наблюдений до 6 ГГц, что **не подтверждает калиброванную точность**,
чувствительность или отсутствие гармонических откликов. Верхние режимы и
пределы уточняются по прибору/руководству, а не по маркетингу Ultra+.

## Владение прибором и возврат к измерениям

В monitoring экран только читается; generator не включается автоматически.
Lab требует явного checkbox возможной RF-генерации, получает секретную
эксклюзивную аренду на 30 с и приостанавливает только tinySA worker.
Панель продлевает аренду каждые 8 с; heartbeat не ждёт длинного SD I/O.
AR-DV1 и его очередь независимы. Другой клиент не может забрать управление.

Истечение аренды между операциями отправляет RF/CAL off и release, оставляя
прибор в lab. Истечение во время I/O прерывает обмен и требует stop/start.
Неопределённый/обрезанный ответ, отмена или обрыв **не повторяются** скрыто;
соединение помечается tainted. Отмена активной операции закрывает только
её worker. Результат старого соединения помечен `context_current=false`.

«Вернуться в monitoring» выключает RF/CAL, задаёт mode input и явный
консервативный набор приёмных настроек. Остальные неизвестные ручные изменения
не объявляются восстановленными. После ручного меню/trace changes данные
консервативно остаются processed artifacts; для исходного acquisition доступен
scanraw. Новая ревизия/connection/segment не смешивается со старой аналитикой.

ACK означает ответ протокола, **не физический RF readback**. LO/PULSE могут
потребовать отдельного выключения в CONFIG. При потере USB их состояние
неизвестно. Перед уходом со стенда проверяйте выходы внешним измерителем.
Нет одновременно analyzer/generator или tracking generator, USB PCM либо IQ.

## API и архив

База: `/api/v1/sessions/{sid}/streams/{stream_id}/instrument/`.

| Маршрут | Контракт |
|---|---|
| `GET capabilities`, `GET settings`, `GET control-lease` | Каталог, схемы форм, кэш состояния; без чтения USB из HTTP |
| `POST operations` | `{operation, parameters, request_id?, lease_id?, expected_revision?}` → 202 + `operation_id` |
| `GET operations/{id}` / `POST operations/{id}/cancel` | queued/running/succeeded/failed/cancelled/unknown, результат/ошибка и актуальность соединения |
| `GET screen` | 202 capture job; preview не пишет архив; `capture {save:true}` сохраняет PNG |
| `GET artifacts`, `GET artifacts/{id}`, `GET artifacts/{id}/content` | Метаданные, SHA-256 и точные байты, ограниченные текущими sid/stream |
| `POST settings`, `POST control-lease` | Совместимые короткие формы setting и acquire/renew/release/resume |

Параметры операций определяет `capabilities.operations`; shell endpoint нет.
Каждая панель создаёт приватную случайную capability `X-Instrument-Client`
(128 бит) на время своего контекста и передаёт её в POST и GET operations.
Без неё lab acquisition отклоняется; секрет `lease_id` выдаётся только при
чтении результата с той же capability. История и запросы другой панели
редактируют этот секрет. Не публикуйте capability/lease в логах или URL.
`request_id` не повторяет mutation. Один ID с иным запросом даёт 409;
после вытеснения истории повтор также отклоняется. Ledger runtime-only:
после перезапуска ядра не повторяйте неопределённую mutation автоматически.
Не более четырёх обычных активных jobs, дополнительный coalesced слот RF off,
128 результатов, 2048 mutation idempotency IDs. Preview, heartbeat, inspect,
service metadata и RF off этот ledger не расходуют; его заполнение не
блокирует heartbeat/off. Таймаут уже начатого I/O прерывает worker и выдаёт
unknown; ожидающая команда отменяется без скрытого позднего выполнения.
Preview ограничен двумя запросами/с; пользовательские артефакты не удаляются.

Бинарные ответы читаются по длине, не по тексту `ch>` внутри файла/пикселей.
Capture: RGB565 big endian 480×320 → PNG. SD: little-endian length prefix,
до 8 MiB. Text до 1 MiB. Scanraw: native integer/float32 grid, stop исключён,
zero readback, исходные wire bytes сохранены. Zero-span содержит UTC начало/
конец; равномерное время каждого sample не выдумывается.

`instrument_artifacts` — отдельная additive SQLite-таблица с schema_version1,
FK на sid/stream и прежним транзакционным ограничением ёмкости. Существующие
свипы и схема измерений не переписываются. Screen, SD, raw_scan, zero_span,
processed_trace, measurement и audio разделены.
Для artifacts list доступны `limit=1..100` и `offset`; все три клиента имеют
навигацию «Новее/Старее», старые записи не скрыты за первой сотней.
Memory archive ограничен 64 артефактами/32 MiB; при заполнении запись отклоняется, ничего не удаляется.
Для долговременной работы используйте SQLite. Старые экспорты свипов не
включают новые артефакты: скачивайте их через этот API или сохраняйте SQLite.

JSON-пресет ядра не является бинарным пресетом производителя. Весь документ
проверяется перед I/O; применение последовательное по ACK, без аппаратного
rollback. RF, произвольные команды и correction tables в этот формат не входят.

## Аудио, проверка и оставшаяся приёмка

Установите `pip install 'openradioinspector[audio]'` на компьютере ядра и
PortAudio ОС. Подключите аналоговый LISTEN output к выбранному входу ядра.
Форма «Аналоговые аудиовходы» показывает реальные устройства либо конкретную
причину недоступности. Запись: mono PCM16, 8/12/24/48 кГц, 0.1–30 с, WAV.
Переполнение, неполная запись и отмена не сохраняются как чистый результат.
Web/macOS воспроизводят WAV штатным audio player; Windows — winsound.
Это запись аналогового входа, не доказательство полноценной FM-демодуляции.

Автоматическая приёмка: `python -m tests.run_release_checks`; на macOS используйте
`python -m tests.run_release_checks --swift` для SDK/native/unsigned Xcode.
**Не запускайте bare `swift test`**: исторические smoke могут трогать прибор.
CI уже содержит Linux/Windows/macOS × Python3.10/3.12/3.14 и пакеты.
Синтетические тесты и Chromium не означают аппаратный PASS.

Физический opt-in, по умолчанию только приём:

```sh
python tests/check_tinysa_full_hardware.py --port /dev/ttyACM0 \
  --ardv1-port /dev/ttyUSB0 --output-dir /private/path/new-bench --hours 8
```

Проверка RF отдельно требует `--lab-rf-test --confirm-physical-setup`, нагрузки
и внешней фиксации реального уровня/спектра. Отчёт и SQLite хранятся вне Git;
короткий запуск отмечается partial. Полный checklist: [hardware acceptance](tinysa-hardware-acceptance.md).
Физические подключения, RF accuracy, LO/PULSE, аналоговый звук и восьмичасовой
двухприборный прогон в этой среде **не выполнялись**.

Источники: [USB interface](https://tinysa.org/wiki/pmwiki.php?n=Main.USBInterface),
[MenuTree](https://tinysa.org/wiki/pmwiki.php?n=TinySA4.MenuTree), официальные
исходники firmware на commit `ea71dfd9e53d6dec95256ac70914659cf260d48d`.
Исходники производителя и приватные wire-логи в репозиторий не добавлялись.
