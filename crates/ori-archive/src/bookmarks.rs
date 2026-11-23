//! The v6 frequency_bookmarks table is shared with the Python rollback server.
use crate::{Archive, ArchiveError, Result};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};

pub(crate) fn install(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS frequency_bookmarks (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        name TEXT NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL,
        UNIQUE(session_id,name,revision))",
    )?;
    Ok(())
}

impl Archive {
    pub async fn frequency_bookmarks(&self, session: &str) -> Result<Vec<Value>> {
        let session = session.to_owned();
        self.inner.read(move |db| {
            let mut statement = db.prepare("SELECT document FROM frequency_bookmarks WHERE session_id=? ORDER BY sequence DESC LIMIT 256")?;
            let mut rows = statement.query([session])?;
            let mut result = Vec::new();
            while let Some(row) = rows.next()? {
                result.push(serde_json::from_str(&row.get::<_, String>(0)?)?);
            }
            Ok(result)
        }).await
    }
}

/// Invoked only in the writer transaction. Both idempotence and the version
/// cap must be decided there so concurrent clients see the same revision.
pub(crate) fn save(
    db: &Connection,
    session: &str,
    name: &str,
    entries: Value,
    id: &str,
    at: &str,
) -> Result<(Value, bool)> {
    let previous: Option<(i64,String)> = db.query_row(
        "SELECT revision,document FROM frequency_bookmarks WHERE session_id=?1 AND name=?2 ORDER BY revision DESC LIMIT 1",
        params![session,name], |r| Ok((r.get(0)?,r.get(1)?))).optional()?;
    let mut revision = 1;
    if let Some((last, document)) = previous {
        let item: Value = serde_json::from_str(&document)?;
        if item["entries"] == entries {
            return Ok((item, false));
        }
        revision = last
            .checked_add(1)
            .ok_or_else(|| ArchiveError::Corrupt("bookmark revision".into()))?;
    }
    let count: i64 = db.query_row(
        "SELECT COUNT(*) FROM frequency_bookmarks WHERE session_id=?",
        [session],
        |r| r.get(0),
    )?;
    if count >= 256 {
        return Err(ArchiveError::Invalid(
            "frequency bookmark version limit".into(),
        ));
    }
    let value = json!({"id":id,"session_id":session,"schema_version":1,"name":name,"revision":revision,"created_at":at,"entries":entries});
    db.execute(
        "INSERT INTO frequency_bookmarks(id,session_id,name,revision,document) VALUES(?,?,?,?,?)",
        params![id, session, name, revision, serde_json::to_string(&value)?],
    )?;
    Ok((value, true))
}
