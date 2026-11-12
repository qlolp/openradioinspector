# Owned synthetic guard cleanup

The Web Playwright fixture owns a temporary loopback Python node. Its cleanup
selects only `North laboratory` at `Synthetic acceptance fixture`, checks the
active status, and requires the original `tinysa-sim` and `ardv1-sim` adapters
before disarming. Foreign projects and physical adapters are never cleanup
targets.

The project list returns seven summary fields and deliberately omits `profile`.
The status response contains the full record at `data.project`. Cleanup uses
that record only after matching its ID, name and site to the selected summary.
The original adapter checks and failed-disarm failure remain required.

Hosted run `43152412416`, artifact `13916140296`, reproduced a TypeError in the
old `workflows.spec.ts` after-each hook when an active guard reached that path.
Its recorded list and status responses confirm both API shapes. This was a test
fixture error; the sequence-gap and Canvas assertions are unchanged.

The focused Vitest regression reproduces that TypeError using the compact list
and full status shapes, then checks successful owned cleanup, inactive/foreign
projects, changed status identity, physical-adapter rejection, and a failed
disarm without retry. It performs no HTTP, serial or hardware operations.
The full 22-case browser suite remains a separate validation step, with the
same ownership, assertions, worker count, timeout and replay denominator.
