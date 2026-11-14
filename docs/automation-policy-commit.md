# Canonical policy admission handoff

`AutomationEffects::policy_committed` is an additive default, cache-only
callback after the policy-change event's canonical ACK and before any policy
dispatch. Existing providers receive the default no-op. Checked configuration
passes the exact original `Arc<dyn TuneCompletionCheck>` supplied at admission;
unchecked configuration passes `None`.

Core can activate its already-staged policy authority by exact Arc identity at
this boundary. The callback performs no I/O or awaits and never captures a new
grant, session generation, or worker receipt. Revoked original authority must
remain inactive. Per-dispatch execution setup remains separate.

If a callback rejects after canonical ACK, the request returns its error and
does not start a new scheduler. The already-committed event and policy state
remain committed; this error does not claim a rollback. Original authority
checks continue to deny dispatch.

Two controlled regressions use an actual Archive and canonical commit notices.
One queues configuration and its first dispatch before awaiting either reply,
then releases coordinator admission and requires activation before that first
dispatch. The other revokes the original receipt after its last admission
decision and before the canonical write, and verifies that post-ACK activation
fails without inventing rollback or minting another receipt. Both were RED
before the handoff was wired. No sleeps or physical hardware are used.
