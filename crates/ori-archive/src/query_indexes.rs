//! Versioned derived indexes. Canonical v6/v4 and precision v1 stay readable by
//! the rollback server; no original row, ID, time or measurement is rewritten.
use crate::{ArchiveError, Result};
use rusqlite::Connection;

pub(crate) fn check(db: &Connection) -> Result<()> {
    let exists: bool = db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='ori_archive_query_schema')",[],|row|row.get(0))?;
    if !exists {
        return Ok(());
    }
    let rows = db
        .prepare("SELECT singleton,version FROM ori_archive_query_schema LIMIT 2")?
        .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if rows.len() != 1 || rows[0].0 != 1 || rows[0].1 < 1 {
        return Err(ArchiveError::Corrupt("query index schema metadata".into()));
    }
    if rows[0].1 != 1 {
        return Err(ArchiveError::UnsupportedSchema {
            domain: "query_indexes",
            version: rows[0].1,
        });
    }
    Ok(())
}
pub(crate) fn install(db: &Connection) -> Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS ori_archive_query_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
        INSERT OR IGNORE INTO ori_archive_query_schema VALUES(1,1);
        CREATE INDEX IF NOT EXISTS ori_monitoring_event_cursor ON monitoring_records(kind,project_id,json_extract(payload,'$.event_id'),id);")?;
    Ok(())
}
