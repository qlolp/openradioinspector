# Ревью deep-исследования возможностей RadioInspectorRT — 11 ноября 2026

Дополнение к [research-rt-capabilities-2026-11-11.md](research-rt-capabilities-2026-11-11.md)
и к [rt-comparison.md](rt-comparison.md).

## 1. Предмет и рамки ревью

11 ноября 2026 года агент выполнил deep-исследование возможностей коммерческой
программы **RadioInspectorRT** (ООО «РадиоСофт») и сопоставил их с текущим
состояниий OpenRadioInspector.

Задача ревью: изучить возможности RadioInspectorRT (24/7 и realtime-анализ
диапазонов частот и выбранных сигналов, интеграция более чем с 200 моделями
анализаторов/приёмников/SDR) и сравнить их с возможностями программы в
репозитории `qlolp/openradioinspector` **на текущее состояние** (main, 2026-11).

## 2. Проверенные факты текущего состояния OpenRadioInspector

| Объект | Факт |
|---|---|
| HEAD на момент ревью | `80c549c` (2026-11-11), ветка `deep-research-rt-review-2026-11-11` |
| Рекорд deep-исследования | `docs/research-rt-capabilities-2026-11-11.md` (добавлен в `80c549c`) |
| Код | ~36 400 строк Python, 128 модулей, ~59 модулей + 6 пакетов, пакет `monitoring/` |
| CLI / API | 17 команд; legacy `/api/*` + `/api/v1/*` (sessions, workers, sweeps, observations, candidates, investigations, automatic policies, task-scheduler, events, archive/capacity, capabilities, health) |
| Клиенты | Raspberry Pi WebUI, Windows (Tkinter), macOS (SwiftUI) |
| Тесты | ~1737; release runner Linux/Python 3.12 — 1422, 0 skips; tinySA slice — 1518, 0 skips |
| Релиз | `0.4.0.dev0`; аппаратная приёмка — BLOCKED |

## 3. Результаты deep-исследования RadioInspectorRT

- Профиль RT: поиск источников излучений, собственная функция — контроль средств
  РЭП (в руководствах не раскрыта).
- 9 задач, 18 средств, 11 блоков интерфейса, 36 разделов подключения приборов.
- Заявленное покрытие: «более 200 различных моделей» и «более 160 устройств» —
  маркетинговые формулировки, расхождение с документацией не разрешено.
- Опции полной RT: DTest, RT2/RT3, RT-Geo, RT-RemoteMD, RT-SoundScaner, RT-DB,
  RT-AN, RT-Meas; RT-Light — только AOR/ICOM сканирующие приёмники; I_MasterDevice,
  I_RemoteCheck, IQ_ProcessPRO, RadioInspectorRP, RadioInspectorWiFi.

## 4. Сопоставление с OpenRadioInspector (итоги ревью)

По 33 функциональным блокам сопоставления:

- реализовано полностью — 13 блоков;
- реализовано первым срезом (read-only / с очерченными границами) — 10;
- не реализовано — 5;
- частично — 4.

Реальные расхождения: глубина реализации, метрология и quirks приборов,
аппаратная приёмка (синтетические срезы), закрытые цифровые декодеры DTest.

## 5. Вердикт ревью

- По контролю диапазона, порогам, сигналам, измерениям, планированию, отчётности
  — OpenRadioInspector уже близок к RadioInspectorRT.
- По платформенности, открытым форматам и лицензионной модели OpenRadioInspector
  превосходит коммерческий оригинал.
- Оставшиеся разрывы: глубина подсистем, проприетарные DTest-декодеры, сетевой
  аудиоконтроль, ГИС с пеленгами, аппаратная приёмка.

Подробности: [rt-comparison.md](rt-comparison.md),
[research-rt-capabilities-2026-11-11.md](research-rt-capabilities-2026-11-11.md).

## 6. Что осталось за рамками ревью

- Физическая приёмка (BLOCKED), цифровые демодуляторы, метрологическая аттестация,
  лицензионные условия RadioInspector — за пределами воспроизведения.
