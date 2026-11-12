# Original checks for manual investigation starts

**Scope:** Only `ori-session/src/service.rs`, dedicated tests and docs. Base
`653c8c4`. Core owns HTTP authentication, retained operation lifetime and actual
device integration. No Core, Archive, dependency or lockfile changes here.

**Frozen additive API:**

- `start_investigation_with_effects_and_check(session, id, effects, check)`
- `start_next_investigation_with_effects_and_check(session, id, effects, check)`

Both take the original `Arc<dyn TuneCompletionCheck>`, separate from automatic
`EligibleStart` selection. Revalidate under the shared domain coordinator after
preceding awaits immediately before saving `tuning`; revalidate that same check
and the returned tune receipt under the coordinator before saving `listening`.
Post-tune revocation uses the existing failed-job/candidate-queued terminal path.
No tune or raw-data rollback is claimed. Existing APIs retain their None checks
and their manual target/confirmed-next semantics.

- [x] Write dedicated actual Archive/Session race and selection tests first.
  Expose the new signatures by delegating to existing unchecked paths, then
  observe behavioral RED before adding check logic.
- [x] Add a separate optional original check inside `start_job` and both checks
  at the frozen commit boundaries.
- [x] Obtain the explicitly handed-off existing Core target/one-job slot, then
  run focused RED→GREEN, full Session tests, strict Clippy/fmt/diff gates.
- [x] Document lifetime/authority distinction and release a scoped commit.

Validation: the unchecked-delegation baseline failed three real behavior tests
and passed two controls. The implemented checks pass all five focused tests and
all 16 Session tests, including the unchanged Python golden. Strict Clippy,
formatting and diff checks pass. Test logs are retained outside the shared target.

Library fixtures have controlled callbacks and an explicitly owned start task;
dropping its result observer must not create new authority. They do not claim
HTTP disconnect behavior or physical device execution. Core supplies that proof.
