OpenRadioInspector RC1 • Windows 11 x64

В приложении нажмите «Подключить локальный узел». Окно можно закрыть.
Управление из этого каталога: ori-supervisor.exe windows-node start | status | shutdown
Автозапуск при входе: ori-supervisor.exe windows-node register
Отмена автозапуска: ori-supervisor.exe windows-node unregister
Локальный API и веб-интерфейс: http://127.0.0.1:8770/modern/
Архив пользователя: %LOCALAPPDATA%\OpenRadioInspector-Windows-RC1\sessions.sqlite3
Перед обновлением остановите узел. Для предыдущей версии архива выполните
ori-core.exe archive-materialize SOURCE.sqlite3 NEW.sqlite3. Выберите новый файл.
