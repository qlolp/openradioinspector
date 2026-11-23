//! Indexed read model for committed RF Explorer Standard 10 kHz survey passes.
mod fixed_guard;
mod guard;
mod index;
mod panorama;
mod validate;
pub(crate) use index::{checkpoint, insert, install};

use self::panorama::{Marker, Panorama, Window};
use self::validate::{Plan, STEP};
use crate::{Archive, ArchiveError, Result, error::invalid, validate::utc, validate_limit};
use chrono::DateTime;
use rusqlite::{Connection, OptionalExtension, Row, params};
use serde_json::{Value, json};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

const MAX_COLUMNS: usize = 2048;
// Whole-band passes have at most 2,398 windows. Reject a long lookback as a
// whole if it exceeds its budget; a truncated Max Hold would be misleading.
const MAX_HOLD_WINDOWS: usize = 65_536;
const MAX_HOLD_PASSES: usize = 8192;
const SELECT: &str = "SELECT pass_key,pass_id,plan,signature,started_at,ended_at,received,unique_points,last_sequence,first_seen_at FROM ori_rfe_passes_v2";

struct Scope {
    session: String,
    stream: String,
}
impl Scope {
    fn new(session: &str, stream: &str) -> Self {
        Self {
            session: session.into(),
            stream: stream.into(),
        }
    }
}

struct Pass {
    key: i64,
    last_sequence: i64,
    first_seen: String,
    id: String,
    plan: Option<Plan>,
    signature: Option<Vec<u8>>,
    start: String,
    end: String,
    received: usize,
    unique: usize,
}
impl Pass {
    fn row(row: &Row<'_>) -> Result<Self> {
        Ok(Self {
            key: row.get(0)?,
            last_sequence: row.get(8)?,
            first_seen: row.get(9)?,
            id: row.get(1)?,
            plan: row
                .get::<_, Option<String>>(2)?
                .as_deref()
                .map(serde_json::from_str)
                .transpose()?,
            signature: row.get(3)?,
            start: row.get(4)?,
            end: row.get(5)?,
            received: row.get::<_, u32>(6)? as usize,
            unique: row.get::<_, u32>(7)? as usize,
        })
    }
    fn plan(&self) -> Result<&Plan> {
        self.plan
            .as_ref()
            .filter(|_| self.signature.is_some())
            .ok_or(ArchiveError::Conflict)
    }
    fn get(db: &Connection, scope: &Scope, id: &str) -> Result<Self> {
        let mut query = db.prepare(&format!(
            "{SELECT} WHERE session_id=? AND stream_id=? AND pass_id=?"
        ))?;
        let mut rows = query.query(params![scope.session, scope.stream, id])?;
        let pass = Self::row(rows.next()?.ok_or(ArchiveError::NotFound)?)?;
        pass.plan()?;
        Ok(pass)
    }
    fn before(&self, db: &Connection, scope: &Scope) -> Result<Option<Self>> {
        let mut query = db.prepare(&format!(
            "{SELECT} WHERE session_id=? AND stream_id=? AND
            (started_at,pass_key)<(?,?) ORDER BY started_at DESC,pass_key DESC LIMIT 1"
        ))?;
        let mut rows = query.query(params![scope.session, scope.stream, self.start, self.key])?;
        rows.next()?.map(Self::row).transpose()
    }
    fn follows(&self, db: &Connection, scope: &Scope, previous: &Pass) -> Result<bool> {
        if self.signature.is_none()
            || self.signature != previous.signature
            || previous.end > self.start
            || previous.last_sequence >= self.key
        {
            return Ok(false);
        }
        let (Some(plan), Some(earlier)) = (&self.plan, &previous.plan) else {
            return Ok(false);
        };
        // Production IDs end in "-<ordinal>". Only a shared, nonempty
        // acquisition instance proves continuity across two archived passes.
        let Some(instance) = self
            .id
            .strip_suffix(&format!("-{}", plan.pass))
            .filter(|prefix| !prefix.trim().is_empty())
        else {
            return Ok(false);
        };
        if earlier.pass.checked_add(1) != Some(plan.pass)
            || previous.id.strip_suffix(&format!("-{}", earlier.pass)) != Some(instance)
        {
            return Ok(false);
        }
        // A fixed window or malformed unidentifiable source between two passes
        // is also a boundary. The canonical ingestion index serves this check
        // without indexing every fixed window or loading unrelated arrays.
        let interrupted: bool = db.query_row(
            "SELECT EXISTS(SELECT 1 FROM sweep_ingestion WHERE session_id=? AND stream_id=? AND sequence>? AND sequence<?)",
            params![scope.session, scope.stream, previous.last_sequence, self.key],
            |row| row.get(0),
        )?;
        Ok(!interrupted)
    }
    fn summary(&self, db: &Connection, scope: &Scope, previous: Option<&Pass>) -> Result<Value> {
        let plan = self.plan()?;
        let complete = self.received == plan.tiles() && self.unique == plan.count();
        let mut summary = serde_json::to_value(plan)?;
        let object = summary.as_object_mut().expect("serialized plan");
        object.extend([
            ("pass_id".into(), json!(self.id)),
            ("tiles".into(), json!(plan.tiles())),
            ("received_tiles".into(), json!(self.received)),
            ("expected_points".into(), json!(plan.count())),
            ("step_hz".into(), json!(STEP)),
            ("unique_points".into(), json!(self.unique)),
            ("started_at".into(), json!(self.start)),
            ("ended_at".into(), json!(self.end)),
            ("complete".into(), json!(complete)),
            (
                "duration_s".into(),
                json!(complete.then(|| seconds(&self.start, &self.end)).flatten()),
            ),
            (
                "revisit_s".into(),
                json!(self.revisit(db, scope, previous)?),
            ),
        ]);
        Ok(summary)
    }
    fn revisit(
        &self,
        db: &Connection,
        scope: &Scope,
        previous: Option<&Pass>,
    ) -> Result<Option<f64>> {
        let Some(p) = previous else { return Ok(None) };
        if !self.follows(db, scope, p)? {
            return Ok(None);
        }
        let pair = db.prepare("SELECT p.started_at,c.started_at FROM ori_rfe_tiles_v2 c
            JOIN ori_rfe_tiles_v2 p ON p.pass_key=? AND p.tile=c.tile WHERE c.pass_key=? ORDER BY c.tile LIMIT 1")?
            .query_row(params![p.key,self.key], |r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?))).optional()?;
        Ok(pair.and_then(|(a, b)| seconds(&a, &b)))
    }
}
fn seconds(start: &str, end: &str) -> Option<f64> {
    let a = DateTime::parse_from_rfc3339(start).ok()?;
    let b = DateTime::parse_from_rfc3339(end).ok()?;
    Some((b - a).num_microseconds()? as f64 / 1_000_000.0)
}

