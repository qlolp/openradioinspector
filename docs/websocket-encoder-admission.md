# WebSocket encoder admission

The binary event stream shares two encoding workers across at most eight
admitted subscribers. A connection waits in the semaphore's FIFO queue for an
encoder instead of disconnecting when both workers are temporarily occupied.
The capacity wait uses the existing 30-second budget. Expiry or a closed encoder
semaphore retains the existing 1013 capacity close; it does not consume the
separate applied-ACK deadline, which starts after the event is sent.

The queued wait observes the same original connection authentication and Core
shutdown as replay and ACK waits. Active encoding still retains connection
admission through the actual blocking task JOIN. The 32 MiB in-flight byte
budget remains a nonblocking admission check, with 1013 on exhaustion. There is
still only one event awaiting an exact applied ACK per connection, and a failed
wait cannot advance the committed cursor.

Each admitted connection may retain one already-loaded event while waiting.
This is a bounded count of eight, not a hard 256 MiB peak-memory guarantee for
maximum-size records. No worker/subscriber/frame limit or ACK timeout was
increased.

The regression holds both real encoder permits, opens three TCP WebSocket
clients and verifies that they wait, then receive and ACK both committed events
without reconnecting. Additional controlled tests cover FIFO admission, the
capacity deadline, original credential revocation, shutdown and unchanged byte
budget rejection. These are synthetic software checks; the original render
latency and target-device performance gates require separate measurements.
