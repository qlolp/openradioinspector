//! Schema 7 replaces only the two source vectors in a durable sweep event.
//! All public readers return the original event, including unknown metadata.
use crate::{ArchiveError, Result, parse_json_value, rows, validate};
use rusqlite::{Connection, Row, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub const VERSION: i64 = 7;
pub(crate) const INSTALL: &str =
    "ALTER TABLE events ADD COLUMN ori_sweep_ref_v1 TEXT REFERENCES sweeps(id)";
const INLINE: &str = "SELECT e.* FROM events e";
const JOINED: &str = "SELECT e.*, s.session_id AS source_session, s.stream_id AS source_stream,
    s.started_at AS source_start, s.ended_at AS source_end, s.level_unit AS source_unit,
    s.frequencies_json AS source_hz, s.levels_json AS source_levels
    FROM events e LEFT JOIN sweeps s ON s.id=e.ori_sweep_ref_v1";
const REFERENCES: &str =
    "(e.ori_sweep_ref_v1 IS NOT NULL OR substr(ltrim(e.payload_json),1,1)='[')";
const VALUE_LIMIT: usize = 32 * 1024 * 1024;

fn corrupt() -> ArchiveError {
    ArchiveError::Corrupt("invalid panorama event reference or source".into())
}

pub(crate) fn check(db: &Connection, version: i64) -> Result<()> {
    let marked: bool = db.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info('events') WHERE name='ori_sweep_ref_v1')",
        [],
        |r| r.get(0),
    )?;
    if marked != (version == VERSION) {
        return Err(corrupt());
    }
    if marked {
        let foreign_key: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM pragma_foreign_key_list('events')
             WHERE \"from\"='ori_sweep_ref_v1' AND \"table\"='sweeps' AND \"to\"='id')",
            [],
            |r| r.get(0),
        )?;
        if !foreign_key {
            return Err(corrupt());
        }
    }
    Ok(())
}

pub(crate) fn enabled(db: &Connection) -> Result<bool> {
    Ok(db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))? == VERSION)
}

pub(crate) fn select(db: &Connection) -> Result<&'static str> {
    Ok(if enabled(db)? { JOINED } else { INLINE })
}

pub(crate) fn digest(frequencies: &str, levels: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(frequencies.as_bytes());
    hash.update([0]);
    hash.update(levels.as_bytes());
    crate::backup::hex(&hash.finalize())
}

// The writer has just inserted this exact sweep in the same transaction.
pub(crate) fn encode(payload: &Value, hash: &str) -> Result<String> {
    let mut residual = serde_json::Map::new();
    for (key, value) in payload.as_object().ok_or_else(corrupt)? {
        if key == "sweep" {
            let sweep = value.as_object().ok_or_else(corrupt)?;
            let fields = sweep
                .iter()
                .filter(|(key, _)| !matches!(key.as_str(), "frequencies_hz" | "levels"))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            residual.insert(key.clone(), Value::Object(fields));
        } else {
            residual.insert(key.clone(), value.clone());
        }
    }
    Ok(serde_json::to_string(&(hash, residual))?)
}

pub(crate) fn payload(row: &Row<'_>) -> Result<Value> {
    let raw: String = row.get("payload_json")?;
    let reference: Option<String> = if row.as_ref().column_index("ori_sweep_ref_v1").is_ok() {
        row.get("ori_sweep_ref_v1")?
    } else {
        None
    };
    let Some(id) = reference else {
        return rows::object(raw);
    };
    let mut frame = parse_json_value(raw.as_bytes(), VALUE_LIMIT)?;
    let frame = frame
        .as_array_mut()
        .filter(|f| f.len() == 2)
        .ok_or_else(corrupt)?;
    let mut rest = frame[1].take();
    let sweep = rest
        .get_mut("sweep")
        .and_then(Value::as_object_mut)
        .ok_or_else(corrupt)?;
    let source_session = row
        .get::<_, Option<String>>("source_session")?
        .ok_or_else(corrupt)?;
    if row.get::<_, i64>("sequence")? < 1
        || row.get::<_, String>("event_type")? != "sweep.received"
        || row.get::<_, Option<String>>("session_id")?.as_ref() != Some(&source_session)
        || sweep.get("id").and_then(Value::as_str) != Some(id.as_str())
        || sweep.get("session_id").and_then(Value::as_str) != Some(source_session.as_str())
        || sweep.get("stream_id") != Some(&json!(row.get::<_, String>("source_stream")?))
        || sweep.get("level_unit") != Some(&json!(row.get::<_, String>("source_unit")?))
        || sweep.contains_key("frequencies_hz")
        || sweep.contains_key("levels")
    {
        return Err(corrupt());
    }
    let interval = sweep.get("interval").ok_or_else(corrupt)?;
    for (field, column) in [("started_at", "source_start"), ("ended_at", "source_end")] {
        let stamp = interval[field].as_str().ok_or_else(corrupt)?;
        if validate::utc(stamp)? != validate::utc(&row.get::<_, String>(column)?)? {
            return Err(corrupt());
        }
    }
    let hz: String = row.get("source_hz")?;
    let levels: String = row.get("source_levels")?;
    if frame[0].as_str() != Some(digest(&hz, &levels).as_str()) {
        return Err(corrupt());
    }
    let hz = parse_json_value(hz.as_bytes(), VALUE_LIMIT)?;
    let levels = parse_json_value(levels.as_bytes(), VALUE_LIMIT)?;
    let points = hz
        .as_array()
        .filter(|values| !values.is_empty())
        .ok_or_else(corrupt)?
        .len();
    if levels
        .as_array()
        .is_none_or(|values| values.len() != points)
    {
        return Err(corrupt());
    }
    sweep.insert("frequencies_hz".into(), hz);
    sweep.insert("levels".into(), levels);
    Ok(rest)
}

pub(crate) fn verify(db: &Connection) -> Result<()> {
    if enabled(db)? {
        let mut stmt = db.prepare(&format!("{JOINED} WHERE {REFERENCES}"))?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            payload(row)?;
        }
    }
    Ok(())
}

/// Called only on a private, consistent copy inside its caller's transaction.
pub(crate) fn materialize_copy(db: &Connection) -> Result<()> {
    if !enabled(db)? {
        return Ok(());
    }
    if db.query_row(
        "SELECT EXISTS(SELECT 1 FROM events WHERE sequence<=0)",
        [],
        |r| r.get::<_, bool>(0),
    )? {
        return Err(corrupt());
    }
    let mut after = 0_i64;
    {
        let sql =
            format!("{JOINED} WHERE e.sequence>? AND {REFERENCES} ORDER BY e.sequence LIMIT 1");
        let mut next = db.prepare(&sql)?;
        let mut update =
            db.prepare("UPDATE events SET payload_json=?, ori_sweep_ref_v1=NULL WHERE sequence=?")?;
        loop {
            let record = {
                let mut rows = next.query([after])?;
                let Some(row) = rows.next()? else { break };
                (
                    row.get::<_, i64>("sequence")?,
                    serde_json::to_string(&payload(row)?)?,
                )
            };
            after = record.0;
            update.execute(params![record.1, after])?;
        }
    }
    db.execute_batch("ALTER TABLE events DROP COLUMN ori_sweep_ref_v1")?;
    db.pragma_update(None, "user_version", crate::schema::MAIN_VERSION)?;
    Ok(())
}