fn cursor(before: Option<String>) -> Result<Option<(String, i64)>> {
    before
        .map(|token| {
            let (time, raw_key) = token
                .rsplit_once('|')
                .ok_or_else(|| invalid("RF Explorer pass cursor"))?;
            let key = raw_key
                .parse::<i64>()
                .map_err(|_| invalid("RF Explorer pass cursor"))?;
            if key <= 0 || key.to_string() != raw_key || utc(time)? != time {
                return Err(invalid("RF Explorer pass cursor"));
            }
            Ok((time.to_owned(), key))
        })
        .transpose()
}

impl Archive {
    pub async fn rf_explorer_passes(
        &self,
        session: &str,
        stream: &str,
        limit: usize,
        before: Option<String>,
    ) -> Result<Value> {
        validate_limit(limit, 100)?;
        let (scope, before) = (Scope::new(session, stream), cursor(before)?);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let boundary = if let Some((time, key)) = &before {
                    // The sort time is fixed at first ingestion. A late earlier
                    // tile can refine the observed interval without moving the cursor.
                    let issued: bool = db.query_row(
                        "SELECT EXISTS(SELECT 1 FROM ori_rfe_passes_v2
                         WHERE pass_key=? AND session_id=? AND stream_id=? AND first_seen_at=?)",
                        params![key, scope.session, scope.stream, time],
                        |row| row.get(0),
                    )?;
                    if !issued {
                        return Err(invalid("RF Explorer pass cursor scope or position"));
                    }
                    "AND (first_seen_at,pass_key)<(?4,?5)"
                } else {
                    ""
                };
                let mut query = db.prepare(&format!(
                    "{SELECT} WHERE session_id=?1 AND stream_id=?2 AND signature IS NOT NULL
                {boundary} ORDER BY first_seen_at DESC,pass_key DESC LIMIT ?3"
                ))?;
                let mut rows = if let Some((time, key)) = before {
                    query.query(params![
                        scope.session,
                        scope.stream,
                        limit as i64 + 1,
                        time,
                        key
                    ])?
                } else {
                    query.query(params![scope.session, scope.stream, limit as i64 + 1])?
                };
                let mut items = Vec::new();
                let mut last = None;
                let mut remaining = maximum;
                while let Some(row) = rows.next()? {
                    if items.len() == limit {
                        return Ok(json!({"items":items,"next_before":last}));
                    }
                    let pass = Pass::row(row)?;
                    remaining = remaining
                        .checked_sub(pass.id.len())
                        .ok_or(ArchiveError::QueryBudget)?;
                    let previous = pass.before(db, &scope)?;
                    items.push(pass.summary(db, &scope, previous.as_ref())?);
                    last = Some(format!("{}|{}", pass.first_seen, pass.key));
                }
                Ok(json!({"items":items,"next_before":null}))
            })
            .await
    }

    pub async fn rf_explorer_pass_windows(
        &self,
        session: &str,
        stream: &str,
        pass_id: &str,
        after_tile: usize,
        limit: usize,
    ) -> Result<Value> {
        validate_limit(limit, 256)?;
        let after = i64::try_from(after_tile).map_err(|_| invalid("RF Explorer window cursor"))?;
        let (scope, id) = (Scope::new(session, stream), pass_id.to_owned());
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let pass = Pass::get(db, &scope, &id)?;
                let mut query = db.prepare(
                    "SELECT tile,sweep_id,started_at,ended_at FROM ori_rfe_tiles_v2
                 WHERE pass_key=? AND tile>? ORDER BY tile LIMIT ?",
                )?;
                let mut rows = query.query(params![pass.key, after, limit as i64 + 1])?;
                let mut items = Vec::new();
                let mut last = None;
                let mut remaining = maximum;
                while let Some(row) = rows.next()? {
                    if items.len() == limit {
                        return Ok(json!({"items":items,"next_after_tile":last}));
                    }
                    let tile: i64 = row.get(0)?;
                    let sweep: String = row.get(1)?;
                    remaining = remaining
                        .checked_sub(sweep.len())
                        .ok_or(ArchiveError::QueryBudget)?;
                    let start: String = row.get(2)?;
                    let end: String = row.get(3)?;
                    items.push(
                        json!({"tile":tile,"sweep_id":sweep,"started_at":start,"ended_at":end}),
                    );
                    last = Some(tile);
                }
                Ok(json!({"items":items,"next_after_tile":null}))
            })
            .await
    }

    pub async fn rf_explorer_pass(
        &self,
        session: &str,
        stream: &str,
        pass_id: &str,
        columns: usize,
        hold_since: Option<String>,
    ) -> Result<Value> {
        validate_limit(columns, MAX_COLUMNS)?;
        let reset = hold_since.as_deref().map(utc).transpose()?;
        let (scope, id) = (Scope::new(session, stream), pass_id.to_owned());
        let timeout = self.inner.config.read_timeout;
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut work = Work::new(timeout, None);
                let pass = Pass::get(db, &scope, &id)?;
                let plan = pass.plan()?;
                let previous = pass.before(db, &scope)?;
                let summary = pass.summary(db, &scope, previous.as_ref())?;
                let mut current = Panorama::new(plan, columns);
                let mut hold = reset.as_ref().map(|_| Panorama::new(plan, columns));

                work.series(
                    db,
                    &scope,
                    &pass,
                    previous,
                    reset.as_deref(),
                    |trace, window, samples| match trace {
                        Trace::Current => current.add(plan, window, samples),
                        Trace::Held => hold
                            .as_mut()
                            .expect("requested Max Hold")
                            .add(plan, window, samples),
                    },
                )?;
                let mut budget = maximum;
                let (columns, coverage) = current.render(db, plan, &mut budget)?;
                let (hold_columns, hold_coverage) = hold
                    .map(|panorama| panorama.render(db, plan, &mut budget))
                    .transpose()?
                    .unzip();
                Ok(json!({
                    "summary": summary, "columns": columns, "coverage": coverage,
                    "hold_columns": hold_columns, "hold_coverage": hold_coverage
                }))
            })
            .await
    }

    pub async fn rf_explorer_point(
        &self,
        session: &str,
        stream: &str,
        pass_id: &str,
        frequency_hz: u64,
        hold_since: Option<String>,
    ) -> Result<Value> {
        let reset = hold_since.as_deref().map(utc).transpose()?;
        let (scope, id) = (Scope::new(session, stream), pass_id.to_owned());
        let timeout = self.inner.config.read_timeout;
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let pass = Pass::get(db, &scope, &id)?;
            let plan = pass.plan()?;
            if !(plan.start_hz..=plan.stop_hz).contains(&frequency_hz)
                || !(frequency_hz - plan.start_hz).is_multiple_of(STEP) {
                return Ok(json!({"current":null,"held":null}));
            }
            let index = ((frequency_hz - plan.start_hz) / STEP) as usize;
            let mut work = Work::new(timeout, Some(plan.source_tiles(index)));
            let previous = if reset.is_some() { pass.before(db, &scope)? } else { None };
            let mut current = Marker::new(index);
            let mut held = Marker::new(index);
            work.series(db, &scope, &pass, previous, reset.as_deref(), |trace, window, samples| {
                match trace {
                    Trace::Current => current.add(plan, window, samples),
                    Trace::Held => held.add(plan, window, samples),
                }
            })?;
            let mut budget = maximum;
            Ok(json!({"current":current.render(db,plan,&mut budget)?,"held":held.render(db,plan,&mut budget)?}))
        }).await
    }
}

