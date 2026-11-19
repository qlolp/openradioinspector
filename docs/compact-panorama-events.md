# Compact panorama events

Schema 7 stores each acquired frequency and level vector once, in the canonical `sweeps` row. HTTP ingestion receipts, live events, durable SSE replay, resume cursors, recent events after restart and source exports retain the full `sweep.received` contract. Sequence numbers, sweep IDs, stream ownership, UTC intervals, units, signed zero, binary64 precision and event-specific metadata survive replay.

## Select a format

An existing schema-6 archive keeps full event payloads. A new archive created through the ordinary archive API also uses schema 6. To select compact history for a fresh native node, run the first command before starting the service and pass the resulting path to the core. On Windows the executable is `ori-core.exe`.

```sh
ori-core archive-init-compact /private/node/sessions.sqlite3
ori-core --mode live --data-dir /private/node --database /private/node/sessions.sqlite3 --allow-serial
```

The destination directory must exist. Initialization publishes a complete standalone SQLite file and refuses an existing destination, symlink or SQLite sidecar. The Rust and Python stores also accept the explicit creation option `compact_sweep_events`. After initialization, either updated writer can open the archive with its ordinary settings. Older writers reject the newer SQLite `user_version`. Operators using Python can invoke `python -m radioinspector.panorama_events init NEW.sqlite`.

## Storage and validation

The schema marker consists of `PRAGMA user_version=7` and the nullable foreign key `events.ori_sweep_ref_v1 REFERENCES sweeps(id)`. Both parts must agree. A compact ingestion transaction commits the canonical sweep, chronological and ingestion indexes, and its event together with WAL synchronous FULL.

For a referenced row, `payload_json` is a two-element JSON array. The first element is the lowercase SHA-256 of the exact UTF-8 `sweeps.frequencies_json` text, one zero byte, and the exact UTF-8 `sweeps.levels_json` text. The second element is the original event payload with only `sweep.frequencies_hz` and `sweep.levels` omitted. All other fields retain their original values, including unknown keys in the payload, sweep and interval. A row with a NULL reference stores its full JSON object with the existing event representation.

Readers resolve references by sweep primary key within the event query's SQLite snapshot. They check the event type, sweep ID, session, stream, interval, unit, missing vector fields and vector checksum before inserting the arrays into the public payload. A dangling ID, mismatched source, damaged vector, malformed frame or unsupported schema produces an error. A missing reference on a framed row fails full-object decoding. Normal startup checks the schema, and replay validates each reference when read. Recovery verification and materialization validate the entire reference history.

## Rollback, recovery and export

Stop the node to establish a final cutover watermark. A live materialization captures a consistent read snapshot including committed WAL pages. Subsequent commits belong to the original archive. Choose a new path on a filesystem with enough free space for the expanded database and SQLite's temporary journal.

```sh
ori-core archive-materialize /private/node/sessions.sqlite3 /private/rollback/sessions-v6.sqlite3
```

The Python equivalent is `python -m radioinspector.panorama_events materialize SOURCE.sqlite NEW.sqlite`. Both commands make a private staged copy, materialize every referenced event, remove the reference column, set schema 6, run integrity checks, sync the standalone database and publish it exclusively. Publication uses same-filesystem hard links and requires support in the destination directory, as on ext4 and NTFS. Source rows and source WAL contents stay under SQLite's ownership. An invalid reference, existing output or stale output sidecar aborts the operation. Select the new database when starting the previous core.

A private recovery bundle preserves its source schema and credentials. Restoring a schema-7 bundle produces a schema-7 archive for the updated readers. Portable SQLite export materializes events as schema 6 and applies its existing credential and automatic-action sanitization. The research worker accepts sealed schema-6 and marked schema-7 recovery snapshots and reads canonical source vectors directly.

## Reproduce storage measurements

The benchmark writes 512 distinct synthetic sweeps with equal-width integer-Hz grids, deterministic four-decimal-place dBm levels (seed 42), fixed-size batches of eight and durable SQLite commits. It measures page growth from a checkpointed session baseline, live WAL file size and the final main file after a truncating checkpoint. Python disables automatic checkpoints during this observation. The Rust run holds a read snapshot to expose WAL headroom while its production writer and maintenance worker operate. Release services use their normal checkpoint policies.

```sh
python -m benchmarks.ori_bench.panorama_storage --sweeps 512 --points 290 --batch 8
python -m benchmarks.ori_bench.panorama_storage --sweeps 512 --points 4096 --batch 8
cargo build --locked -p ori-archive --example panorama_storage
python -m benchmarks.ori_bench.panorama_storage --sweeps 512 --points 290 --batch 8 --native target/debug/examples/panorama_storage
python -m benchmarks.ori_bench.panorama_storage --sweeps 512 --points 4096 --batch 8 --native target/debug/examples/panorama_storage
```

Use the Cargo target directory for the example executable if the workspace sets `CARGO_TARGET_DIR`. Synthetic Linux results follow. Every WAL measured zero bytes after the final checkpoint.

| Writer | Points | Schema | Logical growth per sweep (B) | Live WAL (B) | Checkpointed main (B) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Python | 290 | 6 | 12,856 | 12,318,832 | 6,881,280 |
| Python | 290 | 7 | 7,112 | 9,517,232 | 3,940,352 |
| Python | 4096 | 6 | 157,416 | 86,940,272 | 80,896,000 |
| Python | 4096 | 7 | 79,304 | 46,696,112 | 40,902,656 |
| Rust | 290 | 6 | 12,944 | 12,949,192 | 6,934,528 |
| Rust | 290 | 7 | 7,216 | 10,205,272 | 4,001,792 |
| Rust | 4096 | 6 | 157,504 | 87,657,152 | 80,949,248 |
| Rust | 4096 | 7 | 79,408 | 47,384,152 | 40,964,096 |

Storage varies with level precision, metadata, batch size and checkpoint policy. Live Raspberry Pi media and Windows hardware require separate capacity and sustained-acquisition measurements.
