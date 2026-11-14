use crate::{ArchiveError, Result};
use rusqlite::{Connection, OptionalExtension};
use serde_json::Value;

const DDL: &str = "CREATE TABLE IF NOT EXISTS ori_candidate_precision_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_candidate_precision_schema VALUES(1,1);
CREATE TABLE IF NOT EXISTS ori_candidate_scalar_bits(candidate_id TEXT PRIMARY KEY REFERENCES candidates(id) ON DELETE CASCADE,
peak_level_bits_le BLOB CHECK(peak_level_bits_le IS NULL OR length(peak_level_bits_le)=8),
score_bits_le BLOB CHECK(score_bits_le IS NULL OR length(score_bits_le)=8),
CHECK(peak_level_bits_le IS NOT NULL OR score_bits_le IS NOT NULL));";

fn exists(db: &Connection, name: &str) -> Result<bool> {
    Ok(db
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            [name],
            |r| r.get::<_, i64>(0),
        )
        .optional()?
        .is_some())
}
pub(crate) fn check(db: &Connection) -> Result<()> {
    let metadata = exists(db, "ori_candidate_precision_schema")?;
    let supplement = exists(db, "ori_candidate_scalar_bits")?;
    if metadata != supplement {
        return Err(ArchiveError::Corrupt(
            "incomplete candidate precision extension".into(),
        ));
    }
    if !metadata {
        return Ok(());
    }
    let rows = db
        .prepare("SELECT singleton,version FROM ori_candidate_precision_schema LIMIT 2")?
        .query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    if rows.len() != 1 || rows[0].0 != 1 || rows[0].1 < 1 {
        return Err(ArchiveError::Corrupt("candidate precision metadata".into()));
    }
    if rows[0].1 != 1 {
        return Err(ArchiveError::UnsupportedSchema {
            domain: "candidate_precision",
            version: rows[0].1,
        });
    }
    Ok(())
}
pub(crate) fn install(db: &Connection) -> Result<()> {
    db.execute_batch(DDL)?;
    Ok(())
}

fn encode(value: &Value, field: &str) -> Result<Option<Vec<u8>>> {
    match value.get(field).filter(|v| !v.is_null()) {
        None => Ok(None),
        Some(value) => value
            .as_f64()
            .filter(|number| number.is_finite())
            .map(|number| Some(number.to_le_bytes().to_vec()))
            .ok_or_else(|| ArchiveError::Invalid("candidate scalar precision".into())),
    }
}
pub(crate) fn save(db: &Connection, value: &Value) -> Result<()> {
    let peak = encode(value, "peak_level")?;
    let score = encode(value, "score")?;
    let id = value["id"]
        .as_str()
        .ok_or_else(|| ArchiveError::Invalid("candidate identity".into()))?;
    if peak.is_none() && score.is_none() {
        db.execute(
            "DELETE FROM ori_candidate_scalar_bits WHERE candidate_id=?",
            [id],
        )?;
    } else {
        db.execute("INSERT INTO ori_candidate_scalar_bits VALUES(?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET peak_level_bits_le=excluded.peak_level_bits_le,score_bits_le=excluded.score_bits_le",rusqlite::params![id,peak,score])?;
    }
    Ok(())
}
pub(crate) fn decode(stored: Option<f64>, bytes: Option<Vec<u8>>) -> Result<Option<f64>> {
    if stored.is_some_and(|value| !value.is_finite()) {
        return Err(ArchiveError::Corrupt(
            "nonfinite candidate scalar REAL".into(),
        ));
    }
    let Some(bytes) = bytes else {
        return Ok(stored);
    };
    let array: [u8; 8] = bytes
        .try_into()
        .map_err(|_| ArchiveError::Corrupt("candidate precision length".into()))?;
    let value = f64::from_le_bytes(array);
    if !value.is_finite() || stored != Some(value) {
        return Err(ArchiveError::Corrupt("candidate precision mismatch".into()));
    }
    Ok(Some(value))
}
pub(crate) fn validate_rows(db: &Connection) -> Result<()> {
    check(db)?;
    if !exists(db, "ori_candidate_scalar_bits")? {
        return Ok(());
    }
    let mut statement=db.prepare("SELECT c.peak_level,c.score,p.peak_level_bits_le,p.score_bits_le FROM ori_candidate_scalar_bits p LEFT JOIN candidates c ON c.id=p.candidate_id")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        decode(row.get(0)?, row.get(2)?)?;
        decode(row.get(1)?, row.get(3)?)?;
    }
    Ok(())
}
