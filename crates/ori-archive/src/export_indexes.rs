//! Versioned, rebuildable indexes; canonical tables and their ordering stay intact.
use crate::{ArchiveError, Result};
use rusqlite::{Connection, OptionalExtension};

const DDL: &str = "CREATE TABLE IF NOT EXISTS ori_export_index_schema(
singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_export_index_schema VALUES(1,1);
CREATE INDEX IF NOT EXISTS ori_sweeps_export_v1 ON sweeps(session_id,stream_id,started_at,id);
CREATE INDEX IF NOT EXISTS ori_observations_export_v1 ON observations(session_id,stream_id,started_at,id);";

pub(crate) fn check(db: &Connection) -> Result<()> {
    if db
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ori_export_index_schema'",
            [],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .is_none()
    {
        return Ok(());
    }
    let rows = db
        .prepare("SELECT singleton,version FROM ori_export_index_schema LIMIT 2")?
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if rows.len() != 1 || rows[0].0 != 1 || rows[0].1 < 1 {
        return Err(ArchiveError::Corrupt("export index metadata".into()));
    }
    if rows[0].1 != 1 {
        return Err(ArchiveError::UnsupportedSchema {
            domain: "export_indexes",
            version: rows[0].1,
        });
    }
    Ok(())
}

pub(crate) fn install(db: &Connection) -> Result<()> {
    db.execute_batch(DDL)?;
    check(db)?;
    for (name, table) in [
        ("ori_sweeps_export_v1", "sweeps"),
        ("ori_observations_export_v1", "observations"),
    ] {
        let actual: String = db.query_row(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
            [name],
            |r| r.get(0),
        )?;
        let columns = db
            .prepare("SELECT name FROM pragma_index_info(?) ORDER BY seqno")?
            .query_map([name], |r| r.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let partial: i64 = db.query_row(
            "SELECT partial FROM pragma_index_list(?) WHERE name=?",
            [table, name],
            |r| r.get(0),
        )?;
        if actual != table
            || columns != ["session_id", "stream_id", "started_at", "id"]
            || partial != 0
        {
            return Err(ArchiveError::Corrupt("export index layout".into()));
        }
    }
    Ok(())
}
