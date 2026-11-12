# Spectrum display ownership

An actually drawn spectrum keeps ownership of its canvas until its original two
animation-frame callbacks publish `data-sweep-id`. The next source is promoted
after that mutation's observer delivery and draws in a later animation frame.
The marker still identifies submitted pixels; it is never produced for an
undrawn source or moved ahead of the existing paint barrier.

The renderer reserves its first source even before the initial draw. Later
distinct sources are held in a FIFO of at most 64 pending snapshots. Snapshots
retain the existing immutable display arrays by reference; they do not copy
measurement vectors. If the pending FIFO is full, a new source replaces only
the last undrawn entry, preserving the owner and FIFO prefix. The read-only
`SpectrumRenderer.presentationStatus` reports pending and overflow-coalesced
source counts. Scope changes, graphics loss/restoration and disposal discard
pending work and cancel old markers. Context recovery draws the latest valid
input rather than replaying an obsolete backlog.

The display context consists of exact session and stream IDs, raw level unit,
full first/last frequency and original point count. Peak-dependent decimation
can select different interior frequencies on every source, so those selected
vectors are not compared as a context identity. The current display DTO does
not carry the original interior grid. This policy cannot distinguish two raw
grids with identical bounds and count, and does not invent such a receipt.

Spectrum accessibility text, units, level/frequency axes, comparison legend
and peak-outside-view warning follow the snapshot actually drawn. Their React
state is committed after pixel submission and before the paint barrier. Old
context pixels stay hidden until the new context has drawn; a late draw cannot
relabel a different scope. Pan/zoom requests remain display-only. They update
the current owner's view and every pending view, then schedule the current
owner's redraw. They do not enqueue a source or wait for its FIFO position,
change owner/source generations, or copy measurement arrays. Repeated
views of an already displayed source can draw once per animation frame and do
not wait for each previous view's two-frame marker.

The live App forwards every newly committed row retained in the existing
256-row buffer, even when one RAF publication or React commit contains several
sources. A local presentation receipt carries an opaque epoch and an exact
`bigint` append count. Spectrum advances its cursor only in committed effects,
then offers the new suffix in order to `SpectrumRenderer.update`. DTOs and
measurement vectors are passed by reference. The first live A/B/C snapshot
admits A before B and C; latest C is not admitted separately ahead of that suffix.

Seed and restored checkpoint rows are a zero-new-source baseline: the latest
baseline may display once, but its history is not queued for replay. Analyzer
scope changes, archive resets (including reused IDs), restore and an unsupported
source fence the presentation epoch. Same-scope reconnect preserves it. The
receipt is neither persisted in checkpoints nor added to any wire/source field.
Disposed plots cancel their source barriers and discard their local cursor.

This handoff adds no unbounded queue. If a publication misses more new rows than
the 256-row buffer retains, `LiveSourceCursor.presentationStatus` reports the
exact `retainedRowEvictions` count for that epoch. The renderer's separate
64-entry pending FIFO and explicit overflow-coalescing policy remain unchanged.
Neither count manufactures a paint marker or makes incomplete source rendering
pass an acceptance check. ACKs still follow parsing/application without waiting
for RAF publication, drawing or paint.

The surrounding peak/event summary, original-point caption and source footer describe the latest incoming
saved measurement, independently of the spectrum's current display snapshot.
The visible localized “Latest saved measurement” label and accessible groups
identify that distinction. Waterfall history also follows incoming history.
The existing source-ID text remains unchanged. Worker decoding, durable event replay,
applied-sequence ACKs, canonical arrays and acquisition cadence are unchanged.
Overflow coalescing is not a claim that every source is painted.

Verification includes a real Canvas/native RAF/native MutationObserver browser
regression: draw A, deliver same-context B before A's barrier, observe A with
matching pixels and labels, then observe B. A raw unit gives A/B different
vertical bounds. Separate unit, full-range and scope changes must cancel A.
Unit tests cover reservation before first draw, the bounded FIFO/overflow,
peak-dependent decimation, cleanup, unthrottled same-source views and pan/zoom
while A owns the canvas with B/C queued. A native browser case triggers Zoom
from A's actual draw metadata mutation with B pending, checks A's new view on
the next scheduled draw, and checks that B inherits that view. The
existing 1,205-event functional and interaction probes keep their original
counts, deadlines and frame-rate requirement. Synthetic tests do not establish
Raspberry Pi, physical-device or long-duration performance acceptance.

A controlled native browser regression mounts the real App, hooks, LiveStore
and Spectrum with an empty seed. Production `parseFrame` handles three exact
290-point sources; only Worker completion scheduling is controlled. Three
sequential applied ACKs complete within one publication checkpoint, while native
Canvas/RAF/MutationObserver must observe A, B and C separately. A spaced control
requires the same markers. The baseline showed only C for the coalesced case.
This isolates a reachable publication-loss path; the earlier thirty-minute
observer did not record per-frame client apply telemetry and cannot attribute
each missing historical marker to this path. Cursor/store/component tests cover
baseline, restore, same-ID reset, duplicates, wrong scope, exact counts beyond
JavaScript safe integers, retained-row eviction, initial admission and obsolete
marker cancellation. Original source IDs, UTC precision, units and every level
and frequency remain unchanged, including negative zero and decomposed IDs.
