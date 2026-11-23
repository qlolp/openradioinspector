//! A rebuildable index of proven survey windows. Its byte per point is the
//! lossless Standard encoding of the canonical level, never an averaged trace.
use super::validate::{POINTS, Tile, tile};
use crate::{
    Result,
    validate::{text, utc},
};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{
    Deserializer,
    de::{Error, SeqAccess, Visitor},
};
use serde_json::{Number, Value, value::RawValue};
use std::fmt;

// V1 hashed the insertion order of nested JSON objects. Its checkpoint may
// already be past a falsely quarantined pass. Separate tables and watermarks
// let either generation reopen and catch up from immutable sweep_ingestion.
const DDL: &str = "
CREATE TABLE IF NOT EXISTS ori_rfe_passes_v2(
    pass_key INTEGER PRIMARY KEY, session_id TEXT NOT NULL, stream_id TEXT NOT NULL,
    pass_id TEXT NOT NULL, last_sequence INTEGER NOT NULL, first_seen_at TEXT NOT NULL, plan TEXT, signature BLOB, started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
    received INTEGER NOT NULL DEFAULT 0, unique_points INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id,stream_id,pass_id),
    FOREIGN KEY(stream_id,session_id) REFERENCES streams(id,session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ori_rfe_history_v2 ON ori_rfe_passes_v2(session_id,stream_id,started_at,pass_key);
CREATE INDEX IF NOT EXISTS ori_rfe_valid_history_v2 ON ori_rfe_passes_v2(session_id,stream_id,first_seen_at,pass_key)
    WHERE signature IS NOT NULL;
CREATE TABLE IF NOT EXISTS ori_rfe_tiles_v2(
    pass_key INTEGER NOT NULL REFERENCES ori_rfe_passes_v2(pass_key) ON DELETE CASCADE, tile INTEGER NOT NULL,
    sweep_id TEXT NOT NULL UNIQUE REFERENCES sweeps(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, samples BLOB NOT NULL CHECK(length(samples)=112),
    PRIMARY KEY(pass_key,tile)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS ori_rfe_ingestion_v2(singleton INTEGER PRIMARY KEY CHECK(singleton=1), sequence INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_rfe_ingestion_v2 VALUES(1,0);";

pub(crate) fn checkpoint(db: &Connection, sequence: i64) -> Result<()> {
    db.execute(
        "UPDATE ori_rfe_ingestion_v2 SET sequence=? WHERE singleton=1",
        [sequence],
    )?;
    super::fixed_guard::checkpoint(db, sequence)
}

struct Source<'a> {
    sequence: i64,
    session: &'a str,
    stream: &'a str,
    pass: &'a str,
    sweep: &'a str,
    start: &'a str,
    end: &'a str,
}

fn record(db: &Connection, source: Source<'_>, tile: Option<Tile>) -> Result<()> {
    let plan = tile
        .as_ref()
        .map(|t| serde_json::to_string(&t.plan))
        .transpose()?;
    let signature = tile.as_ref().map(|t| t.signature.as_slice());
    // A NULL signature permanently quarantines a malformed or mixed pass.
    // Ordinary canonical archival and access by raw sweep ID remain available.
    let (key,valid) = db.prepare("INSERT INTO ori_rfe_passes_v2(pass_key,session_id,stream_id,pass_id,plan,signature,started_at,ended_at,last_sequence,first_seen_at)
        VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?1,?7)
        ON CONFLICT(session_id,stream_id,pass_id) DO UPDATE SET
            last_sequence=MAX(last_sequence,excluded.last_sequence),
            signature=CASE WHEN signature=excluded.signature AND plan=excluded.plan THEN signature END,
            started_at=MIN(started_at,excluded.started_at),ended_at=MAX(ended_at,excluded.ended_at)
        RETURNING pass_key,signature IS NOT NULL")?
        .query_row(params![source.sequence,source.session,source.stream,source.pass,plan,signature,source.start,source.end],
            |r| Ok((r.get::<_,i64>(0)?,r.get::<_,bool>(1)?)))?;
    let Some(tile) = tile.filter(|_| valid) else {
        return Ok(());
    };
    let num = tile.number as i64;
    let previous = db.prepare("SELECT tile,ended_at FROM ori_rfe_tiles_v2 WHERE pass_key=? AND tile<=? ORDER BY tile DESC LIMIT 1")?
        .query_row(params![key,num], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?))).optional()?;
    let next = db.prepare("SELECT tile,started_at FROM ori_rfe_tiles_v2 WHERE pass_key=? AND tile>? ORDER BY tile LIMIT 1")?
        .query_row(params![key,num], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?))).optional()?;
    if previous
        .as_ref()
        .is_some_and(|(n, t)| *n == num || t.as_str() > source.start)
        || next.as_ref().is_some_and(|(_, t)| t.as_str() < source.end)
    {
        db.execute(
            "UPDATE ori_rfe_passes_v2 SET signature=NULL WHERE pass_key=?",
            [key],
        )?;
        return Ok(());
    }
    // Only the shifted last physical tile can overlap its predecessor.
    let last = tile.plan.tiles() as i64;
    let meets_tail = (num == last && previous.is_some_and(|(n, _)| n == last - 1))
        || (num == last - 1 && next.is_some_and(|(n, _)| n == last));
    let overlap = if meets_tail {
        tile.plan.tiles() * POINTS - tile.plan.count()
    } else {
        0
    };
    db.prepare("INSERT INTO ori_rfe_tiles_v2(pass_key,tile,sweep_id,started_at,ended_at,samples) VALUES(?,?,?,?,?,?)")?
        .execute(params![key,num,source.sweep,source.start,source.end,tile.samples.as_slice()])?;
    db.prepare("UPDATE ori_rfe_passes_v2 SET received=received+1,unique_points=unique_points+? WHERE pass_key=?")?
        .execute(params![(POINTS-overlap) as i64,key])?;
    Ok(())
}

