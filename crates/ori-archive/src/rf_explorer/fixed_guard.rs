//! Compact references to eligible fixed-window originals. Earlier writers can
//! append sweeps without this index, so catch up from the ingestion watermark.
use super::*;
use crate::{BaselineSelection, rows};
use ori_monitoring::Sweep;

const DDL: &str = "
CREATE TABLE IF NOT EXISTS ori_rfe_fixed_sources_v1(
    sweep_id TEXT PRIMARY KEY REFERENCES sweeps(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL, stream_id TEXT NOT NULL, signature BLOB NOT NULL,
    start_hz INTEGER NOT NULL, stop_hz INTEGER NOT NULL, started_us INTEGER NOT NULL, ended_us INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ori_rfe_fixed_latest_v1 ON ori_rfe_fixed_sources_v1(session_id,stream_id,started_us,sweep_id);
CREATE INDEX IF NOT EXISTS ori_rfe_fixed_window_v1 ON ori_rfe_fixed_sources_v1(session_id,stream_id,start_hz,stop_hz,started_us,sweep_id);
CREATE INDEX IF NOT EXISTS ori_rfe_fixed_context_v1 ON ori_rfe_fixed_sources_v1(session_id,stream_id,signature,started_us,sweep_id);
CREATE TABLE IF NOT EXISTS ori_rfe_fixed_ingestion_v1(singleton INTEGER PRIMARY KEY CHECK(singleton=1),sequence INTEGER NOT NULL);
INSERT OR IGNORE INTO ori_rfe_fixed_ingestion_v1 VALUES(1,0);";

// Apply the original fixed-window shape bounds to the stored JSON in both
// entry points. Only a verified physical 112-point frame can reach the index.
const INPUT: &str = "s.level_unit='dBm'
    AND length(CAST(s.frequencies_json AS BLOB))<8192 AND length(CAST(s.levels_json AS BLOB))<8192
    AND length(CAST(s.settings_json AS BLOB))<65536
    AND CASE WHEN json_valid(s.settings_json) THEN json_extract(s.settings_json,'$.adapter')='rf-explorer-3g-serial'
        AND json_extract(s.settings_json,'$.detector')='normal' AND json_type(s.settings_json,'$.survey') IS NOT 'object' END";

fn index_row(db: &Connection, row: &Row<'_>) -> Result<()> {
    let raw = match rows::sweep(row) {
        Ok(raw) => raw,
        Err(
            ArchiveError::Invalid(_)
            | ArchiveError::Json(_)
            | ArchiveError::Corrupt(_)
            | ArchiveError::Sql(
                rusqlite::Error::InvalidColumnType(..)
                | rusqlite::Error::FromSqlConversionFailure(..),
            ),
        ) => return Ok(()),
        Err(error) => return Err(error),
    };
    let Ok(bytes) = ori_contracts::to_json_vec(&raw) else {
        return Ok(());
    };
    let Ok(sweep) = ori_contracts::from_json_slice::<Sweep>(&bytes, ori_monitoring::MAX_JSON_BYTES)
    else {
        return Ok(());
    };
    // fingerprint validates the complete hardware Normal readback, all 112
    // original frequencies and dBm samples, and the measurement settings.
    let Ok(signature) = ori_monitoring::fingerprint(&sweep) else {
        return Ok(());
    };
    db.execute(
        "INSERT INTO ori_rfe_fixed_sources_v1
        (sweep_id,session_id,stream_id,signature,start_hz,stop_hz,started_us,ended_us)
        VALUES(?1,?2,?3,unhex(?4),?5,?6,?7,?8)",
        params![
            sweep.id,
            sweep.session_id,
            sweep.stream_id,
            signature,
            raw["settings"]["start_hz"].as_i64(),
            raw["settings"]["stop_hz"].as_i64(),
            sweep.interval.started_at.unix_micros(),
            sweep.interval.ended_at.unix_micros()
        ],
    )?;
    Ok(())
}

pub(super) fn insert(db: &Connection, raw: &Value) -> Result<()> {
    let settings = &raw["settings"];
    if settings["adapter"] != "rf-explorer-3g-serial"
        || settings["detector"] != "normal"
        || settings["survey"].is_object()
    {
        return Ok(());
    }
    let mut statement = db.prepare(&format!(
        "SELECT s.* FROM sweeps s WHERE s.id=? AND {INPUT}"
    ))?;
    if let Some(row) = statement.query([raw["id"].as_str()])?.next()? {
        index_row(db, row)?;
    }
    Ok(())
}

pub(super) fn checkpoint(db: &Connection, sequence: i64) -> Result<()> {
    db.execute(
        "UPDATE ori_rfe_fixed_ingestion_v1 SET sequence=? WHERE singleton=1",
        [sequence],
    )?;
    Ok(())
}

pub(super) fn install(db: &Connection) -> Result<()> {
    db.execute_batch(DDL)?;
    let (after, boundary) = db.query_row(
        "SELECT sequence,(SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion) FROM ori_rfe_fixed_ingestion_v1 WHERE singleton=1",
        [], |row| Ok((row.get::<_,i64>(0)?,row.get::<_,i64>(1)?)))?;
    if after >= boundary {
        return Ok(());
    }
    // Filter before loading the raw arrays. Legacy archives may contain
    // unrelated, opaque spectra much larger than a Standard physical frame.
    let mut statement = db.prepare(&format!(
        "SELECT s.* FROM sweep_ingestion i CROSS JOIN sweeps s
        ON s.id=i.sweep_id AND s.session_id=i.session_id AND s.stream_id=i.stream_id
        WHERE i.sequence>?1 AND i.sequence<=?2 AND {INPUT} ORDER BY i.sequence"
    ))?;
    let mut cursor = statement.query(params![after, boundary])?;
    while let Some(row) = cursor.next()? {
        index_row(db, row)?;
    }
    checkpoint(db, boundary)
}

struct Source {
    id: String,
    start: i64,
    end: i64,
}
impl Source {
    fn from_row(row: &Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            id: row.get(0)?,
            start: row.get(1)?,
            end: row.get(2)?,
        })
    }
}

