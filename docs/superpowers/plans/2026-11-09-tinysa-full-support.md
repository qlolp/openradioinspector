# Полная поддержка tinySA Ultra ZS405

Утверждено пользователем 9 ноября 2026: реализовать ранее согласованный полный
план. Типизированные команды + удалённый экран, генератор отдельно в lab.

## Цель и ограничения

Одно ядро владеет USB; WebUI/Pi, Windows и macOS используют совместимое
`/api/v1`. Основной профиль — ZS405/HW0.4.5.1/firmware
`tinySA4_v1.4-235-gea71dfd`; исходники производителя зафиксированы на
`ea71dfd9e53d6dec95256ac70914659cf260d48d`. PR119 — приёмная основа.
Не переносить Basic/ZS406/ZS407 пределы на ZS405. В monitoring RF не включать.
Аппаратный ACK не равен измеренному состоянию выхода. Не имитировать I/Q,
USB PCM, одновременный tracking generator или физическую приёмку.

## Этапы

1. Каталог всех пунктов меню и capability evidence; ограниченные текстовые/
   бинарные codecs и typed command registry. `scanraw` имеет отдельную сетку,
   формат уровней и конфигурацию zero; не использовать inclusive scan decoder.
2. Один controller на существующий worker, операции/jobs, exclusive lab lease,
   capture 480×320, touch/release, удержание и keypad. В monitoring экран только
   просмотр. Все потенциальные RF/preset/menu изменения требуют lab и явного
   разрешения возможной генерации. Истёкшая lease не разрешает старый touch.
3. Полные receive settings: start/stop/center/span, zero-span/multiband,
   RBW/VBW/points/time/repeat/accuracy, attenuator/LNA/Ultra/spur/corrections,
   traces/calc/markers и trigger. Raw/processed/timed artifacts различаются.
4. Measurement presets и host analysis, SD artifacts, presets/CSV, calibration,
   self-test, firmware и expert workflows с явными физическими шагами.
5. Генератор/AM/FM/sweep/CAL/LO/pulse в lab; LISTEN и общий аналоговый audio
   capture/playback. Отсутствующий audio input показывает причину, не fake PCM.
6. Общие API + полнофункциональные панели трёх клиентов, реальные HTTP и
   controller regressions, поставки, opt-in bench и восьмичасовой протокол.

## Интерфейсы и данные

Совместимые маршруты
`/api/v1/sessions/{sid}/streams/{stream_id}/instrument/{resource}`:
capabilities/settings/operations/screen/control-lease/artifacts. Нет raw shell
endpoint. Длительная операция возвращает ID с состоянием/отменой; повторный
request_id не повторяет mutation. Все sid/stream/generation проверяются.
Рабочая очередь сохраняет single-flight; отмена неопределённого I/O требует
нового stop/start и не скрывает taint.

Raw sweeps и старый архив не переписываются. Processed trace, zero-span,
screen, SD file и measurement result сохраняются как типизированные artifacts
с raw bytes, provenance, interval UTC и checksum. Artifact schema версионируется
отдельно от конкурентных migrations основного архива.

## Приёмка

Каждое поведение сначала воспроизводится failing regression, затем исправляется.
Проверить binary embedded prompt/fragmentation/limits/endian/grid, leases/
stale replies/cancel/restart, независимость ARDV1, exact data/units/provenance,
round-trip SQLite/SD, реальные HTTP и три клиента, чистые установки и CI.
Аппаратные результаты и восьмичасовой стенд отдельны от synthetic PASS.
Полный охват — каждый пункт меню имеет реализованный путь управления либо
явно документированное физическое/профильное ограничение, без ложного статуса
аппаратной готовности. Подробный журнал выполнения хранится приватно вне Git.
