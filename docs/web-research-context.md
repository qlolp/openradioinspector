# Research evidence context

Baseline: `7e20bd21b5590e94c610a4384af29a78a2db50d9`.

A baseline comparison belongs to the selected original sweep and the request
generation that selected it. Selecting another sweep, changing history filters,
changing project or leaving the component aborts that request. Its late reply
cannot attach a comparison to another source, even when a transport completes
after cancellation. Valid compatible and incompatible responses for the current
source keep their existing meaning.

When the node's event high-water mark rolls back, the existing local connection
version advances with the live archive reset. Project-scoped reads, history
pages, selected comparisons and event/original dialogs invalidate together.
Pending original-source lookups and event-review acknowledgements retain their
original dialog generation and abort signal. They cannot reopen old evidence or
start a second lookup in the replacement context. The same project can then
load fresh history normally.

This changes client context only. Workspace/theme/locale choices, pairing
credentials and the Core's guard state keep their existing ownership. No service
commands, measurement calculations, renderer data or archived raw values change.

The regression suite mounts the real App/Research components with React
`createRoot`/`act`, real hooks, API envelope handling and controlled transport
promises. Only browser graphics and an unused worker boundary are replaced;
there are no relabelled event frames or RF effects. `happy-dom` is an exact-pinned
test dependency and adds no production runtime dependency. Existing unit,
TypeScript, build and formatting checks complement these state tests; they do
not establish browser graphics, physical hardware or Pi performance acceptance.
