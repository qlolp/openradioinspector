# Установка OpenRadioInspector на Debian amd64

Комплект 0.4.0 для Debian 12 и 13 на x86-64 включает ядро с поддержкой последовательных USB-портов, supervisor и встроенный WebUI. Служба запускается через systemd и по умолчанию слушает `127.0.0.1:8770`. Прибор и задание выбирает оператор. Веб-браузер можно закрыть во время записи.

## Установка без сети

На целевой машине команда `dpkg --print-architecture` должна вывести `amd64`, а PID 1 должен принадлежать systemd. Заранее установите системные зависимости с носителя Debian или из репозитория своей версии ОС. Для работы службы нужны `libc6` версии 2.36 или новее, `libgcc-s1`, `ca-certificates`, `systemd` и `init-system-helpers`. При старте ядро загружает сертификаты для HTTPS-уведомлений из системного хранилища даже при автономной работе. SQLite и ресурсы браузерного интерфейса входят в комплект.

Перенесите DEB, манифест и `SHA256SUMS` из одного выпуска в отдельный каталог. Для публичного тега `linux-amd64-v0.4.0` используйте имя из примера ниже. В артефакте CI файл называется `openradioinspector-linux_0.4.0-1_amd64.deb`. Выполните команды из каталога загруженных файлов, подставив имя своего DEB.

```sh
sha256sum --check SHA256SUMS
sudo dpkg -i ./OpenRadioInspector-0.4.0-linux-amd64.deb
systemctl is-enabled openradioinspector-linux.service
sudo /usr/lib/openradioinspector-linux/check-install
```

Проверка выводит версию пакета, результаты проверки SHA-256 двух исполняемых файлов и состояние работающего supervisor. Откройте WebUI в браузере на этой машине. Подготовка приборов и работа с измерениями описаны во встроенном справочнике. [WebUI](http://127.0.0.1:8770/modern/) · [Офлайн-справочник](operator-guide/index.html)

| Путь или команда | Назначение |
|---|---|
| `/etc/openradioinspector-linux/supervisor.json` | Адрес, порты, путь к заданиям и ограничение архива. По умолчанию лимит архива составляет 64 ГиБ. |
| `/var/lib/openradioinspector-linux` | Измерения, задания и состояние службы. Каталог закрыт для остальных пользователей. |
| `/usr/share/doc/openradioinspector-linux/release.json` | Ревизия исходников, целевая ABI, хеши компонентов и WebUI. |
| `/usr/share/doc/openradioinspector-linux/notices` | Сведения о зависимостях и их лицензиях. |
| `journalctl -b -u openradioinspector-linux` | Журнал службы с момента загрузки. |

Разместите архив на локальном диске с поддержкой POSIX-владельцев и SQLite WAL. Для отдельного диска настройте постоянное монтирование каталога данных до установки. Учитывайте место под WAL, резервные копии и другие программы. Освободите порт 8770, если его уже занимает другой экземпляр ядра.

## USB и подключение

Systemd запускает службу от пользователя `ori-linux` с дополнительной группой `dialout`. Используйте отдельный порт для каждого приёмника и постоянные имена из `/dev/serial/by-id/`. После подключения прибора проверьте его имя и группу устройства. Выберите соответствующий порт и профиль прибора в WebUI перед началом сбора.

## Обновление и откат

Храните каждый комплект с контрольными суммами в отдельном каталоге. Перед обновлением завершите запись, остановите службу и сохраните конфигурацию и весь каталог данных на другом диске. Пример создаёт новый файл с закрытыми правами в существующем каталоге резервных копий.

```sh
sudo systemctl stop openradioinspector-linux
backup=$(sudo mktemp /mnt/backup/ori-debian-XXXXXXXX.tar)
sudo tar -C / -cpf "$backup" var/lib/openradioinspector-linux etc/openradioinspector-linux
sudo tar -tf "$backup" >/dev/null
sudo sha256sum "$backup"
sha256sum --check SHA256SUMS
sudo dpkg --force-confold -i ./OpenRadioInspector-0.4.0-linux-amd64.deb
sudo /usr/lib/openradioinspector-linux/check-install
```

Для отката перейдите в каталог предыдущего проверенного комплекта, повторите проверку `SHA256SUMS` и установите предыдущий DEB через `dpkg --force-confold -i`. Служба запускается после установки. Проверьте журнал и историю измерений в WebUI. Пакет сохраняет изменения оператора в `supervisor.json`. При обычном удалении и при `dpkg --purge openradioinspector-linux` измерения сохраняются. Операция `--purge` удаляет пакетную конфигурацию из `/etc`.

При первом старте служба создаёт архив схемы 7. Существующий архив схемы 6 открывается в прежнем формате. Для возврата к ядру с поддержкой только схемы 6 сохраните исходный каталог и до смены ядра создайте совместимую копию. Выходной файл должен быть новым.

```sh
sudo systemctl stop openradioinspector-linux
state=/var/lib/openradioinspector-linux
sudo -u ori-linux /usr/lib/openradioinspector-linux/ori-core archive-materialize \
  "$state/sessions.sqlite3" "$state/compatible-v6.sqlite"
```

## Сборка и объём проверки

На x86-64 Debian 12 используйте чистый checkout, Rust 1.99.1, Node 26.13.0, инструменты сборки C, CMake, Python 3.11+, binutils и dpkg. Каталоги вывода должны быть новыми.

```sh
npm ci --prefix clients/web --no-audit --no-fund
python3 packaging/pi/build_rc.py --platform linux-amd64 --distribution \
  --output-dir build/linux-amd64/release --rehearsal-dir build/linux-amd64/rehearsal
```

Workflow `linux-amd64.yml` собирает DEB на нативном x86-64 в Debian 12. Два задания загружают тот же DEB и запускают отдельные системы Debian 12 и 13 с PID 1 systemd и единственным сетевым интерфейсом loopback. Они проверяют чистую установку, обновление с тестового RC, откат, перезапуск, каждый ресурс WebUI и сохранность исходных синтетических измерений в архивах схем 6 и 7. В артефактах `openradioinspector-linux-amd64-proof-debian12-<SHA>-<attempt>` и `…debian13-<SHA>-<attempt>` файл `environment.json` привязывает результат к хешу DEB, а `lifecycle.jsonl` содержит результаты каждого этапа. Проверка физической установки, USB-приборов и длительной записи на диске станции требует отдельного стенда.
