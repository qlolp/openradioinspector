# Context HTTP fixture seeding

Each `ContextHTTPTests` case still creates its own SQLite archive with the
original 16 sweeps: seven four-point sources and nine 16,384-point sources.
The existing public `ingest_sweeps` API commits the first six sources together,
creates the second session at the original position, then commits the foreign
source and nine large sources together. All 18 events retain their original
order, including the interleaved second `session.created` event.

This reduces measurement commits from 16 to two while retaining SQLite
`synchronous=FULL`. The archive is still closed, reopened at its measured
capacity, and required to report full. HTTP request bodies, framing errors,
limits, deadlines, and all existing assertions are unchanged.

Controlled software checks compare the original and batched public source,
session, event, UTC, float-bit, ordering, and capacity values. Both Python 3.10
and 3.12 pass all 46 context and fixture tests. A Linux trace of the same owned
SQLite workload observed 35 versus 18 successful sync calls, including archive
creation and reopening. These syscall counts do not establish physical storage
performance. New-head Windows CI remains a separate required check.