enum Trace {
    Current,
    Held,
}

struct Work {
    tiles: Option<[usize; 2]>,
    started: Instant,
    timeout: Duration,
    windows: usize,
}
impl Work {
    fn new(timeout: Duration, tiles: Option<[usize; 2]>) -> Self {
        Self {
            tiles,
            started: Instant::now(),
            timeout,
            windows: 0,
        }
    }

    fn series(
        &mut self,
        db: &Connection,
        scope: &Scope,
        pass: &Pass,
        mut previous: Option<Pass>,
        reset: Option<&str>,
        mut add: impl FnMut(Trace, &Arc<Window>, &[u8]) -> Result<()>,
    ) -> Result<()> {
        self.visit(db, pass, |window, samples| {
            add(Trace::Current, window, samples)?;
            if reset.is_some_and(|since| window.start.as_str() >= since) {
                add(Trace::Held, window, samples)?;
            }
            Ok(())
        })?;
        let Some(since) = reset else { return Ok(()) };
        let mut newer = None;
        let mut count = 1;
        while let Some(older) = previous {
            if older.end.as_str() < since
                || !newer.as_ref().unwrap_or(pass).follows(db, scope, &older)?
            {
                break;
            }
            if count >= MAX_HOLD_PASSES {
                return Err(ArchiveError::QueryBudget);
            }
            self.visit(db, &older, |window, samples| {
                if window.start.as_str() >= since {
                    add(Trace::Held, window, samples)?;
                }
                Ok(())
            })?;
            count += 1;
            previous = older.before(db, scope)?;
            newer = Some(older);
        }
        Ok(())
    }
    fn visit(
        &mut self,
        db: &Connection,
        pass: &Pass,
        mut accept: impl FnMut(&Arc<Window>, &[u8]) -> Result<()>,
    ) -> Result<()> {
        if self.started.elapsed() > self.timeout
            || (self.tiles.is_none()
                && pass.received > MAX_HOLD_WINDOWS.saturating_sub(self.windows))
        {
            return Err(ArchiveError::QueryBudget);
        }
        const SOURCES: &str =
            "SELECT tile,started_at,ended_at,samples FROM ori_rfe_tiles_v2 WHERE pass_key=?1";
        let constraint = if self.tiles.is_some() {
            " AND tile IN (?2,?3)"
        } else {
            ""
        };
        let mut query = db.prepare(&format!("{SOURCES}{constraint} ORDER BY tile"))?;
        let mut rows = match self.tiles {
            Some([first, tail]) => query.query(params![pass.key, first as i64, tail as i64])?,
            None => query.query([pass.key])?,
        };
        while let Some(row) = rows.next()? {
            if self.started.elapsed() > self.timeout || self.windows >= MAX_HOLD_WINDOWS {
                return Err(ArchiveError::QueryBudget);
            }
            let window = Arc::new(Window {
                tile: row.get::<_, u32>(0)? as usize,
                pass_key: pass.key,
                start: row.get(1)?,
                end: row.get(2)?,
            });
            let samples = row
                .get_ref(3)?
                .as_blob()
                .map_err(|_| ArchiveError::Corrupt("RF Explorer tile samples".into()))?;
            accept(&window, samples)?;
            self.windows += 1;
        }
        Ok(())
    }
}
