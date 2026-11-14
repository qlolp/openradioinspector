# Original-grant cancellation while awaiting coverage admission

`confirm_instrument_coverage` waits for the project's lifecycle lock using the
same original InstrumentControlGrant. If that grant is revoked or expires
while another operation holds the lock, coverage returns the existing denied
error without waiting for that operation to release it. The original grant is
rechecked after lock acquisition.

The cancellation select covers lock acquisition only. Once admitted, the
existing coverage mutation is awaited normally, including its canonical write
and post-write checks. No fresh grant is captured, no newer ownership epoch's
coverage is changed, and no mutation future is abandoned on revocation.

The regression acquires actual analyzer and receiver manual leases, holds the
actual project lifecycle through a public receiver tune, queues analyzer
coverage, and revokes its original grant through real disarm. It verifies that
coverage completes before the tune releases the project lock, then drains all
owned operations and checks final stopped coverage. The regression was RED
before the acquisition fix and GREEN afterward. The control fixture and
Archive are owned synthetic resources; no hardware validation is claimed.
