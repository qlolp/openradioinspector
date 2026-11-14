# Health samples and archive context

Baseline: `48dd054eb193d009113597bd18432027301e04d8`.

A health response describes the head sampled by its request. It cannot be
compared with a frame that applied while that request was in flight. The shared
Web/Windows client captures the original applied cursor before each health
request and tracks accepted request order and connection/archive epoch. An old
response or failure cannot overwrite a newer sample or a replacement node.

An accepted head below the captured applied cursor OR the previously observed
head captured when that request began is evidence of archive rollback. A lower
sample relative only to a concurrently accepted newer head is ignored as
ambiguous; it neither regresses health nor resets context. The accepted rollback
fact remains pending until the
existing reset/reseed/replay path consumes it, even if the replacement archive
grows before the next health response. The last observed sample accompanies the
existing bounded in-memory applied checkpoint, so remounting does not mix old
retained live rows with a replacement archive whose head is still ahead of the
applied cursor. No health history or additional raw buffer is accumulated.

Healthy live replay continues from the last applied checkpoint. Health metadata
does not advance a live cursor. Archive replacement uses the existing explicit
display reset, invalidating research and dialogs through connectionVersion.
Core contracts, commands, guard state, manual mutation acknowledgement, pairing
credentials and raw measurements are unchanged; there are no new API requests
in the normal polling schedule.

Controlled React DOM tests use real useDesk/LiveStore and the production strict
frame parser/preparation. They deliver complete original SSE envelopes and await
the actual apply acknowledgement; only the browser worker scheduling boundary
and graphics are represented in the DOM host. Tests cover an in-flight
100→101 frame, exact cursors above2^53, observed100→99 while applied50, the same
case across a real unmount/remount, replacement growth before resync, and stale
reply/failure ordering across replay and connection changes. The healthy race
previously caused an unnecessary replay; a fresh resync response masked a false
archive clear in that case. No browser graphics, hardware or Pi performance
acceptance is claimed by these tests.

Browser API requests already use `cache: 'no-store'`; this correction does not
change caching or network policy.
