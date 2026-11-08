# Установка, данные и откат

Сценарии ниже реализованы, но готовые установщики и чистые ОС пока не приняты.
Сборки предварительные и неподписанные; подпись/notarization не заявляются.

## Python / локальное ядро

Из wheel: `python -m pip install 'openradioinspector-<version>-py3-none-any.whl[serial]'`.
Запуск полного рабочего места: `radioinspector-desktop`.
Удалённый режим: `radioinspector-desktop --server https://trusted-core.example`.
Для проверки пакета без GUI/приборов: `radioinspector-desktop --self-check --data-dir /private/test-data`.

Отдельное ядро: `radioinspector-core --data-dir /private/data --port 8770`.
`--check-ready` проверяет HTTP уже запущенного ядра без открытия базы.
Запуск ядра сам по себе не открывает приборы и не включает сбор измерений.
Одновременное использование каталога двумя экземплярами launcher запрещено.

Данные Windows: `%LOCALAPPDATA%\OpenRadioInspector`; macOS:
`~/Library/Application Support/OpenRadioInspector`; Linux:
`$XDG_DATA_HOME/openradioinspector` или `~/.local/share/openradioinspector`.
Архив — `sessions.sqlite3`, задания — `tasks.json`. Журналы установленного desktop и отдельного ядра — `logs/desktop.log`, `logs/core.log` в каталоге данных, UTC, ротация 1 МиБ и три предыдущих файла. Ошибка запуска desktop показывается в диалоге. Сохранять весь каталог,
включая SQLite WAL/SHM, только после корректной остановки записи и ядра.
Каталог заданий на POSIX должен принадлежать пользователю и запрещать чужую запись.

## Windows

На сборочной машине: `powershell -File packaging/windows/build.ps1`.
Результат: `build/windows/packages/*-windows-x64-unsigned.exe`.
Установщик работает для текущего пользователя, включает Python/NumPy/Tk/pySerial.
Удаление приложения не должно удалять пользовательский каталог измерений.
Проверить на чистой Windows установку, запуск, обновление, удаление и сохранность данных.
Существующие команды `radioinspector-windows` и CLI сохранены.

## macOS

На macOS 14 или новее, с Xcode: `bash packaging/macos/build.sh`.
Результат: `build/macos/packages/*-unsigned.dmg`, архитектура сборочной машины.
SwiftUI `.app` включает отдельное локальное ядро. EOF родительского канала
останавливает только собственный процесс; чужие процессы по имени не завершаются.
`ORI_REMOTE_SERVER=https://trusted-core.example` запускает приложение без локального ядра.
Для удалённого подключения Swift-клиент требует HTTPS; настройте доверенный
TLS reverse proxy перед ядром на Pi. Не ослабляйте проверку сертификатов.
Отдельно проверить Gatekeeper, доступ к USB, обновление и выход приложения.

## Raspberry Pi

Требуются Python >=3.10 с venv, systemd и группа dialout.
Передать wheel и каталог `packaging/pi` на Pi:
`sudo sh packaging/pi/install.sh /absolute/path/openradioinspector-<version>-py3-none-any.whl`.
Установщик останавливает существующую службу, сохраняет данные, ставит пакет в
`/opt/openradioinspector/venv`; ядро работает от `openradioinspector` с доступом к dialout.
Данные: `/var/lib/openradioinspector`; журнал: `journalctl -u openradioinspector`.
Конфигурация: `/etc/default/openradioinspector`, по умолчанию bind `127.0.0.1:8770`,
лимит архива 1 ГиБ. Установщик не скачивает коммерческие материалы.

Удалённый доступ разрешён только в доверенной сети; до проверенной
аутентификации не выставлять API в Интернет. Для Swift необходим HTTPS proxy.
Проверки: перезагрузка, HTTP readiness, отсутствие приборов, возврат USB,
заполненный диск, остановка службы во время измерения, сохранность архива.

## Резервирование и откат

Остановить приложение/службу и дождаться завершения приборного I/O. Скопировать
каталог данных в закрытое хранилище, записать SHA-256 и проверить восстановление
в отдельном каталоге. До обновления сохранить предыдущий установщик/wheel.
Для отката остановить новую версию, установить прежнюю и восстановить совместимую
копию данных. Не выполнять скрытное понижение формата архива.

Контроль состава: `python tools/release/check_public_tree.py --artifact <wheel-or-sdist>`.
Полный выпуск дополнительно требует перечня фактически включённых runtime-компонентов,
их лицензий/уведомлений, SHA-256 и установки без исходного checkout.
