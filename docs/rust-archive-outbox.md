# Canonical notification outbox

The Archive owns notification storage and atomic claim/compare-and-set updates.
It does not perform HTTP delivery, compute notification IDs, sign messages, or
start a supervisor. Guard/core retain the existing Python delivery policy and
transport callback. All writes use the same dedicated canonical writer actor;
there is no guard SQL connection or second writer.

## Typed interface

`NotificationRow` contains `id`, `project_id`, `created_at`, `payload: Value`,
`state`, `attempts: u32`, and nullable `next_at`, `error`, `lease_until`, and
`acknowledged_at`. Normal rows and pages contain no webhook credentials.

```text
enqueue_notification(NotificationRow) -> CommittedBatch
notification(id) -> Option<NotificationRow>
notification_page(project, Option<MeasurementCursor>, limit) -> Page<NotificationRow>
webhook_config(project) -> Option<WebhookConfig>
claim_notification(now, lease_until) -> NotificationClaim
finish_notification(id, attempts, lease_until, NotificationCompletion) -> bool
acknowledge_notification(project, id, at) -> bool
```

Page limits are 1–100, with a one-row lookahead and descending creation-time/ID
seek cursor. The cursor's `started_at` field carries notification creation time.
Notification lookup/page reads use the existing independent bounded reader
actors; private credential reads have explicit byte accounting without a
serializable credential DTO. The configured SQL and response budgets still
apply. Invalid timestamps, impossible limits and oversized commands fail before
an admitted mutation changes data.

`enqueue_notification` inserts or ignores an existing deterministic ID. It must
never reset a previously delivered attempt, acknowledgement, lease or disabled
state. A fixed enqueue mutation can compose the insertion with related canonical
metadata changes in one `apply` transaction. Ordinary read/modify/commit domain
operations use the shared coordinator where required; atomic outbox CAS remains
an independent writer operation.

`WebhookConfig` exposes `url()` and `secret()` for internal callbacks. It has no
`Debug` or `Serialize` implementation. Claim replies likewise cannot be
accidentally formatted/serialized as public data. Credential-bearing mutation
debug output is redacted. No credential is included in an event, normal row,
page or public settings response.

## Claim and completion

`NotificationClaim` has three outcomes:

- `Empty`: no eligible work remains at the supplied time.
- `Disabled { notification_id }`: the existing row was marked disabled because
  its project disables webhooks or has no configuration; do not call transport.
- `Deliver(Box<ClaimedNotification>)`: `row` contains the incremented attempt and
  committed lease, `config` supplies the private callback configuration, and
  `body()` exposes the exact stored UTF-8 JSON bytes.

The claim transaction follows `OperationalMonitoring.deliver_one`. Expired
delivering rows at attempt five or later become failed with
`last_attempt_interrupted` and a null lease. It then selects the oldest eligible
creation-time/ID row with fewer than five attempts: pending and due, or delivering
with an expired lease. A configured/enabled claim increments attempts and saves
the new lease atomically. Two simultaneous callers cannot claim the same live
lease. No transport runs before COMMIT.

The service supplies UTC `now` and a future lease (Python uses thirty seconds).
After releasing any domain coordinator, it signs and sends `body()` directly.
The original Python HMAC covers the **stored bytes**, not reserialized
`row.payload`; whitespace, key order and original numeric lexemes can differ.
Transport cancellation after claim leaves a durable lease for normal recovery.

`NotificationCompletion` is `Sent`, `Retry { next_at, error }`, or
`Failed { error }`. Guard computes retry policy/timestamps from the original
Python rules (at most five attempts; delay `min(300, 5 * 2**attempts)` seconds).
Archive changes a completion only when ID, attempt count, delivering state and
the exact claimed lease still match. The returned boolean is the actual SQL CAS
outcome. A stale completion cannot clear a newer lease or overwrite its state.
An expired but unreclaimed lease can still complete, matching Python's CAS.

Acknowledgement is separately scoped by project/ID and retains the earliest
existing acknowledgement through `COALESCE`. Foreign/missing IDs return false.
It does not cancel delivery. The original disabled/expired cleanup column
semantics are retained: a disabled row does not automatically re-enable when
settings change, and unrelated legacy scheduling fields are not rewritten.

## Verification boundary

The owned fixture generator calls the actual Python `OperationalMonitoring`
methods against SQLite with a pure fake transport. It records claim state,
payload bytes and retry/sent/acknowledged rows. Rust regressions compare those
results and exercise real temporary SQLite concurrency, expired/reclaimed
leases, stale CAS, disabled/missing configuration, duplicate enqueue, scoped
acknowledgement, exact opaque bodies, bounded pages and archive reopen.

There is no physical hardware or actual webhook delivery in these checks. The
core release integration separately validates its transport callback, headers,
HMAC and supervisor/cancellation behavior.
