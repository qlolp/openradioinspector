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
**Сеанс и приборы → Подключить приборы…**.
Select **RF Explorer 3G Combo**. For an automatic survey with 10 kHz between bins:

1. Choose **Автообзор 10 кГц**.
2. Keep **Весь активный вход** to scan the full range reported by the active SMA
   input. To survey **100–400 MHz**, choose **Свои границы** and enter **100**
   and **400**. Both endpoints are included. The core calculates the windows.
3. Enter the RF Explorer port on the computer running the core. For a local core,
   **Выбрать порт → Обновить список** refreshes the Mac's port list after plugging
   in the analyzer. Multiple USB serial names can refer to the same device.
4. Click **Начать автообзор 10 кГц**. Each window is recorded as a separate sweep.
   The core repeats the survey until you click **Завершить приём**.

The spectrum header shows the planned range, pass number and window number,
followed by the frequencies from the selected window. The detector and RBW come
from the instrument readback. **Normal** shows current levels and **Max Hold**
shows retained maxima. A 10 kHz bin spacing can have a different RBW.

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
