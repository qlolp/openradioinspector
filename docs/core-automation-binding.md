# Core automation binding

The production binding is implemented and verified on Linux with every Core
test target, the original Python automation oracle, and strict static checks.

This slice connects the existing `ori-automation` policies to the same Core
SessionService, canonical Archive, owned device workers, and research pool.
Policies remain runtime-only, disabled by default, and require new opt-in after
restart. Metadata and enabling a Draft-session policy never open a device.

An explicit policy update captures its originating authentication receipt and
original per-stream Guard absence/manual grants. Scheduled dispatch retrieves
that authority; it never obtains another grant. Disabling or replacing the
policy permanently revokes the old authority, including tasks still draining.
One policy's receipt cannot replace another client's in-flight receipt.
Every update, including disabling a policy, validates its selected original
Guard grants before changing policy state. A missing manual token cannot revoke
another admitted claimed policy or publish a policy-change event.

The library's additive `AutomationEffects::setup_running` callback runs only
after the existing Session lookup confirms Running, inside the owned policy
task and before scientific, evidence, or device work. Its default preserves old
providers. The library retains both the original check and setup check. The
Core override captures one current Archive lifecycle receipt and actual owned
worker handles when an actor participates. Pure derivation from uploaded raw
sweeps does not require a connected analyzer and never interprets an absent
actor as readiness or permission to open one. Checks use immutable receipts
without SQL, await, grant renewal, or replacement lookup. A normal successful tune does not revoke its
own worker identity; the returned TuneOutcome supplies the post-ACK revision.

Archive owns lifecycle truth because Guard also writes Session transitions.
`capture_session_lifecycle` enrolls Draft sessions without authorizing work;
`cached_session_lifecycle` creates a new Running receipt during dispatch setup.
Neither API refreshes an existing receipt. Stop, lifecycle replacement, and
Archive close invalidate old dispatches permanently. Draft opt-in may activate
after the operator explicitly starts that same session.

Detection submits exactly one owned Sweep to the bounded Python RPC method
`automatic_detection_regions`. Python calls the existing `detect_regions`
algorithm. Rust validates its closed response and retains original Hz, dBm,
float values, IDs, and UTC intervals; it does not implement threshold math.
The shared research owner must keep cancellation, process cleanup, and its
registration until the actual child is reaped.

Handoff uses an existing retained worker and its original execution gate. It
does not open, steal, resume, or replace a worker. Completion uses the existing
SessionService evidence checks. A completion policy with no receiver filter
conservatively requires all its originally selected target grants to remain
valid. That entire policy may be blocked by one target's ownership change;
this does not claim per-receiver independence within that broad policy.
Other policies and sessions retain independent authority.

Core starts one retained shutdown attempt. It revokes admission immediately,
then awaits AutomationService's owned tasks before closing Archive or releasing
the writer lease. A cancelled HTTP waiter does not detach configuration,
scientific work, or cleanup.

The additive `configure_with_check` entrypoint keeps the original request
authority through coordinator admission and the ingestion-boundary await. It
rechecks before changing the policy epoch, runtime state, or canonical event;
the existing unchecked library entrypoint retains its original behavior.

`validated_policy_patch` runs the library's existing closed validator and
16 KiB limit before Core stages authority. `policy_committed` activates the
exact pending original Arc only after the canonical event ACK and before the
first dispatch. A rejected post-ACK grant leaves the committed policy inactive;
it cannot revive the previous client's authority or invent a rollback.

The accompanying Python HTTP oracle uses a private SQLite file and loopback
server. It records disabled defaults, original validation messages, and Draft
opt-in followed by explicit start and the original detector. Hardware
validation is not performed by these tests.

The Linux integration gate uses an isolated installed Python 3.15 worker for
the original detector, an owned blocked child for cancellation and actual reap,
two independently paired TLS credentials, and explicitly connected synthetic
receiver workers. Completion is also tested after the receiver has stopped,
using its already-canonical evidence without reopening an actor.
Additional controlled cases cover committed stop/restart, queued original-auth
revocation, legitimate manual renewal, permanent return/reacquire revocation,
missing-token updates, and a dropped configuration waiter whose owned command
must finish cleanup before close acknowledges and the writer lease releases.

The existing Runtime explicitly refuses Windows LAN private state without a
verified owner-only DACL and refuses research children without process-tree
containment. The tests assert those actual fail-closed boundaries on Windows;
they do not skip the cases, synthesize detector results, or claim Windows TLS
or scientific-child availability. macOS and Windows were not executed in this
Linux workspace. Those boundaries remain separate from hardware verification.
