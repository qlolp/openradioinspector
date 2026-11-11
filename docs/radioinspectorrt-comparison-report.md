# Сравнительный анализ возможностей программного комплекса RadioInspectorRT и открытой платформы OpenRadioInspector

> **Примечание о редакционной сверке.** Это скорректированная версия сравнительного отчёта. Все факты об OpenRadioInspector выверены по коду текущего `main` (`80c549c`, ноябрь 2026) в ходе независимого ревью ([`docs/reviews/2026-11-09/README.md`](reviews/2026-11-09/README.md), а также `docs/rt-comparison.md`). Исправления: версия схемы архива v3 → **v6**, статус напряжённости поля/единиц, «полный паритет» → «первый срез» для адаптивного порога и фонограмм, Wi-Fi → «не подключён», удалён несуществующий `audio_control.py`, уточнён RemoteMD (PCM-транспорт есть). Ключевые замечания к измерениям и безопасности, требующие внимания, помечены отдельно.

Развитие средств программно-определяемого радио (Software-Defined Radio, SDR), портативных анализаторов спектра и автоматизированных систем радиоконтроля радикально изменило архитектуру комплексов радиомониторинга и технических средств защиты информации (ТСЗИ / TSCM). На протяжении более чем двух десятилетий эталоном промышленного программного обеспечения на постсоветском пространстве и ряде международных рынков выступало семейство программ **«РадиоИнспектор»** ([RadioInspector](https://www.radioinspector.com/)), разработанное компанией ООО «Лаборатория радиоизмерений» и продвигаемое дистрибьюторами, включая [ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspector) и [Waypoint Counter Surveillance](https://waypointcs.ca/tscm-equipment-sales/radio-inspector/). Центральное место в этой линейке занимает модификация **RadioInspectorRT** ([RadioInspectorRT](https://www.inspectorsoft.ru/radioinspectorrt)), специализированная для круглосуточного (24/7) поиска несанкционированных радиоизлучений, закладных устройств и оперативного спектрального анализа в реальном времени.

Параллельно с проприетарными комплексами развивается открытая инженерная реализация — проект **OpenRadioInspector** ([qlolp/openradioinspector](https://github.com/qlolp/openradioinspector)), распространяемый под лицензией MIT и находящийся на стадии релиза `0.4.0.dev0` (актуальный срез ветки `main` на ноябрь 2026 года — ревизия `80c549c`). Проект ставит целью воспроизведение методического, математического и алгоритмического фундамента классического комплекса «РадиоИнспектор» на современном кроссплатформенном стеке (Python, SQLite, REST API, WebUI, macOS SwiftUI и Windows Tkinter) с отказом от аппаратных ключей защиты и закрытых форматов хранения данных.

Настоящее исследование представляет собой инженерный и архитектурный аудит функциональных возможностей коммерческого комплекса **RadioInspectorRT** (включая его опциональные расширения `DTest`, `RT-Group`, `RT-Geo`, `RT-Meas`, `RT-AdvancedDB`, а также сопутствующие подсистемы `I_MasterDevice`, `I_RemoteCheck`, `IQ_ProcessPRO` и облегченную сборку `RadioInspectorRT-Light` дистрибутива 32314) в прямом сопоставлении с текущим состоянием кодовой базы и документации репозитория **OpenRadioInspector** ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)).

## Архитектурная парадигма и позиционирование систем

### Экосистема коммерческого пакета RadioInspectorRT

Программное обеспечение серии «РадиоИнспектор» строилось как модульное семейство Windows-приложений (выполненных на базе среды Delphi/VCL с использованием СУБД FireBird 2.5 и библиотек быстрого преобразования Фурье `libfftw3f-3.dll`, согласно [инвентаризации коммерческого дистрибутива](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)). Внутри семейства проведено четкое разделение ролей между базовыми пакетами ([Program Options - Waypoint Counter Surveillance](https://waypointcs.ca/tscm-equipment-sales/program-options/)):

1. **RadioInspectorRC (Radio Control):** Измерительная версия с полной метрологической поддержкой, предназначенная для гражданского радиоконтроля, надзора за использованием радиочастотного спектра и паспортизации радиоэлектронных средств (РЭС). Она в обязательном порядке работает через сервер управления приборами `I_MasterDevice`, который учитывает коэффициенты калибровки антенн, затухание в фидерных линиях и инструментальные погрешности тракта ([RadioInspector - ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspector)).
2. **RadioInspectorRT (Real-Time / TSCM):** Поисковая модификация, адаптированная для задач выявления негласных датчиков перехвата информации (НДП), закладок и нелицензированных передатчиков в режиме реального времени 24/7 ([RadioInspectorRT - ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspectorrt)). В базовой конфигурации `RadioInspectorRT` обращается к измерительным приборам напрямую в локальном режиме без прослойки `I_MasterDevice` (сетевое подключение доступно при покупке опции `RT-RemoteMD`), содержит встроенную подсистему записи фонограмм и управления вторым связным приемником, а также функцию управления передатчиками радиоэлектронного подавления (РЭП / джамминга) при срабатывании критериев обнаружения ([Program Options](https://waypointcs.ca/tscm-equipment-sales/program-options/)).
3. **RadioInspectorRT-Light:** Упрощенная сборка (например, исследованный в репозитории пакет № 32314 версии `7.6.0.1`, исполняемый файл `InspectorRT.exe` размером 13,4 МБ, описанный в [docs/capabilities-light-32314.md](https://github.com/qlolp/openradioinspector/blob/main/docs/capabilities-light-32314.md)), предназначенная исключительно для управления связными сканирующими приемниками (AOR, Icom) без поддержки анализаторов спектра ([RadioInspectorRT-Light](https://www.inspectorsoft.ru/radioinspectorrt-light)).
4. **Сопутствующие автономные программы:** Комплекс дополняется программой постобработки баз данных панорам `RadioInspectorRP` ([RadioInspectorRP](https://www.inspectorsoft.ru/radioinspectorrp)), средой глубокого анализа и демодуляции записанных IQ-файлов `IQ_ProcessPRO` ([IQ Process Pro](https://waypointcs.ca/tscm-equipment-sales/iq-process-pro/)), программой централизованного мониторинга удаленных постов `I_RemoteCheck` ([RadioInspector Features](https://waypointcs.ca/tscm-equipment-sales/features/)), модулем анализа беспроводных сетей `RadioInspectorWiFi` ([RadioInspector](https://www.radioinspector.com/)) и утилитами `DBConverterCSV` и `ISBDViewer`.

```svg
<svg viewBox="0 0 680 240" xmlns="http://www.w3.org/2000/svg">
  <style>
    :root {
      --lumi-ink: light-dark(#000000, #e0e0e0);
      --lumi-muted: light-dark(#5f6368, #9aa0a6);
      --lumi-dim: light-dark(#f2f0f0, #242528);
      --lumi-container: light-dark(#e8e6e6, #2d2f33);
      --lumi-outline: light-dark(#727676, #9a9b9c);
      --lumi-accent-bg: light-dark(#dcf1ff, #192967);
      --lumi-primary: light-dark(#3186ff, #9dd2ff);
      --lumi-sans: 'Google Sans Flex', var(--font-family, 'Google Sans', sans-serif);
      --lumi-mono: 'Google Sans Code', ui-monospace, monospace;
    }
    text { font-weight: 400; stroke: none; }
    .t-l1 { font-family: var(--lumi-sans); font-size: 15px; fill: var(--lumi-ink); }
    .t-l2 { font-family: var(--lumi-mono); font-size: 13px; font-style: italic; fill: var(--lumi-ink); }
    .t-l4 { font-family: var(--lumi-sans); font-size: 13px; fill: var(--lumi-muted); }
    .node-step { fill: var(--lumi-dim); stroke: none; }
    .node-focal { fill: var(--lumi-accent-bg); stroke: var(--lumi-primary); stroke-width: 1.25; }
    .node-store { fill: var(--lumi-container); stroke: var(--lumi-outline); stroke-width: 1; }
    .edge-seg { fill: none; stroke: var(--lumi-ink); stroke-width: 1.3; stroke-linecap: round; stroke-dasharray: 0 4; }
    .edge-arrow { fill: none; stroke: var(--lumi-ink); stroke-width: 1.3; stroke-linecap: round; stroke-dasharray: 0 4; marker-end: url(#lumi-caret-1); }
  </style>
  <defs>
    <marker id="lumi-caret-1" viewBox="0 0 10 10" refX="7.5" refY="5" markerUnits="userSpaceOnUse" markerWidth="10" markerHeight="10" orient="auto">
      <path d="M 3 1.2 L 7.5 5 L 3 8.8" fill="none" stroke="var(--lumi-ink)" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" />
    </marker>
  </defs>

  <rect class="node-step" x="20" y="24" width="184" height="80" rx="40" />
  <text class="t-l1" x="112" y="55" text-anchor="middle" dominant-baseline="central">Драйверы приборов</text>
  <text class="t-l2" x="112" y="77" text-anchor="middle" dominant-baseline="central">SCPI / Soapy / tinySA</text>

  <line class="edge-arrow" x1="212" y1="64" x2="242" y2="64" />

  <rect class="node-focal" x="250" y="24" width="180" height="80" rx="40" />
  <text class="t-l1" x="340" y="55" text-anchor="middle" dominant-baseline="central">Ядро Python Core</text>
  <text class="t-l4" x="340" y="77" text-anchor="middle" dominant-baseline="central">Потоки и детектор</text>

  <line class="edge-arrow" x1="438" y1="64" x2="468" y2="64" />

  <rect class="node-store" x="476" y="24" width="184" height="80" rx="40" />
  <text class="t-l1" x="568" y="55" text-anchor="middle" dominant-baseline="central">Архив SQLite v6</text>
  <text class="t-l2" x="568" y="77" text-anchor="middle" dominant-baseline="central">CEF / SigMF / CSV</text>

  <path class="edge-arrow" d="M 340 112 L 340 128 Q 340 140 324 140 L 128 140 Q 112 140 112 148 L 112 152" />
  <line class="edge-arrow" x1="340" y1="112" x2="340" y2="152" />
  <path class="edge-arrow" d="M 340 112 L 340 128 Q 340 140 356 140 L 552 140 Q 568 140 568 148 L 568 152" />

  <rect class="node-step" x="20" y="160" width="184" height="64" rx="32" />
  <text class="t-l1" x="112" y="183" text-anchor="middle" dominant-baseline="central">WebUI (Raspberry Pi)</text>
  <text class="t-l2" x="112" y="203" text-anchor="middle" dominant-baseline="central">/platform + SSE</text>

  <rect class="node-step" x="250" y="160" width="180" height="64" rx="32" />
  <text class="t-l1" x="340" y="183" text-anchor="middle" dominant-baseline="central">macOS Клиент</text>
  <text class="t-l2" x="340" y="203" text-anchor="middle" dominant-baseline="central">SwiftUI (/api/v1)</text>

  <rect class="node-step" x="476" y="160" width="184" height="64" rx="32" />
  <text class="t-l1" x="568" y="183" text-anchor="middle" dominant-baseline="central">Windows Клиент</text>
  <text class="t-l2" x="568" y="203" text-anchor="middle" dominant-baseline="central">Tkinter (/api/v1)</text>
</svg>
```

### Архитектура открытой платформы OpenRadioInspector

В отличие от монолитной Windows-архитектуры оригинала, разделенной на полдюжины платных исполняемых файлов и защищенной USB-ключами Guardant (`GrdVkc32.dll`), **OpenRadioInspector** спроектирован вокруг единого серверного ядра на языке Python (59 модулей верхнего уровня и 6 специализированных пакетов: `devices`, `export`, `monitoring`, `tinysa`, `web`, `windows`), к которому по унифицированному протоколу HTTP REST и Server-Sent Events (`/api/v1/*`) подключаются три равноправных графических клиента ([AGENTS.md](https://github.com/qlolp/openradioinspector/blob/main/AGENTS.md), [docs/architecture.md](https://github.com/qlolp/openradioinspector/blob/main/docs/architecture.md)):

* **Встроенная веб-панель (Raspberry Pi WebUI):** доступна по маршруту `/platform` для автономных одноплатных постов радиоконтроля.
* **Нативный клиент macOS:** приложение на Swift / SwiftUI в каталоге `macos/`.
* **Настольный клиент Windows:** тонкий клиент на базе Python `tkinter` в пакете `radioinspector/windows/`.

Вместо восьми разрозненных баз данных FireBird (`.IFIXDB`, `.INRMS`, `.IMEAS`, `.ITASKDB` и др.) в OpenRadioInspector применяется единый транзакционный архив сеансов **SQLite** (схема версии **6**) с дисциплиной «один писатель» (`session_store.py`, `monitoring/store.py`), где все частоты строго нормализованы в целых герцах (`int Hz`), временные метки хранятся в формате ISO UTC, а некалиброванные аппаратные отсчеты (например, сырые значения S-метра приемника AOR AR-DV1) сохраняются без фиктивного пересчета в дБм до применения калибровочного профиля ([AGENTS.md](https://github.com/qlolp/openradioinspector/blob/main/AGENTS.md)).

| Критерий сравнения | Коммерческий комплекс RadioInspectorRT | Открытая платформа OpenRadioInspector (`v0.4.0.dev0`) |
| --- | --- | --- |
| **Разработчик и лицензия** | ООО «Лаборатория радиоизмерений», проприетарная коммерческая лицензия ([radioinspector.com](https://www.radioinspector.com/)) | Открытый проект в репозитории [qlolp/openradioinspector](https://github.com/qlolp/openradioinspector), лицензия **MIT** |
| **Защита и привязка** | Аппаратный USB-ключ Guardant (`GrdVkc32.dll`), привязка не более чем к **трём** заранее согласованным моделям приборов (базовая поставка — одна; `RT2`/`RT3` — две/три) ([RadioInspectorRT](https://www.inspectorsoft.ru/radioinspectorrt)) | Полное отсутствие аппаратных ключей и ограничений на число запускаемых копий или подключаемых приборов ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) |
| **Операционные системы** | Только Microsoft Windows 7 / 8 / 8.1 / 10 / 11 ([Receiver Support](https://waypointcs.ca/tscm-equipment-sales/receiver-support/)) | Кроссплатформенно: Linux (включая Raspberry Pi OS), macOS (SwiftUI), Windows (Tkinter + WebUI) ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) |
| **Технологический стек** | Delphi / VCL (133 экранные формы RCDATA DFM в `InspectorRT.exe`), `libfftw3f-3.dll`, `FreeImage.dll`, `WavContr.dll` ([docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)) | Python, Swift, JavaScript/HTML/CSS, `numpy`, встроенный HTTP/SSE сервер ([openradioinspector](https://github.com/qlolp/openradioinspector)) |
| **Архитектура хранения данных** | СУБД FireBird 2.5 (8 отдельных файлов баз данных с закрытыми структурами `.IFIXDB`, `.IMEAS`, `.ITASKDB`) и бинарные архивы панорам ([docs/capabilities-light-32314.md](https://github.com/qlolp/openradioinspector/blob/main/docs/capabilities-light-32314.md)) | Единая открытая БД **SQLite** (схема **v6**) + прямой экспорт в международные стандарты **ITU-R SM.1809 CEF**, **SigMF 1.2.0** и **CSV** ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) |
| **Сетевое взаимодействие** | Проприетарный обмен по TCP-портам `47382`, `47383`, `47384` через отдельные платные программы `I_MasterDevice`, `I_RemoteCheck` и опцию `RT-Group` ([RadioInspector](https://waypointcs.ca/tscm-equipment-sales/radio-inspector/)) | Встроенный REST API (`/api/*` и `/api/v1/*`) с потоковой передачей событий через Server-Sent Events (`/api/v1/events`) без внешних утилит ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) |
| **Метрологический статус** | Свидетельство о метрологической аттестации ПО № 34/2-2008 (МИ 2517-99) для измерительных версий ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) | Явный дисклеймер: не является утвержденным типом СИ; реализует математические методики ITU-R SM.328-10, SM.1880, ГОСТ 30318-95 и Норм ГКРЧ 19-21 ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) |

> **Важно (замечание ревью).** Сетевое преимущество «из коробки» пока ограничено отсутствием аутентификации API: ядро не проверяет `Authorization`/токены, поэтому при `--host 0.0.0.0` или через DNS-rebinding любой хост в сети может управлять приборами и читать архив. Это блокер для позиционирования «удалённый пост», требующий устранения до широкого развёртывания (см. [web-security-ревью](reviews/2026-11-09/web-security.md)).

---

## Интеграция с аппаратным обеспечением (более 200 моделей против универсальных транспортов)

Одним из главных маркетинговых и технических преимуществ коммерческого комплекса **RadioInspectorRT** является поддержка **более 180–200 моделей** профессиональных анализаторов спектра, измерительных приемников, связных сканеров и SDR-трансиверов от ведущих мировых производителей, а также более 15 векторных анализаторов сигналов ([RadioInspector](https://www.radioinspector.com/), [Receiver Support](https://waypointcs.ca/tscm-equipment-sales/receiver-support/)). В оглавлении «Книги 6» руководства по эксплуатации коммерческого пакета описано 36 специализированных разделов подключения аппаратуры (плюс контроллеры NI GPIB и внешние коммутаторы/конвертеры), где для каждого прибора жестко закодированы в библиотеке `Idevim.dll` (2,2 МБ) и драйвере `rsib32.dll` (86 КБ) его специфические команды, таймауты, форматы бинарных трасс, полосы ПЧ и аппаратные поправки ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md), [docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)).

В то же время коммерческая модель лицензирования `RadioInspectorRT` накладывает жесткое эксплуатационное ограничение: базовая поставка позволяет управлять **одной** заранее согласованной моделью, для двух-трёх приборов требуются платные опции `RT2`/`RT3`, а изменить модель после поставки без переоформления лицензии невозможно ([описание «РадиоСофт»](https://www.inspectorsoft.ru/radioinspectorrt)).

В **OpenRadioInspector** задача совместимости с оборудованием решена архитектурно иначе ([docs/devices.md](https://github.com/qlolp/openradioinspector/blob/main/docs/devices.md)). Вместо написания сотен закрытых бинарных оберток под каждую модификацию прибора созданы **6 универсальных транспортных уровней** в пакете `radioinspector/devices/` и специализированные аппаратные профили для наиболее востребованных портативных приборов (в частности, **tinySA / tinySA Ultra ZS405** и связного приемника **AOR AR-DV1**), причем каждый прибор обслуживается собственным изолированным рабочим потоком (`worker thread`) с индивидуальной блокировкой (`lock`), что позволяет одновременно сканировать спектр на анализаторе и перестраивать приемник расследования без взаимных блокировок ([AGENTS.md](https://github.com/qlolp/openradioinspector/blob/main/AGENTS.md)).

| Семейство оборудования | Поддержка в коммерческом RadioInspectorRT | Поддержка в OpenRadioInspector (`radioinspector/devices/`) | Оценка паритета и нюансы |
| --- | --- | --- | --- |
| **Лабораторные и портативные анализаторы спектра (Keysight / Agilent / HP, Rohde & Schwarz, Anritsu, Tektronix, Advantest, Aeroflex/IFR, Ceyear)** | Прямые индивидуальные драйверы для >120 моделей: R&S FSW, FSV, FSU, FSQ, FSP, FSL, FSH, PR100, PR200, EM100, EB200, EB500, ESMD, ESIB, ETL; Keysight/Agilent E44xx, N9000A–N9030A (CXA/EXA/MXA/PXA), N9320B/40B, FieldFox N99xx; Anritsu MS271x/272x, MS27102A; Tektronix RSA306B/500/600, USG ([Receiver Support](https://waypointcs.ca/tscm-equipment-sales/receiver-support/)) | Универсальные драйверы **SCPI по TCP/IP** (порт 5025, `devices/scpi.py`), **VXI-11 / LXI** (`Vxi11Instrument`) и **SCPI через RS-232/USB** (`devices/serial_scpi.py`). Реализованы `*IDN?`, `FREQ:CENT`, `FREQ:SPAN`, `BAND`, `TRAC? TRACE1` и парсинг кривых (`CURVE`, `TRACE`) ([docs/devices.md](https://github.com/qlolp/openradioinspector/blob/main/docs/devices.md)) | **Частичный паритет (протокольный):** базовый съем трасс работает с любым SCPI/VXI-11 анализатором, но нет реестра приборных особенностей (*quirks*) и бинарных форматов `REAL,32`/`INT,32`; совместимость по конкретной модели не подтверждена ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) |
| **Связные и сканирующие приемники (AOR, Icom, Narda, БЕЛАН)** | Встроенные классы `UCScaner_*` для AOR AR-DV1, AR5001D, AR6000, AR5000, AR8200/8600, AR3000A, SR2000/2200, Alpha; Icom IC-R10, R20, R8500, R9500, PCR1000/1500/2500; Narda NRA-3000/6000, IDA-3106; СК4-БЕЛАН ([docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)) | Выделенный серийный драйвер и аппаратный профиль для **AOR AR-DV1** (`ardv1-serial`, `ardv1-sim`) с поддержкой перестройки, демодуляции и чтения сырого S-метра без искажений; Narda/SCPI-приемники — через общий SCPI-транспорт ([docs/devices.md](https://github.com/qlolp/openradioinspector/blob/main/docs/devices.md)) | **Точечный паритет по AOR AR-DV1** (программный контур; аппаратная приёмка не выполнена — `hardware_validation=not_run`); устаревшие аналоговые сканеры 1990–2000-х (AR3000, PCR1000, IC-R10) не портировались |
| **Малогабаритные и бюджетные анализаторы спектра (tinySA / tinySA Ultra)** | **Отсутствует:** коммерческий пакет ориентирован на дорогостоящий сегмент и не поддерживает семейство tinySA | **Преимущество OpenRadioInspector:** пакет `radioinspector/tinysa/` и профиль `tinysa_profile.py` (включая **tinySA Ultra ZS405**) со step-scan, zero-span, SD-картой и режимом LISTEN ([docs/roadmap.md](https://github.com/qlolp/openradioinspector/blob/main/docs/roadmap.md)) | **Превосходство открытой версии** для полевых и бюджетных развертываний |
| **Программно-определяемые радиосистемы (SDR) и USB-анализаторы** | Поддержка USRP B200 / B210, Signal Hound (SA44B, SA124B, BB60A/C/D, SM200A/B/C), ThinkRF R5500 ([Receiver Support](https://waypointcs.ca/tscm-equipment-sales/receiver-support/)) | Универсальный слой **SoapySDR** (`devices/soapy.py`, охватывающий HackRF, Airspy, LimeSDR, BladeRF, USRP, PlutoSDR) и драйвер **RTL-SDR / `rtl_tcp`** (`devices/rtl.py`), жизненный цикл в `docs/SOAPY_LIFECYCLE.md` ([docs/devices.md](https://github.com/qlolp/openradioinspector/blob/main/docs/devices.md)) | **Расширение через SoapySDR/`rtl_tcp`:** более широкий парк массовых SDR; калиброванный sweep не реализован, Signal Hound API напрямую не интегрирован |
| **Специализированные комплексы («Кассандра» и антенные коммутаторы)** | Полная нативная интеграция с комплексами «Кассандра» (ТМ9, ТМ30, С6, С9, С30, МК, СО), антенными коммутаторами и компасом RS CM03/USB ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) | Модуль антенного компаса (`antenna_compass`) реализован; аппаратное управление «Кассандрой» и физическим антенным коммутатором отсутствует ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) | Метод разности панорам с физическим антенным коммутатором не реализован (средний приоритет) |
| **Синтетический генератор / Симулятор** | Отсутствует в виде открытого программного генератора для CI/CD (только воспроизведение ранее записанных баз) | **Преимущество OpenRadioInspector:** детерминированный генератор (`devices/simulator.py`, профили `tinysa-sim`, `ardv1-sim`) для сквозных тестов детекторов и UI без железа ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) | Обеспечивает сборку 1737 автотестов в репозитории |

---

## Спектральный анализ 24/7, пороговые алгоритмы и обнаружение сигналов

Ядром как коммерческого **RadioInspectorRT**, так и **OpenRadioInspector** является непрерывное панорамное сканирование частотных диапазонов с сохранением каждой развертки (свипа) в базу данных, отображением огибающих и автоматическим выделением сигналов, превысивших заданные критерии ([RadioInspector Features](https://waypointcs.ca/tscm-equipment-sales/features/)).

В документации коммерческого комплекса определены три классических метода поиска несанкционированных радиоизлучений:

1. **Метод сравнения с порогом** (горизонтальная/наклонная линия порога, адаптивный порог, три линии порога, табличная маска спектра).
2. **Метод сравнения с эталонной панорамой** (вычитание сохраненного или усредненного «чистого» спектра из текущего).
3. **Метод разности панорам** (дифференциальное сравнение спектров с двух разнесенных антенн через автоматический антенный коммутатор).

| Подсистема спектрального контроля | Реализация в RadioInspectorRT | Реализация в OpenRadioInspector | Статус |
| --- | --- | --- | --- |
| **Панорамы и накопление статистики** | Текущая, Max Hold, Min Hold и Average панорамы в реальном времени; непрерывная запись в БД ([Cassandra M](https://beltim.by/en/catalog/product/kompleks-radiomonitoringa-kassandra-m/)) | `panorama.py`, `trace.py`, `sweep_average.py`, `session_store.py`: текущие/мин/макс/усреднённые трассы, потоковая запись в SQLite с UTC и геопривязкой, лимит архива (`docs/archive-capacity.md`) | **Полный паритет** |
| **Спектрограмма (2D / 3D Waterfall)** | 2D-водопад + **3D-спектрограмма** (поверхность амплитуда-частота-время) ([RadioInspector](https://waypointcs.ca/tscm-equipment-sales/radio-inspector/)) | 2D-водопад реализован (WebUI, клиенты, архивный поток); **3D отсутствует** ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) | **Паритет по 2D; отсутствие 3D** |
| **Управляемая и адаптивная линия порога** | Горизонтальная/ломаная линия + адаптивный (динамический) порог, огибающий шумовую дорожку ([RadioInspectorRT](https://www.inspectorsoft.ru/radioinspectorrt)) | `threshold.py`, `sweep_threshold.py`: горизонтальные/наклонные линии; адаптивный порог как **первый срез** — локальная медиана (окно 3–101) + запас 0–120 дБ; огибающая минимума оригинала не воспроизведена ([docs/adaptive-sweep-threshold.md](https://github.com/qlolp/openradioinspector/blob/main/docs/adaptive-sweep-threshold.md)) | **Первый срез** (не полный паритет) |
| **Три линии порога и маска спектра** | До трёх независимых порогов и табличные маски ([docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)) | `sweep_three_thresholds.py` (три горизонтальные линии на свипе), `spectral_mask.py` (JSON-маска 2–128 точек с интерполяцией) | **Первый срез** (read-only, конфигурация не сохраняется) |
| **Эталонная панорама и разность панорам** | Эталонный спектр (в т.ч. усреднением) и вычитание трасс; работа с коммутатором ([RadioInspector Features](https://waypointcs.ca/tscm-equipment-sales/features/)) | `sweep_references.py` (усреднение 2–16 свипов), `sweep_comparison.py` (Δ дБ), `monitoring/detector.py` (появление/исчезновение с контролем сетки) | **Первый срез по математике**; аппаратный коммутатор отсутствует |
| **Реестр сигналов и сопоставление** | Список сигналов (`ISBDViewer`), фильтр по минимальной мощности, объединение соседних отсчетов ([RadioInspector Features](https://waypointcs.ca/tscm-equipment-sales/features/)) | `signals.py`, `signal_list.py` (правило близости 5×, отсечка по мощности, сверка с БД присвоений 4×, диагностика изрезанности) | **Паритет + расширение** |
| **Окно сканирований до/после ($N_1/N_2$)** | Сохранение $N_1$ до и $N_2$ после для анализа пакетных передач ([docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)) | read-only срез: окно соседей 0–16 до/после (до 33 записей) + округление частот по профилю ([docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)) | **Частичный** (аналитический срез, не автоматический захват) |
| **Занятость полосы и каналов (Occupancy)** | Процент занятости каналов/полос по времени, почасовые графики ([RadioInspectorRP](https://www.inspectorsoft.ru/radioinspectorrp)) | `occupancy.py`, `sweep_occupancy.py`, `occupancy_export.py`: занятость, почасовые графики, эффективность, ON/OFF-длительности, панели Windows/macOS ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) | **Полный паритет** |

---

## Измерение параметров ВЧ-излучений и проверка на соответствие нормам

В коммерческом комплексе радиоизмерительная подсистема (`RT-Meas`, `RC-Meas`, `RadioInspectorRP`) отвечает за оценку занимаемой ширины полосы, центральной частоты, напряженности поля, отношения сигнал/шум, глубины АМ, девиации ЧМ и параметров DVB-T/T2 ([RF Signal Analysis](https://waypointcs.ca/tscm-equipment-sales/advanced-rf-signal-analysis-and-performance-measurement-subsystem/)). В базе `IMEASDB_EMPTY.IMEAS` под каждый тип измерения выделена отдельная таблица (`MEAS_B2_RESULT`, `MEAS_N1902_RESULTCBW`, `MEAS_MEASE_RESULT`, `MEAS_DVB_RESULTCODE` и др., см. [docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)).

В **OpenRadioInspector** модуль `radioinspector/measurements.py` и смежные `sweep_quality.py`, `measurement_results.py` реализуют математическую часть этих стандартов с возвратом предупреждений о применимости методики. **При этом независимое ревью выявило подтверждённые дефекты корректности, поэтому статус «паритет» следует читать как «алгоритм реализован, но требует исправления» там, где помечено.**

* **Ширина полосы по методу β/2 (ITU-R SM.328-10):** `occupied_bandwidth_beta2` интегрирует плотность мощности и находит границы, за которыми остаётся по β/2 % полной мощности. Алгоритм реализован, но при наличии `NaN` во входной трассе молча возвращает `bandwidth_hz = nan` без диагностики — требуется валидация входов ([measurements.py](https://github.com/qlolp/openradioinspector/blob/main/radioinspector/measurements.py)).
* **Ширина по уровню −N дБ и контрольная −30 дБ:** `bandwidth_at_level` и `control_bandwidth` реализуют ГОСТ 30318-95 / Нормы 19-02/19-21. Есть непокрытый краевой случай: переход через порог на граничной точке полосы обзора не находится, полоса завышается и выдаётся ложное предупреждение ([measurements.py:299-336](https://github.com/qlolp/openradioinspector/blob/main/radioinspector/measurements.py)).
* **Оценка центральной частоты:** реализованы `center_frequency_centroid` и `complex_center_frequency`, а также программный частотомер (пик в окне с границей погрешности). **Паритет** по наличию методик.
* **Напряжённость поля и конвертация единиц:** ⚠️ **требует исправления.** `field_strength()` в текущем коде возвращает `max(уровней) + calibration` с единицей `"dB"` (не дБ(мкВ/м)); методики E/U в `docs/measurement-methods.md` значатся нереализованными. `units.convert` содержит ошибку dBmV (без множителя 1e-3, т.е. 60 дБ) и падает `UnitError` на парах dBmV↔dBuV и dBuV/m↔dBV/m ([units.py:132-137,212-223](https://github.com/qlolp/openradioinspector/blob/main/radioinspector/units.py), [measurements.py:535-606](https://github.com/qlolp/openradioinspector/blob/main/radioinspector/measurements.py)). В оригинале к тому же есть графический конструктор измерительной схемы с базой паспортов антенн/кабелей — его в открытой версии нет.
* **Параметры DVB-T:** реализована сверка введённых MER/BER (до/после Витерби и Рида-Соломона) с порогами (**паритет по валидации метрик**); определение класса аналоговых ТВ (PAL/SECAM/NTSC с видеокадром) не реализовано.
* **Поиск интермодуляционных помех и гармоник:** реализован в обеих системах ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)).

---

## Цифровая обработка сигналов (DTest, SCARS, IQ_ProcessPRO) — главное функциональное расхождение

Наибольший технологический разрыв между **RadioInspectorRT** (максимальная комплектация) и **OpenRadioInspector** лежит в области потоковой демодуляции и декодирования цифровых протоколов.

В коммерческом `RadioInspectorRT` за этот контур отвечают платная опция **DTest** ([Digital Signal Analysis](https://waypointcs.ca/tscm-equipment-sales/digital-signal-analysis/), [DTest](https://www.inspectorsoft.ru/dtest)), подсистема классификации **SCARS** и программа отложенного анализа **IQ_ProcessPRO** ([IQ Process Pro](https://waypointcs.ca/tscm-equipment-sales/iq-process-pro/)). При наличии прибора с потоковой выдачей IQ коммерческий пакет выполняет:

1. **Программную демодуляцию и декодирование цифровых стандартов:**
* **Сотовая связь:** GSM (2G), UMTS (3G), LTE (4G), 5G NR — топология сети, идентификаторы базовых станций (MCC, MNC, LAC, CellID), выявление ложных БС (IMSI-catcher).
* **Транкинг и PMR/LMR:** TETRA (4 слота, предупреждение `TETRA РЕЖИМ DMO!!!` при связи трубка-трубка в обход БС), APCO-P25, DMR/MotoTRBO (2 слота), dPMR, NXDN — ID источника/назначения, Color Code, голосовая демодуляция незашифрованных переговоров ([Digital Signal Analysis](https://waypointcs.ca/tscm-equipment-sales/digital-signal-analysis/)).
* **Беспроводные протоколы:** DECT (RFP/PP, сверка с «белым списком»), Bluetooth (MAC/LAP на нестандартной сетке), IEEE 802.15.4 (ZigBee, ISA100.11a, WirelessHART, MiWi).
* **Телевещание:** DVB-T, DVB-T2 и аналоговое ТВ (PAL, SECAM, NTSC) с выводом изображения.

2. **Постобработку IQ (`IQ_ProcessPRO`):** сдвиг частоты, цифровая фильтрация, передискретизация, децимация, оконное взвешивание, созвездия, пакетный прогон через декодеры `DTest`.

В **OpenRadioInspector** (`radioinspector/iq.py`) авторы прозрачно зафиксировали, что проприетарные декодеры `DTest` не воспроизводятся. Вместо этого реализованы:

* **Калькулятор бюджета IQ (`iq.py`, CLI `iq`):** расчёт минимальной $F_d$, длительности $L$ и объёма $M = F_d \times L$ для DECT/GSM/TETRA/APCO-25/DMR и проверка возможностей прибора ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)). Это **планировщик, а не анализатор**: сама запись IQ и декодеры отсутствуют.
* **Экспорт в SigMF 1.2.0 (`export/sigmf.py`):** сохранение метаданных/записей в открытом формате для GNU Radio, Inspectrum, URH, SDR++.
* **Аппаратный декодер через AOR AR-DV1:** делегирование демодуляции самому приёмнику при handoff ([docs/roadmap.md](https://github.com/qlolp/openradioinspector/blob/main/docs/roadmap.md)). Цифровые режимы AR-DV1 в текущем serial-срезе **не отправляются** (FM/AM только).
* **Wi-Fi (802.11):** модуль `wifi.py` (разбор кадров 802.11, оценка загрузки, инвентаризация точек доступа) присутствует, но **не подключён** к CLI/API и не покрыт тестами — заявлять его «реализованным» преждевременно ([wifi.py](https://github.com/qlolp/openradioinspector/blob/main/radioinspector/wifi.py)).

---

## Аудиоконтроль, запись фонограмм и двухприемниковая схема (Handoff)

1. **Запись фонограмм с предысторией (5-секундный буфер):** фирменная особенность `RadioInspectorRT` — запись демодулированного аудио с 5-с предысторией и НЧ-осциллограф. В **OpenRadioInspector** это воспроизведено частично: модули `phonogram.py`, `audio.py`, `audio_stream.py` поддерживают запись с предысторией и PCM-доставку, но **редактирование и автостарт не реализованы** ([docs/audio-stream.md](https://github.com/qlolp/openradioinspector/blob/main/docs/audio-stream.md)) — статус «первый срез», не «полный паритет».
2. **Аудиоконтроль вторым приёмником и Handoff:** в коммерческом RT опция `RT-SoundScaner` передаёт частоту обнаруженного сигнала на второй приёмник без остановки обзора. В OpenRadioInspector на уровне ядра `/api/v1` реализован конвейер **`automatic-handoff`** (кандидат → очередь → перестройка `AOR AR-DV1`), с независимыми opt-in политиками detection/handoff/completion ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)). Программный контур есть, но аппаратная приёмка двухприборной схемы не выполнена (`hardware_validation=not_run`).
3. **Передача демодулированного звука по сети (RemoteMD):** в коммерческом комплексе — связка `I_MasterDevice` + `RT-RemoteMD`. В OpenRadioInspector **PCM-транспорт реализован** (`audio_stream.py`: 16-бит LE mono, до 4 подписчиков, ограниченные очереди, отбрасывание отстающих); сквозной аппаратный аудиотракт и полный RemoteMD не завершены.

---

## Геоинформационная система (ГИС), пеленгование и планировщик задач

| Функциональная область | RadioInspectorRT (+ RP / Geo / AdvancedDB) | OpenRadioInspector (`v0.4.0.dev0`) | Вывод |
| --- | --- | --- | --- |
| **ГИС и карты покрытия** | Опции `RT-Geo`/`RP-Geo`: привязка к GPS, оффлайн-карты OSM и MapInfo, зоны радиопокрытия ([Program Options](https://waypointcs.ca/tscm-equipment-sales/program-options/)) | `geoloc.py`, `measurement_map.py`, CLI `coverage`, `/api/coverage`: привязка к координатам, карты покрытия по дуге большого круга, экспорт **GeoJSON** ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md)) | **Паритет по расчётному ядру**; интерактивный слой пеленгов на карте — план |
| **Амплитудное пеленгование и триангуляция** | Направленная антенна + компас `RS CM03/USB`, учёт магнитного склонения, пересечение пеленгов ([RadioInspector](https://www.radioinspector.com/)) | `geoloc.py`, CLI `df`, `antenna_compass`: расчёт пеленга, триангуляция, антенный компас | **Полный паритет** |
| **Планировщик задач и автоматический мониторинг** | `RT-AdvancedDB`, таблицы `TASKPLAN` (~40 колонок) и `FREQS` ([docs/capabilities-light-32314.md](https://github.com/qlolp/openradioinspector/blob/main/docs/capabilities-light-32314.md)) | `tasks.py`, `task_scheduler.py`, `monitoring/`: модель `TASKPLAN` (6 статусов, профили фиксированных частот 7–11 точек, пороги 6/20/30 дБ), `/api/v1/task-scheduler` | **Полный паритет** |
| **Генерация отчётов и протоколов** | Генератор Word/HTML по шаблонам с тегами ([RadioInspector Features](https://waypointcs.ca/tscm-equipment-sales/features/)) | `reporting.py`, `docx_report.py`, `keywords.py`: `.docx`/текстовые протоколы, 317 ключевых слов (`#Имя#`, `#D1#…#3D#`) | **Паритет по шаблонам** |
| **Групповой/удалённый мониторинг (`RT-Group`, `I_RemoteCheck`)** | `RT-Group` — сравнение спектров нескольких копий в реальном времени; `I_RemoteCheck` — статусы/тревоги на поэтажном плане ([Program Options](https://waypointcs.ca/tscm-equipment-sales/program-options/)) | Многоклиентская архитектура + SSE-тревоги (`/api/v1/events`), `monitoring/guard.py` | **Частичный:** SSE-тревоги есть, но нет плана помещения и синхронного мульти-постового сравнения |

---

## Стратегическое резюме: сильные стороны, ограничения и вектор развития OpenRadioInspector

Проект **OpenRadioInspector** ([qlolp/openradioinspector](https://github.com/qlolp/openradioinspector)) вышел за рамки прототипа и достиг **функционального паритета с базовым ядром RadioInspectorRT** в большинстве математических дисциплин — **с двумя оговорками**: (1) часть недавних возможностей существует как read-only «первые срезы», а не полноценные подсистемы; (2) независимое ревью подтвердило ряд дефектов корректности измерений, которые надо закрыть, прежде чем считать паритет полноценным.

1. **Где OpenRadioInspector уже равен или превосходит:**
* **Математика спектрального контроля и измерений:** панорамы (текущая/мин/макс/усреднённая), управляемые и наклонные пороги, тройные пороги и маски спектра, эталонные панорамы, правило группировки 5×/4×, β/2 (ITU-R SM.328-10), −N дБ/−30 дБ (ГОСТ 30318-95 / Нормы 19-21), центр тяжести, занятость полосы, амплитудное пеленгование, планировщик `TASKPLAN`, отчёты на 317 тегов ([README.md](https://github.com/qlolp/openradioinspector/blob/main/README.md), [docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)).
* **Современная архитектура и открытость:** Linux/Pi + macOS (SwiftUI) + Windows, REST/SSE API, изолированные потоки приборов, автоматический handoff tinySA → AR-DV1, детерминированный симулятор (1737 тестов), экспорт в **SQLite, CEF (SM.1809), SigMF 1.2.0, GeoJSON** ([AGENTS.md](https://github.com/qlolp/openradioinspector/blob/main/AGENTS.md)).

2. **Где сохраняется отставание от полной комплектации RadioInspectorRT:**
* **Глубина аналитических срезов:** окно $N_1/N_2$, округление частот, база результатов с методиками — изолированные read-only срезы, требуют сквозной интеграции в клиенты и фоновый цикл.
* **Потоковый цифровой декодинг (`DTest`/`SCARS`/`IQ_ProcessPRO`):** есть расчёт бюджета IQ и экспорт SigMF, но нет записи IQ и программных демодуляторов (GSM/LTE/5G, TETRA DMO, DMR, APCO-25, DECT, DVB-T2, аналоговое ТВ).
* **Индивидуальные аппаратные профили (*quirks*) и бинарный транспорт:** универсальные SCPI/VXI-11/SoapySDR закрывают базовый съем, но для R&S/Keysight/Anritsu на скоростях >1 ГГц/с нужен `REAL,32`/`INT,32` вместо медленного ASCII `CURVE`.
* **Специфические TSCM-функции:** разность панорам с физическим антенным коммутатором, полный RemoteMD, 3D-водопад, управление средствами РЭП.

### Рекомендуемые технические шаги

1. **Запись IQ в SigMF + открытые декодеры:** дополнить `iq.py` и `devices/soapy.py` реальным сбросом IQ в `.sigmf-data`/`.sigmf-meta` по триггеру детектора и подключить через плагин-интерфейс открытые CLI-декодеры (`dsd-fme` для DMR/TETRA/P25, `rtl_433` для ISM-телеметрии) без нарушения MIT.
2. **Бинарный SCPI (`FORMat REAL,32`) и реестр приборных профилей:** поддержать IEEE 488.2 Definite Length Arbitrary Block (`#N...`) для ускорения передачи панорам 10 000–600 000 точек с R&S/Keysight.
3. **Программный эквивалент антенного коммутатора (`RT-Group` Lite):** режим дифференциального вычитания трасс двух одновременно подключённых приёмников (опорный «улица» минус обследуемый «помещение») для поиска НДП методом разности панорам без физического ВЧ-коммутатора.

> **Приоритет перед перечисленным (из ревью).** Прежде чем наращивать функции, стоит закрыть подтверждённые блокеры: аутентификацию API, исправление единиц/`field_strength`/SCPI-парсинга, вынос `detector_state` из строки project (P1), SSRF-валидацию webhook и стабилизацию тест-сьюта на Windows. Подробности — [`docs/reviews/2026-11-09/README.md`](reviews/2026-11-09/README.md) и v2-отчёт.

---

## Источники и документация

1. [Официальный сайт RadioInspector](https://www.radioinspector.com/)
2. [RadioInspector и подсистема SCARS — Waypoint](https://waypointcs.ca/tscm-equipment-sales/radio-inspector/)
3. [Функциональные возможности RadioInspector — Waypoint](https://waypointcs.ca/tscm-equipment-sales/features/)
4. [Опции RadioInspector RT/RC/RP/RT-Light — Waypoint](https://waypointcs.ca/tscm-equipment-sales/program-options/)
5. [DTest и SCARS — Waypoint](https://waypointcs.ca/tscm-equipment-sales/digital-signal-analysis/)
6. [IQ Process Pro — Waypoint](https://waypointcs.ca/tscm-equipment-sales/iq-process-pro/)
7. [Поддерживаемые приёмники и анализаторы — Waypoint](https://waypointcs.ca/tscm-equipment-sales/receiver-support/)
8. [RT-Group — Waypoint](https://waypointcs.ca/2021/04/26/radioinspector-rt-group-real-time-simultaneous-multiple-receiver-spectrum-trace-display-monitoring-and-comparative-analysis/)
9. [Подсистема измерения параметров ВЧ-сигналов — Waypoint](https://waypointcs.ca/tscm-equipment-sales/advanced-rf-signal-analysis-and-performance-measurement-subsystem/)
10. [Линейка СПО «РадиоИнспектор» — ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspector)
11. [RadioInspectorRT — ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspectorrt)
12. [RadioInspectorRT-Light — ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspectorrt-light)
13. [RadioInspectorRP — ООО «РадиоСофт»](https://www.inspectorsoft.ru/radioinspectorrp)
14. [DTest — ООО «РадиоСофт»](https://www.inspectorsoft.ru/dtest)
15. [«Кассандра-М» и TETRA DMO — «Безопасность бизнеса»](https://www.bugshunt.ru/info/articles/ispolzovanie-vozmozhnostey-kompleksa-radiomonitoringa-i-tsifrovogo-analiza-signalov-kassandra-m-dlya/)
16. [«Кассандра-М» — ЗАО «БЕЛТИМ СБ»](https://beltim.by/en/catalog/product/kompleks-radiomonitoringa-kassandra-m/)
17. [Репозиторий OpenRadioInspector](https://github.com/qlolp/openradioinspector)
18. [docs/rt-comparison.md](https://github.com/qlolp/openradioinspector/blob/main/docs/rt-comparison.md)
19. [docs/capabilities-light-32314.md](https://github.com/qlolp/openradioinspector/blob/main/docs/capabilities-light-32314.md)
20. [docs/commercial-app-inventory.md](https://github.com/qlolp/openradioinspector/blob/main/docs/commercial-app-inventory.md)
21. [docs/devices.md](https://github.com/qlolp/openradioinspector/blob/main/docs/devices.md)
22. [docs/roadmap.md](https://github.com/qlolp/openradioinspector/blob/main/docs/roadmap.md)
23. [docs/reviews/2026-11-09/README.md](https://github.com/qlolp/openradioinspector/blob/main/docs/reviews/2026-11-09/README.md)

---

Документ подготовлен и сверен с помощью [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness):

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
