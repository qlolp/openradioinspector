# Windows owned-command failure evidence

The release runner still rejects a command that exits while its original
Windows Job Object has active descendants. Before the existing forced cleanup,
it now saves `<check>.windows-job.json` beside the command log. Modern check
names start with `modern-`, so the existing diagnostic report collector retains
these files on failure.

The observer makes one `JobObjectBasicProcessIdList` query with a fixed buffer
for 32 members: two 32-bit DWORD counts followed by pointer-sized process IDs.
`ERROR_MORE_DATA` is recorded as overflow; the observer never reallocates or
retries enumeration. An overflow or failed query provides no membership or
closure proof.

Each listed PID is opened with query-limited and synchronization rights only.
The opened handle must pass `IsProcessInJob` against the original Job handle
before any process details are read. A foreign/reused PID, access denial, or
uncertain membership records no image, birth time or wait state. Confirmed
members provide handle-based creation time, an image path bounded to a
1,024-character buffer, a zero-time wait result and exit code. A signaled process
may have exit code 259; that number alone never means the process is active.
Every opened query handle is closed, including when a later query fails.

Membership and lifecycle facts can race with process exit. The report retains
those uncertainties and API errors; it never becomes authority to signal a PID
or declare closure. Termination still targets only the original owned Job.
No command line, environment, child output or global process scan is collected.
Collection and report-write failures cannot replace the original strict gate
error or bypass its existing cleanup. Timeouts, accounting grace, termination
and acceptance rules are unchanged.

Portable fake-API tests exercise the native structure sizes, denied/foreign
handles, bounded overflow, exit-code ambiguity, query-handle cleanup, report
persistence and failure isolation. They do not substitute for a real Windows
run. The original hosted failure did not record member identities, so its
responsible descendant remains unknown.
