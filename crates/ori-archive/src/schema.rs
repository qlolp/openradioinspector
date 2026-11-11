use crate::{ArchiveConfig, ArchiveError, Result, validate};
use rusqlite::{
    Connection, OpenFlags, OptionalExtension, TransactionBehavior, functions::FunctionFlags,
};
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
};

pub const MAIN_VERSION: i64 = 6;
pub const MONITORING_VERSION: i64 = 4;
pub const PRECISION_VERSION: i64 = 1;
pub const PRECISION_DDL: &str = "CREATE TABLE IF NOT EXISTS ori_archive_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_archive_schema VALUES(1,1);
CREATE TABLE IF NOT EXISTS ori_observation_level_bits(observation_id TEXT PRIMARY KEY REFERENCES observations(id) ON DELETE CASCADE, level_bits_le BLOB NOT NULL CHECK(length(level_bits_le)=8));";

static OPEN_WRITERS: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();
pub(crate) struct ProcessWriterLease(PathBuf);
impl ProcessWriterLease {
    pub fn acquire(path: &Path) -> Result<Self> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)?;
        let canonical = if path.exists() {
            path.canonicalize()?
        } else {
            parent.canonicalize()?.join(
                path.file_name()
                    .ok_or_else(|| crate::error::invalid("archive filename"))?,
            )
        };
        let mut paths = OPEN_WRITERS
            .get_or_init(Default::default)
            .lock()
            .map_err(|_| ArchiveError::WorkerStopped)?;
        if !paths.insert(canonical.clone()) {
            return Err(ArchiveError::AlreadyOpen);
        }
        Ok(Self(canonical))
    }
}
impl Drop for ProcessWriterLease {
    fn drop(&mut self) {
        if let Ok(mut paths) = OPEN_WRITERS.get_or_init(Default::default).lock() {
            paths.remove(&self.0);
        }
    }
}

fn table(db: &Connection, name: &str) -> Result<bool> {
    Ok(db
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .is_some())
}
pub(crate) fn versions(db: &Connection) -> Result<(i64, i64, i64)> {
    crate::candidate_precision::check(db)?;
    let main: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    let monitoring = if table(db, "monitoring_schema")? {
        db.query_row(
            "SELECT COALESCE(MAX(version),0) FROM monitoring_schema",
            [],
            |r| r.get(0),
        )?
    } else {
        0
    };
    let metadata = table(db, "ori_archive_schema")?;
    let supplement = table(db, "ori_observation_level_bits")?;
    if metadata != supplement {
        return Err(ArchiveError::Corrupt(
            "incomplete canonical precision extension".into(),
        ));
    }
    let precision = if metadata {
        let rows = db
            .prepare("SELECT singleton,version FROM ori_archive_schema")?
            .query_map([], |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)))?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        if rows.len() != 1 || rows[0].0 != 1 || rows[0].1 < 1 {
            return Err(ArchiveError::Corrupt("precision schema metadata".into()));
        }
        rows[0].1
    } else {
        0
    };
    for (domain, version, supported) in [
        ("main", main, MAIN_VERSION),
        ("monitoring", monitoring, MONITORING_VERSION),
        ("precision", precision, PRECISION_VERSION),
    ] {
        if version > supported || version < 0 {
            return Err(ArchiveError::UnsupportedSchema { domain, version });
        }
    }
    Ok((main, monitoring, precision))
}

pub(crate) fn functions(db: &Connection) -> Result<()> {
    db.create_scalar_function(
        "context_utc",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let input = ctx.get::<String>(0)?;
            validate::utc(&input).map_err(|e| rusqlite::Error::UserFunctionError(Box::new(e)))
        },
    )?;
    db.create_scalar_function(
        "context_utf8",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(ctx.get::<String>(0)?.into_bytes()),
    )?;
    Ok(())
}

