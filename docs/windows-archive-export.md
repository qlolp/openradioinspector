# Windows: экспорт NDJSON snapshot

`AnalyticsWindowsApp` содержит кнопку «Скачать NDJSON snapshot…». Она явно импортирует `tkinter.filedialog`, захватывает подключение и `session_id` до открытия `asksaveasfilename` и вызывает `PlatformClient.export_archive()` в существующем background action-канале. Повторная операция во время `_action_running` не запускается.

Клиент использует общий `fetch_archive_ndjson`: серверный streaming snapshot записывается во временный каталог, полностью проверяется парсером, сравнивается с `X-Archive-Sweeps`/`X-Archive-Observations` и только затем атомарно заменяет выбранный файл. Архив не материализуется весь в память. Стабильные ID, целые Гц, `dBm` и `s_meter_count` сохраняются без преобразования.

Видимый `archive_export_label` показывает число записей и SHA-256 либо текст ошибки. Сбой не останавливает live-измерения, analytics или TaskScheduler. Повторный export не перезаписывает старый файл при incomplete stream.

`tests.test_windows_archive_export` проверяет реальный HTTP round trip, исходные единицы, immutable snapshot, захват session/destination и отображение результата или ошибки. Аппаратный стенд не проводился.

Вложенный цикл событий файлового диалога не допускает повторный экспорт.
Cancel не отправляет запрос; смена сеанса/подключения, начало другого действия
или закрытие окна во время диалога отменяют выбранный путь. Поздний ответ
не заменяет статус нового контекста. Hidden Tk regression запускается через
`ORI_WINDOWS_RELEASE_TK=1 python -m unittest tests.test_windows_release_flows`:
используются реальные кнопки и переменные Tk, но диалог и API подменены,
пользовательские файлы не открываются. Native-диалог Windows OS не проверен.
