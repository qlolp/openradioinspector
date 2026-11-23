# macOS local Swift package

Open `DvoynoyObzor.xcodeproj` to run and sign the macOS app. The project links
`DvoynoyObzorSession` from the local `DvoynoyObzorKit` package at `Package.swift`.

## Code ownership

- `DvoynoyObzor/DvoynoyObzorApp.swift`: the Xcode target's only Swift source;
  delegates to the package's `DvoynoyObzorApplication`.
- `DvoynoyObzor/`: package implementation, including the application scene,
  views, models, transport adapters, and startup self-check. The manifest excludes
  the launcher. Add implementation files here; SwiftPM discovers them automatically.
  Do not add them to the Xcode app target's Compile Sources phase.
- `Tests/`: regression tests for the same module that the app links.
- `SpektrKomandy/`: existing local protocol and contract client package, retained
  as a dependency. Its generated contract dependencies remain in `../contracts/`.

The existing source paths and `DvoynoyObzorSession` module name are retained for
release tooling, tests, and CI. Implementation types remain internal; only the
application type, its initializer, and scene body are public. Xcode still owns
app identity, signing, Info.plist generation, and app bundle packaging.

## RF Explorer 3G Combo

With the bundled Rust development core, first open **Подключение → Локальное
ядро на этом Mac → Запустить локальное ядро**. Once connected, open
**Подключить приборы…** next to the core status.
Select **RF Explorer 3G Combo** to configure an automatic survey with 10 kHz between bins.

1. Choose **Автообзор 10 кГц**.
2. Keep **Весь активный вход** to scan the full range reported by the active SMA
   input. The **80–120 МГц** and **100–400 МГц** buttons fill both inclusive endpoints.
   Under **Свои границы**, enter Start / Stop or Center / Span in MHz. Changing
   input style preserves whole Hz. **Сохранить диапазон** keeps a named preset on
   this Mac. The on-screen plan uses the 112-point survey profile. The committed
   pass reports the actual window count and the instrument readback supplies the
   board, SMA input and module limits.
3. Enter the RF Explorer port on the computer running the core. For a local core,
   **Выбрать порт → Обновить список** refreshes the Mac's port list after plugging
   in the analyzer. Multiple USB serial names can refer to the same device.
4. Click **Начать автообзор 10 кГц**. Each window is recorded as a separate sweep.
   The core repeats the survey until you click **Завершить приём**.

**Текущее окно** shows the individual source sweep, its detector and recorded RBW.
**Весь проход** shows the saved coverage, holes, UTC age, completed duration and
measured revisit interval when the core reports them. Each trace column retains
the source of its strongest measured value. Select **Программный Max Hold** to
overlay compatible passes. **Сбросить Hold** sets the cutoff for this client and
stream. The instrument detector remains visible beside the RBW.

Click a peak and **Открыть исходный свип** for its original point index, integer
Hz, dBm and UTC interval. **Предыдущее окно**, **Следующее окно** and
**Воспроизвести по UTC** navigate the committed originals in their recorded
timing. **Пауза показа** freezes the display while the core keeps acquiring.
**Маркеры** places up to four named frequencies on the graph. When the core
advertises frequency bookmarks, a set can be saved in the session and opened
on another client. Recalling a marker queries its exact frequency, including
when the display column has a stronger neighbouring peak. Read-only clients
can recall saved sets. Saving sets and submitting scientific calculations
require operator access.

**Анализ свипа** and **Анализ исходника** run the analyses offered by the core.
For a tolerance envelope, select an archived reference, set independent upper
and lower deviations in dB, and inspect the classified target bins and gaps.
For band power, enter two measured frequencies of one original window. The
result shows the selected span, source indices, recorded RBW and estimated dBm.
The result identifies the uncalibrated ENBW and reports the available
uncertainty. These workflows have synthetic tests.

For repeated measurements in one window, choose **Окно прибора** to follow the
current instrument settings or **Задать окно** to enter the endpoints in MHz
with whole-kHz precision. Click **Подключить и начать приём**. Both modes use
500,000 baud and require `features.rf_explorer_3g_spectrum`. Automatic surveys
also require `features.rf_explorer_10khz_survey`.

**Наблюдение** shows the spectrum and waterfall in dBm. **Завершить приём**
stops this stream. Open **Архив** in the same session to find and reopen
its recorded sweeps.

Side-by-side development app bundles can set the Info.plist string
`ORIDevelopmentArchiveNamespace` for their bundled Rust core. For example,
`RFExplorerSurvey` selects `~/Library/Application Support/OpenRadioInspector/macOS-Development/Rust-RFExplorerSurvey`.
The default directory is `macOS-Development/Rust`. Use 1–64 ASCII letters,
digits, hyphens or underscores, starting with a letter or digit.

## Validation

Use Swift 6.4 or newer and macOS 14 or newer. The Python interpreter on `PATH`
must have the root project's dependencies installed (`python3 -m pip install ..`
from an activated virtual environment in `macos/`) for the production event fixture.

From `macos/`, run:

```sh
swift test --skip testPhysicalTinySAUltraSmokeIfConnected
swift test --package-path SpektrKomandy
```

The physical tinySA case also requires `ORI_RUN_PHYSICAL_TINYSA_TEST=1` before
it enumerates ports. Use the separate [hardware protocol](../docs/release/hardware-acceptance.md)
with the normal port owner stopped; an ordinary `swift test` must not claim USB.

Build the `DvoynoyObzor` scheme in Xcode to check the launcher-to-package boundary.
In environments that prohibit SwiftPM's nested sandbox, add `--disable-sandbox`
to the test commands. Some integration tests skip when their optional fixtures
are not configured. Hardware validation is separate; the package reorganization
does not establish device compatibility or measurement accuracy.

## Полевой выезд с Android

Дважды нажмите файл `.orifield` в Finder или выберите **Файл → Открыть полевой выезд…**.
Откроется отдельное окно только для чтения. Просмотр работает локально, без запуска службы,
подключения приборов и импорта в рабочую базу. При открытии приложение проверяет CRC,
SHA-256, количество записей, трек и ссылки. Во вкладках доступны исходные свипы,
наблюдения приёмника, точки GPS, заметки и разрывы. Кнопка в заметке ведёт к записанному
источнику. Для просмотра всех отсчётов и настроек раскройте **Исходная запись**.
Учебный файл прилагается к [описанию формата](../docs/field-archive.md).