pub(crate) fn insert(db: &Connection, sequence: i64, raw: &Value) -> Result<()> {
    super::fixed_guard::insert(db, raw)?;
    let settings = &raw["settings"];
    let Some(pass) = settings["survey"]["pass_id"]
        .as_str()
        .filter(|s| !s.trim().is_empty())
    else {
        return Ok(());
    };
    // The canonical writer has already validated IDs, interval and ownership.
    let window = tile(
        settings,
        &raw["frequencies_hz"],
        &raw["levels"],
        raw["level_unit"].as_str().unwrap_or_default(),
    );
    record(
        db,
        Source {
            sequence,
            session: text(raw, "session_id")?,
            stream: text(raw, "stream_id")?,
            sweep: text(raw, "id")?,
            pass,
            start: text(&raw["interval"], "started_at")?,
            end: text(&raw["interval"], "ended_at")?,
        },
        window,
    )
}

// A historical record can claim a survey while carrying an arbitrary large
// spectrum. Read exactly one Standard frame before allocating JSON values.
// serde_json verifies the closing bracket and end of input after the visitor.
fn archived_points(raw: &str) -> Option<Value> {
    struct Frame;
    impl<'de> Visitor<'de> for Frame {
        type Value = Value;
        fn expecting(&self, format: &mut fmt::Formatter<'_>) -> fmt::Result {
            format.write_str("112 numeric RF Explorer points")
        }
        fn visit_seq<A: SeqAccess<'de>>(
            self,
            mut array: A,
        ) -> std::result::Result<Value, A::Error> {
            let mut values = Vec::with_capacity(POINTS);
            for i in 0..POINTS {
                let token: &RawValue = array
                    .next_element()?
                    .ok_or_else(|| A::Error::invalid_length(i, &self))?;
                let number = token.get().parse::<Number>().map_err(A::Error::custom)?;
                values.push(Value::Number(number));
            }
            Ok(Value::Array(values))
        }
    }
    let mut decoder = serde_json::Deserializer::from_str(raw);
    let frame = decoder.deserialize_seq(Frame).ok()?;
    decoder.end().ok()?;
    Some(frame)
}

pub(crate) fn install(db: &Connection) -> Result<()> {
    super::fixed_guard::install(db)?;
    db.execute_batch(DDL)?;
    let after: i64 = db.query_row(
        "SELECT sequence FROM ori_rfe_ingestion_v2 WHERE singleton=1",
        [],
        |r| r.get(0),
    )?;
    let boundary: i64 = db.query_row(
        "SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion",
        [],
        |r| r.get(0),
    )?;
    if after >= boundary {
        return Ok(());
    }
    // Older Rust and Python writers commit to sweep_ingestion as well. Read
    // settings to identify candidates; never decode unrelated raw arrays or
    // depend on the stream's *current*, mutable device settings. The cursor
    // holds one source at a time and advances atomically with the derived rows.
    let mut query = db.prepare("SELECT i.sequence,i.session_id,i.stream_id,i.sweep_id,
        s.started_at,s.ended_at,s.level_unit,json_extract(s.settings_json,'$.survey.pass_id'),
        s.settings_json,s.frequencies_json,s.levels_json
        FROM sweep_ingestion i CROSS JOIN sweeps s ON s.id=i.sweep_id AND s.session_id=i.session_id AND s.stream_id=i.stream_id
        WHERE i.sequence>? AND i.sequence<=? AND
          CASE WHEN json_valid(s.settings_json) THEN json_type(s.settings_json,'$.survey.pass_id') END='text'
        ORDER BY i.sequence")?;
    let mut rows = query.query(params![after, boundary])?;
    while let Some(row) = rows.next()? {
        let pass: String = row.get(7)?;
        if pass.trim().is_empty() {
            continue;
        }
        let start = utc(&row.get::<_, String>(4)?);
        let end = utc(&row.get::<_, String>(5)?);
        let window = if matches!((&start,&end),(Ok(a),Ok(b)) if b>=a) {
            let archived = |column| {
                row.get_ref(column)
                    .ok()
                    .and_then(|field| field.as_str().ok())
            };
            let settings = archived(8)
                .and_then(|raw| crate::parse_json_value(raw.as_bytes(), 16 * 1024 * 1024).ok());
            match (
                settings,
                archived(9).and_then(archived_points),
                archived(10).and_then(archived_points),
            ) {
                (Some(settings), Some(hz), Some(levels)) => {
                    tile(&settings, &hz, &levels, &row.get::<_, String>(6)?)
                }
                _ => None,
            }
        } else {
            None
        };
        record(
            db,
            Source {
                sequence: row.get(0)?,
                session: &row.get::<_, String>(1)?,
                stream: &row.get::<_, String>(2)?,
                sweep: &row.get::<_, String>(3)?,
                pass: &pass,
                start: &start.unwrap_or_default(),
                end: &end.unwrap_or_default(),
            },
            window,
        )?;
    }
    checkpoint(db, boundary)
}
