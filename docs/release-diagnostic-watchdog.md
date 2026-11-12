# Release diagnostic timer ownership

The synthetic release runner uses the existing joined Python watchdog on
Darwin with Python 3.15. Other platform/version selections remain unchanged:
older CPython versions affected by gh-140815 already use this watchdog, while
corrected Linux/Windows versions retain the native timer.

The hosted Darwin Python 3.15.2 ARM run at `b93bd818` printed one incomplete
native 60-second dump, then completed all 2,028 tests successfully in 300.404
seconds. It did not produce another periodic dump or exit before the existing
600-second command deadline. A same-interpreter green control at `ae87d380`
produced five complete dumps and exited normally. The monitoring supervisor
appeared in the green control too, so its presence does not identify the blocker.

CPython's native timer releases its running lock only after its traceback dump
returns. `cancel_dump_traceback_later()` waits for that lock without a deadline.
The runner calls cancellation after the suite report is written. This matches
the failed run's incomplete dump and post-suite delay, but the final main-thread
stack was not captured. The policy is a diagnostic-harness mitigation; it is
not a claim that the precise upstream failure was reproduced locally.

The selected watchdog retains the requested interval (60 seconds in the release
profile), repeats full all-thread Python tracebacks, and explicitly stops and
joins its owned thread before returning. A dump failure or failed join remains
a release failure. Its existing two-second join bound, the outer 600-second
command budget, product daemon flags, test selection and suite outcomes are
unchanged. As with the existing older-CPython fallback, a Python watchdog cannot
dump while native code holds the GIL indefinitely; the owned outer timeout still
terminates that failed command.

The controlled regression writes a successful synthetic report and holds the
native-cancellation boundary behind a releasable gate. It fails on the previous
selection and always releases and joins its test thread. A separate real owned
child requires repeated actual GIL-safe traceback dumps, confirmed watchdog
join, and process exit. It does not invoke a deliberately hanging native timer.

Source references:

- [CPython 3.15.2 native dump and cancellation](https://github.com/python/cpython/blob/v3.15.2/Modules/faulthandler.c#L693)
- [CPython gh-140815](https://github.com/python/cpython/issues/140815)