pub(crate) fn writer(config: &ArchiveConfig) -> Result<Connection> {
    if config.path.exists() {
        let probe = Connection::open_with_flags(
            &config.path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        versions(&probe)?; // No WAL/DDL/version changes to an unsupported future archive.
    }
    let mut db = Connection::open_with_flags(
        &config.path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    functions(&db)?;
    versions(&db)?;
    db.busy_timeout(config.busy_timeout)?;
    db.pragma_update(None, "foreign_keys", true)?;
    let mode: String = db.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
    if mode != "wal" {
        return Err(ArchiveError::Corrupt("WAL could not be enabled".into()));
    }
    db.pragma_update(None, "synchronous", "FULL")?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (main, monitoring, _) = versions(&tx)?;
    // Only fixed canonical DDL from the actual Python empty archive.
    tx.execute_batch(include_str!("schema_v6.sql"))?;
    let mut columns = tx.prepare("PRAGMA table_info(investigations)")?;
    let listening = columns
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<std::result::Result<Vec<_>, _>>()?
        .iter()
        .any(|c| c == "listening_started_at");
    drop(columns);
    if !listening {
        tx.execute_batch("ALTER TABLE investigations ADD COLUMN listening_started_at TEXT")?;
    }
    if monitoring < 4 {
        let mut after = 0_i64;
        loop {
            let rows=tx.prepare("SELECT rowid,kind,id,created_at FROM monitoring_records WHERE rowid>? ORDER BY rowid LIMIT 128")?.query_map([after],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
            if rows.is_empty() {
                break;
            }
            for (rowid, kind, id, time) in rows {
                tx.execute(
                    "UPDATE monitoring_records SET created_at=? WHERE kind=? AND id=?",
                    rusqlite::params![validate::utc(&time)?, kind, id],
                )?;
                after = rowid;
            }
        }
    }
    let count: i64 = tx.query_row("SELECT COUNT(*) FROM monitoring_schema", [], |r| r.get(0))?;
    if count == 0 {
        tx.execute("INSERT INTO monitoring_schema VALUES(4)", [])?;
    } else if count == 1 {
        tx.execute("UPDATE monitoring_schema SET version=4 WHERE version<4", [])?;
    } else {
        return Err(ArchiveError::Corrupt(
            "multiple monitoring schema rows".into(),
        ));
    }
    tx.execute_batch("INSERT OR IGNORE INTO monitoring_sessions SELECT r.id,j.value FROM monitoring_records r,json_each(r.payload,'$.session_ids') j WHERE r.kind='project'; DROP INDEX IF EXISTS monitoring_sweeps_session_time;")?;
    if main < 6 || monitoring < 4 {
        let mut after = 0_i64;
        loop {
            let rows=tx.prepare("SELECT s.rowid,s.id,s.session_id,s.started_at,s.ended_at,s.frequencies_json FROM sweeps s LEFT JOIN monitoring_sweep_index i ON i.sweep_id=s.id WHERE i.sweep_id IS NULL AND s.rowid>? ORDER BY s.rowid LIMIT 128")?.query_map([after],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,String>(4)?,r.get::<_,String>(5)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
            if rows.is_empty() {
                break;
            }
            for (rowid, id, sid, start, end, raw) in rows {
                let frequencies: Vec<i64> = serde_json::from_str(&raw)?;
                let min = frequencies.iter().min().ok_or_else(|| {
                    ArchiveError::Corrupt("empty historical frequency grid".into())
                })?;
                let max = frequencies.iter().max().unwrap();
                tx.execute(
                    "INSERT INTO monitoring_sweep_index VALUES(?,?,?,?,?,?)",
                    rusqlite::params![
                        id,
                        sid,
                        validate::utc(&start)?,
                        validate::utc(&end)?,
                        min,
                        max
                    ],
                )?;
                after = rowid;
            }
        }
    }
    if main < 6 {
        tx.execute_batch("INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) SELECT id,session_id,stream_id FROM sweeps WHERE NOT EXISTS(SELECT 1 FROM sweep_ingestion i WHERE i.sweep_id=sweeps.id) ORDER BY id;")?;
    }
    tx.execute_batch(PRECISION_DDL)?;
    crate::candidate_precision::install(&tx)?;
    let count: i64 = tx.query_row("SELECT COUNT(*) FROM ori_archive_schema", [], |r| r.get(0))?;
    if count != 1 {
        return Err(ArchiveError::Corrupt("precision schema metadata".into()));
    }
    tx.pragma_update(None, "user_version", MAIN_VERSION)?;
    tx.commit()?;
    Ok(db)
}

pub(crate) fn reader(config: &ArchiveConfig) -> Result<Connection> {
    let db = Connection::open_with_flags(
        &config.path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    functions(&db)?;
    db.busy_timeout(config.busy_timeout)?;
    db.pragma_update(None, "query_only", true)?;
    Ok(db)
}

pub(crate) fn database_bytes(db: &Connection) -> Result<u64> {
    let count: i64 = db.query_row("PRAGMA page_count", [], |r| r.get(0))?;
    let size: i64 = db.query_row("PRAGMA page_size", [], |r| r.get(0))?;
    Ok((count as u64) * (size as u64))
}
pub(crate) fn capacity_check(db: &Connection, limit: Option<u64>, before: bool) -> Result<()> {
    if let Some(max_database_bytes) = limit {
        let database_bytes = database_bytes(db)?;
        if database_bytes > max_database_bytes || (before && database_bytes == max_database_bytes) {
            return Err(ArchiveError::Capacity {
                database_bytes,
                max_database_bytes,
            });
        }
    }
    Ok(())
}
