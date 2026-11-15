# Local core from the native development app

The normal Xcode app can explicitly start the shared compatibility core with its
local-core control. This does not connect receivers, arm monitoring, or enable a
generator. The core exclusively owns hardware and archive writes; the SwiftUI
client connects through `/api/v1/*`.

Personal RC builds continue to use `NativeLocalService` and their supervised
Rust LaunchAgent. `NativeDevelopmentCore` refuses to launch in personal RC,
including when a Python environment is configured. There is no fallback from a
failed RC service to Python.

Discovery first checks the packaged
`Contents/Helpers/openradioinspector-core/openradioinspector-core`. In an Xcode
checkout it uses `.venv/bin/python3` or `.venv/bin/python`. A prepared interpreter
can also be selected explicitly through the UI; a validated selection is saved
in user preferences for subsequent explicit launches. Developers may configure
`ORI_DEVELOPMENT_PYTHON` and `ORI_DEVELOPMENT_CHECKOUT` in the Xcode scheme.
Dependencies must already be installed in the selected environment: the app does
not install packages or search global Python installations. A missing environment
is reported rather than silently launching a different backend.

The development archive is separate from personal RC and command-line defaults:
`~/Library/Application Support/OpenRadioInspector/macOS-Development`.
Each launch binds loopback on an OS-selected port and writes readiness to a
private, unique temporary directory. The client validates the ready URL and
`/api/v1/health` before exposing its endpoint. Arguments are passed directly to
`Process`, without shell interpolation. Selection of an interpreter authorizes
running that executable and the selected checkout's launcher.

The application retains the child's stdin lifeline. Stop closes that lifeline
and asynchronously waits for child exit; quitting the application also closes
its descriptors. A shutdown timeout retains ownership and blocks a second
launch. No process is killed by PID, port, or name. An already running core such
as `http://127.0.0.1:8770` can be connected through the regular node connection
controls and is never claimed as owned by this launcher. Changing nodes does not
implicitly stop the app's existing local core.

Twelve Swift Testing cases cover readiness validation, argument construction,
RC exclusion, successful ownership/release, failed-health cleanup, missing
environments, persistence, cancellation, and shutdown timeout ownership. A real
synthetic subprocess confirms EOF shutdown through Foundation's `Process` pipe.
The focused suite passed on macOS. Hardware behavior has not been
validated by these tests. Hardware preparation remains a separate explicit step.
