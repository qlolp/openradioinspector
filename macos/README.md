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

## Validation

Use Swift 6.4 or newer and macOS 14 or newer. The Python interpreter on `PATH`
must have the root project's dependencies installed (`python3 -m pip install ..`
from an activated virtual environment in `macos/`) for the production event fixture.

From `macos/`, run:

```sh
swift test --skip testPhysicalTinySAUltraSmokeIfConnected
swift test --package-path SpektrKomandy
```

Build the `DvoynoyObzor` scheme in Xcode to check the launcher-to-package boundary.
In environments that prohibit SwiftPM's nested sandbox, add `--disable-sandbox`
to the test commands. Some integration tests skip when their optional fixtures
are not configured. Hardware validation is separate; the package reorganization
does not establish device compatibility or measurement accuracy.