fn selection(sources: Vec<Source>) -> Result<BaselineSelection> {
    let time = |micros| {
        chrono::DateTime::from_timestamp_micros(micros)
            .map(|t| t.to_rfc3339_opts(chrono::SecondsFormat::Micros, true))
            .ok_or_else(|| invalid("RF Explorer source timestamp"))
    };
    let first = sources.first().map(|s| s.start);
    let last = sources.last().map(|s| s.end);
    let mut periods: Vec<_> = sources
        .windows(2)
        .map(|p| p[1].start - p[0].start)
        .collect();
    periods.sort_unstable();
    let middle = periods.len() / 2;
    let cadence = periods.get(middle).map(|&p| {
        if periods.len().is_multiple_of(2) {
            (periods[middle - 1] as f64 + p as f64) / 2_000_000.
        } else {
            p as f64 / 1_000_000.
        }
    });
    Ok(BaselineSelection {
        count: sources.len() as u64,
        sweep_ids: sources.into_iter().map(|s| s.id).collect(),
        first_started_at: first.map(time).transpose()?,
        last_ended_at: last.map(time).transpose()?,
        elapsed_seconds: first
            .zip(last)
            .map_or(0., |(start, end)| (end - start) as f64 / 1_000_000.),
        sweep_period_seconds: cadence,
    })
}

const CURRENT_CONTEXT: &str = "SELECT signature FROM ori_rfe_fixed_sources_v1
    WHERE session_id=?1 AND stream_id=?2 AND (?3 IS NULL OR start_hz=?3) AND (?4 IS NULL OR stop_hz=?4)
    ORDER BY started_us DESC,sweep_id DESC LIMIT 1";
const CURRENT_WINDOW: &str = "SELECT signature FROM ori_rfe_fixed_sources_v1
    WHERE session_id=?1 AND stream_id=?2 AND start_hz=?3 AND stop_hz=?4
    ORDER BY started_us DESC,sweep_id DESC LIMIT 1";
const PREVIOUS_SOURCE: &str = "SELECT sweep_id,started_us,ended_us FROM ori_rfe_fixed_sources_v1
    WHERE session_id=?1 AND stream_id=?2 AND signature=?3 AND started_us<?4 AND ended_us<=?4
    ORDER BY started_us DESC,sweep_id DESC LIMIT 1";
// Seek to the first independent timestamp, then its greatest eligible original
// ID. Both seeks use the same index even if thousands of originals share a time.
const FIRST_SOURCE: &str = "SELECT sweep_id,started_us,ended_us FROM ori_rfe_fixed_sources_v1
    WHERE session_id=?1 AND stream_id=?2 AND signature=?3 AND ended_us<=?4
    AND started_us=(SELECT started_us FROM ori_rfe_fixed_sources_v1 INDEXED BY ori_rfe_fixed_context_v1
        WHERE session_id=?1 AND stream_id=?2 AND signature=?3 AND started_us>?5 AND started_us>=?6
        AND started_us<?4 AND ended_us<=?4 ORDER BY started_us LIMIT 1)
    ORDER BY sweep_id DESC LIMIT 1";

impl Archive {
    pub async fn rfe_fixed_guard_selection(
        &self,
        session: &str,
        stream: &str,
        start_hz: Option<i64>,
        stop_hz: Option<i64>,
    ) -> Result<BaselineSelection> {
        let scope = Scope::new(session, stream);
        let mut remaining = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let current_context = if start_hz.is_some() && stop_hz.is_some() {
                    CURRENT_WINDOW
                } else {
                    CURRENT_CONTEXT
                };
                let signature: Option<Vec<u8>> = db
                    .query_row(
                        current_context,
                        params![scope.session, scope.stream, start_hz, stop_hz],
                        |row| row.get(0),
                    )
                    .optional()?;
                let Some(signature) = signature else {
                    return selection(Vec::new());
                };

                let mut latest = db.prepare(PREVIOUS_SOURCE)?;
                let mut recent = Vec::with_capacity(128);
                let mut before = i64::MAX;
                while recent.len() < 128 {
                    let next = latest
                        .query_row(
                            params![scope.session, scope.stream, signature, before],
                            Source::from_row,
                        )
                        .optional()?;
                    let Some(next) = next else { break };
                    remaining -= crate::actor::check_response_size(&next.id, remaining)?;
                    before = next.start;
                    recent.push(next);
                }
                recent.reverse();
                if recent.len() < 128 {
                    return selection(recent);
                }

                let mut earliest = db.prepare(FIRST_SOURCE)?;
                let mut sources = Vec::with_capacity(192);
                let (mut last_start, mut last_end) = (i64::MIN, i64::MIN);
                while sources.len() < 64 {
                    let next = earliest
                        .query_row(
                            params![
                                scope.session,
                                scope.stream,
                                signature,
                                before,
                                last_start,
                                last_end
                            ],
                            Source::from_row,
                        )
                        .optional()?;
                    let Some(next) = next else { break };
                    remaining -= crate::actor::check_response_size(&next.id, remaining)?;
                    (last_start, last_end) = (next.start, next.end);
                    sources.push(next);
                }
                sources.extend(recent);
                selection(sources)
            })
            .await
    }
}
