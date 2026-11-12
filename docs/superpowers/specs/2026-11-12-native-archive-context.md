# Native archive context reset

The native live view and monitoring workspace must discard evidence from a
restored archive even when server URL, session, project, and stream IDs match.
The current health high-water mark is checked before a retained cache is reused.
A lower mark than previously observed or already applied proves a new archive
context and advances the existing request generations.

The reset is client-local. It clears spectrum/history, raw selection, pinned
comparison and analysis results, event/source evidence, audio status/samples and
playback, and pending request/dialog state. The selected session/project and
`native.workspace` preference remain available for a fresh actual snapshot.
Resetting never sends disarm, worker stop, device control, or phonogram stop.

Both live and monitoring models check health because the workspace can operate
without an active live subscription. The live model also notifies the workspace
synchronously when it detects rollback, before the new snapshot is published.
An older in-flight health or data response cannot reverse that invalidation.

Normal reconnects remain different: a nondecreasing archive mark retains the
last applied cursor and replays committed but unseen events. It never resumes
from the newest health mark. Cursor comparison captures the applied cursor
before awaiting health, so an event applied during that request is not mistaken
for an archive rollback.

Health checks bypass the local HTTP cache and request revalidation. A failed
check cannot republish an unverified retained measurement cache. Legacy REST
peers without health remain usable when no retained live cache is involved.

The public health contract exposes an event high-water mark, not a unique server
boot identifier. A replacement archive whose mark has already caught up between
checks cannot be distinguished by this signal alone. No physical hardware,
Raspberry Pi throughput, memory, or latency acceptance follows from these tests.
Portable SDK validation and hosted macOS model/Xcode validation are reported
separately on their exact source.
