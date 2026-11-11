# Automation admission and Running setup

`AutomationEffects::setup_running` is an additive default callback. The owned
policy task calls it after its actual Session lookup confirms Running and
before science, evidence queries, or device effects. The library composes its
returned check with the original policy check, so a provider cannot replace
revoked authority. Existing providers retain the same original check.

Draft opt-in remains valid. A Draft pass reports `session_not_running` without
requesting execution setup; a later explicit start permits a fresh dispatch
receipt. Each dispatch keeps its setup through completion and never refreshes
it after scientific or device awaits. Setup does not hold the supervisor
mailbox, so another session can progress independently.

`AutomationService::configure_with_check` retains originating request authority
through queued admission and the ingestion-boundary await. Under the existing
domain coordinator it revalidates before allocating a policy slot, cancelling
old work, changing the policy epoch/state, or publishing an event. A revoked
queued request consumes no session-policy capacity. The original `configure`
entrypoint remains available with its previous unchecked-provider semantics.

Scheduler visibility follows the original Python services: enabling one kind
starts only that kind; explicit `AutomationService::start` starts all kinds.
The status schema and policy persistence behavior are unchanged.

Verification used actual temporary Archives, committed start/stop transitions,
an owned setup barrier, and the existing Python authority fixtures. Five new
regressions were observed failing before the change; all 14 automation tests,
strict package Clippy, and workspace formatting passed. No hardware was used.
