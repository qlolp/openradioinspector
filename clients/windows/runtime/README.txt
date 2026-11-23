OpenRadioInspector • Windows 11 x64

В приложении нажмите «Подключить локальный узел». Окно можно закрыть.
Управление из этого каталога: ori-supervisor.exe windows-node start | status | shutdown
Автозапуск при входе: ori-supervisor.exe windows-node register
Отмена автозапуска: ori-supervisor.exe windows-node unregister
Локальный API и веб-интерфейс: http://127.0.0.1:8770/modern/
Архив по умолчанию: %LOCALAPPDATA%\OpenRadioInspector-Windows-RC1\sessions.sqlite3
Перед обновлением остановите узел. Если архив перенесён, подключите выбранный том.
Перед установкой прежней версии выполните из этого каталога
ori-supervisor.exe windows-node prepare-rollback
Дождитесь возврата проверенной базы в каталог узла. Для ядра, читающего только v6, выполните
ori-core.exe archive-materialize SOURCE.sqlite3 NEW.sqlite3. Выберите новый файл.

В этом каталоге откройте operator-guide\index.html для просмотра офлайн-инструкции со снимками.
