//! Page only metadata into SQLite's bounded temporary store, never point arrays.
use super::{MAX_RECORD, Shared, SnapshotExportLimits, SnapshotExportQuery, deadline};
use crate::{ArchiveError, Result, validate};
use rusqlite::{Connection, ToSql, params};
use std::{sync::atomic::Ordering, time::Instant};

const PAGE_ROWS: usize = 128;
// Core admits at most two raw cursors: at most 10GiB of metadata files total.
const SORT_BYTES: u64 = 5 * 1024 * 1024 * 1024;

fn budget(shared: &Shared) -> Result<()> {
    if shared.cancelled.load(Ordering::Acquire) {
        return Err(ArchiveError::Closed);
    }
    if Instant::now()
        >= *shared
            .deadline
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?
    {
        return Err(ArchiveError::QueryBudget);
    }
    Ok(())
}

pub(super) fn prepare(
    db: &Connection,
    query: &SnapshotExportQuery,
    limits: SnapshotExportLimits,
    shared: &Shared,
) -> Result<(u64, u64)> {
    prepare_bounded(db, query, limits, shared, SORT_BYTES)
}

fn prepare_bounded(
    db: &Connection,
    query: &SnapshotExportQuery,
    limits: SnapshotExportLimits,
    shared: &Shared,
    sort_bytes: u64,
) -> Result<(u64, u64)> {
    if sort_bytes == 0 || sort_bytes > SORT_BYTES {
        return Err(ArchiveError::QueryBudget);
    }
    deadline(limits, shared)?;
    // Main was opened SQLITE_OPEN_READ_ONLY. Only the connection's TEMP
    // database is writable during preparation; query_only is restored below.
    db.set_limit(
        rusqlite::limits::Limit::SQLITE_LIMIT_LENGTH,
        MAX_RECORD as i32,
    )?;
    db.execute_batch("PRAGMA query_only=OFF; PRAGMA temp.cache_size=-4096; PRAGMA temp.journal_mode=OFF;
        CREATE TEMP TABLE ori_export_order(kind INTEGER NOT NULL,started_at TEXT NOT NULL,
        id_utf8 BLOB NOT NULL,id TEXT NOT NULL,PRIMARY KEY(kind,started_at,id_utf8)) WITHOUT ROWID;")?;
    let page_size: i64 = db.query_row("PRAGMA temp.page_size", [], |r| r.get(0))?;
    if page_size <= 0 || page_size as u64 > sort_bytes {
        return Err(ArchiveError::QueryBudget);
    }
    db.execute_batch(&format!(
        "PRAGMA temp.max_page_count={};",
        sort_bytes / page_size as u64
    ))?;
    budget(shared)?;
    let mut insert = db.prepare("INSERT INTO temp.ori_export_order VALUES(?,?,?,?)")?;
    let mut counts = [0_u64; 2];
    for (kind, table) in ["sweeps", "observations"].into_iter().enumerate() {
        let mut after: Option<(String, String, String)> = None;
        loop {
            budget(shared)?;
            deadline(limits, shared)?;
            let mut sql =
                format!("SELECT stream_id,started_at,id FROM main.{table} WHERE session_id=?");
            let mut args: Vec<&dyn ToSql> = vec![&query.session_id];
            if let Some(stream) = &query.stream_id {
                sql.push_str(" AND stream_id=?");
                args.push(stream);
            }
            if let Some((stream, started, id)) = &after {
                if query.stream_id.is_none() {
                    sql.push_str(" AND (stream_id,started_at,id)>(?,?,?)");
                    args.push(stream);
                } else {
                    sql.push_str(" AND (started_at,id)>(?,?)");
                }
                args.push(started);
                args.push(id);
            }
            sql.push_str(if query.stream_id.is_none() {
                " ORDER BY stream_id,started_at,id LIMIT 128"
            } else {
                " ORDER BY started_at,id LIMIT 128"
            });
            let mut statement = db.prepare(&sql)?;
            let mut rows = statement.query(rusqlite::params_from_iter(args.iter()))?;
            drop(args);
            let mut read = 0;
            while let Some(row) = rows.next()? {
                let stream: String = row.get(0)?;
                let started: String = row.get(1)?;
                let id: String = row.get(2)?;
                let utc = validate::utc(&started)?;
                insert
                    .execute(params![kind as i64, utc, id.as_bytes(), id])
                    .map_err(|e| {
                        if e.sqlite_error_code() == Some(rusqlite::ErrorCode::DiskFull) {
                            ArchiveError::QueryBudget
                        } else {
                            e.into()
                        }
                    })?;
                counts[kind] = counts[kind]
                    .checked_add(1)
                    .ok_or(ArchiveError::QueryBudget)?;
                after = Some((stream, started, id));
                read += 1;
                budget(shared)?;
            }
            if read < PAGE_ROWS {
                break;
            }
        }
    }
    drop(insert);
    db.execute_batch("PRAGMA query_only=ON")?;
    Ok((counts[0], counts[1]))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::OpenFlags;
    use std::sync::mpsc::sync_channel;

    #[test]
    fn exhausted_temporary_metadata_quota_never_changes_the_main_archive() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("sources.sqlite");
        let seed = Connection::open(&path).unwrap();
        seed.execute_batch(
            "CREATE TABLE sweeps(stream_id TEXT,started_at TEXT,id TEXT,session_id TEXT);
            CREATE TABLE observations(stream_id TEXT,started_at TEXT,id TEXT,session_id TEXT);
            CREATE INDEX covered ON sweeps(session_id,stream_id,started_at,id);",
        )
        .unwrap();
        for i in 0..32 {
            seed.execute(
                "INSERT INTO sweeps VALUES('stream','2026-11-11T00:00:00.000000Z',?,'session')",
                [format!("{i:04}-{}", "x".repeat(1000))],
            )
            .unwrap();
        }
        drop(seed);
        let before = std::fs::read(&path).unwrap();
        let db = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
        db.execute_batch("PRAGMA temp_store=FILE; BEGIN").unwrap();
        let (sender, _receiver) = sync_channel(1);
        let shared = Shared::new(std::time::Duration::from_secs(5), sender, None);
        let query = SnapshotExportQuery {
            session_id: "session".into(),
            stream_id: None,
        };
        let result = prepare_bounded(&db, &query, SnapshotExportLimits::default(), &shared, 8192);
        assert!(
            matches!(result, Err(ArchiveError::QueryBudget)),
            "{result:?}"
        );
        drop(db);
        assert_eq!(std::fs::read(&path).unwrap(), before);
    }
}
