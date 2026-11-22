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
Select **RF Explorer 3G Combo** and choose its USB serial port on the computer
running the live core. The local port menu lists the Mac's ports. For a remote
core, type the port path on that host. The core must advertise
`features.rf_explorer_3g_spectrum`.

**Подключить и начать приём** creates a spectrum session and starts its
receiver at 500,000 baud. By default, it reads the range from the analyzer.
To request a range on its active module, enable **Задать диапазон при подключении**
and enter both endpoints in MHz with whole-kHz precision. For example,
100 to 101.110 MHz spans 111 intervals of 10 kHz on a 112-point sweep.
The plot shows the spacing from the received frequencies and the RBW, detector
and active SMA input reported for the selected sweep.

**Наблюдение** shows the spectrum and waterfall in dBm. **Завершить приём**
stops this stream. Open **Архив** in the same session to find and reopen
its recorded sweeps.

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
