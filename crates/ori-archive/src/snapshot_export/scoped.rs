//! Selected original records under the existing live-export lifecycle.
use super::{
    Failure, Launch, Query, Ready, Record, Reply, Shared, SnapshotExport, SnapshotExportLimits,
    Source, bounded, deadline,
};
use crate::{Archive, ArchiveError, Result, error::invalid, rows, schema, validate};
use chrono::{DateTime, Datelike, SecondsFormat, Utc};
use rusqlite::{
    Connection, OptionalExtension, functions::FunctionFlags, limits::Limit, params_from_iter,
    types::Value as SqlValue,
};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    sync::{Arc, atomic::Ordering, mpsc},
    time::Instant,
};

#[derive(Clone, Debug, Default)]
pub struct ScopedSnapshotQuery {
    pub session_id: Option<String>,
    pub stream_id: Option<String>,
    pub started_at_us: Option<i64>,
    pub ended_at_us: Option<i64>,
    pub min_frequency_hz: Option<i64>,
    pub max_frequency_hz: Option<i64>,
}
impl ScopedSnapshotQuery {
    pub(super) fn valid(&self) -> bool {
        !self
            .session_id
            .as_deref()
            .is_some_and(|s| s.trim().is_empty())
            && !self
                .stream_id
                .as_deref()
                .is_some_and(|s| s.trim().is_empty())
            && !matches!((self.started_at_us,self.ended_at_us),(Some(a),Some(b)) if a>=b)
            && !self.min_frequency_hz.is_some_and(|v| v < 0)
            && !self.max_frequency_hz.is_some_and(|v| v < 0)
            && !matches!((self.min_frequency_hz,self.max_frequency_hz),(Some(a),Some(b)) if a>b)
    }
}
#[derive(Clone, Debug, Serialize)]
pub struct ScopedSnapshotInfo {
    pub boundary: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ScopedSnapshotKind {
    Stream,
    Sweep,
    Observation,
}
impl ScopedSnapshotKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stream => "stream",
            Self::Sweep => "sweep",
            Self::Observation => "observation",
        }
    }
}
#[derive(Debug)]
pub struct ScopedSnapshotRecord {
    pub kind: ScopedSnapshotKind,
    pub value: Value,
}

/// Shares the same two Archive slots, reaper, cancellation and sticky JOIN result
/// as SnapshotExport. Call close before releasing admission or derived storage.
#[derive(Clone)]
pub struct ScopedSnapshotExport(SnapshotExport);
impl ScopedSnapshotExport {
    pub fn open_live(
        archive: Archive,
        query: ScopedSnapshotQuery,
        limits: SnapshotExportLimits,
    ) -> Result<Self> {
        let source = Source::Live(archive.inner.live_export_path.clone());
        SnapshotExport::launch(
            source,
            Some(archive),
            Query::Scoped(query),
            limits,
            Launch::default(),
        )
        .map(Self)
    }
    pub async fn ready(&self) -> Result<ScopedSnapshotInfo> {
        match self.0.ready_value().await? {
            Ready::Scoped(info) => Ok(info),
            Ready::Legacy(_) => Err(ArchiveError::WorkerStopped),
        }
    }
    pub async fn next_record(&self) -> Result<Option<ScopedSnapshotRecord>> {
        self.0
            .next_value()
            .await?
            .map(|record| match record {
                Record::Scoped(record) => Ok(record),
                Record::Legacy(_) => Err(ArchiveError::WorkerStopped),
            })
            .transpose()
    }
    pub async fn close(&self) -> Result<()> {
        self.0.close().await
    }
}

fn check(shared: &Shared) -> Result<()> {
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
fn add(
    clauses: &mut Vec<String>,
    parameters: &mut Vec<SqlValue>,
    field: &str,
    operator: &str,
    value: SqlValue,
) {
    clauses.push(format!("{field}{operator}?"));
    parameters.push(value);
}
fn scope(
    query: &ScopedSnapshotQuery,
    alias: &str,
    stream_key: &str,
) -> (Vec<String>, Vec<SqlValue>) {
    let (mut clauses, mut parameters) = (Vec::new(), Vec::new());
    if let Some(id) = &query.session_id {
        add(
            &mut clauses,
            &mut parameters,
            &format!("{alias}.session_id"),
            "=",
            id.clone().into(),
        );
    }
    if let Some(id) = &query.stream_id {
        add(
            &mut clauses,
            &mut parameters,
            &format!("{alias}.{stream_key}"),
            "=",
            id.clone().into(),
        );
    }
    (clauses, parameters)
}
fn where_sql(clauses: &[String]) -> String {
    if clauses.is_empty() {
        String::new()
    } else {
        format!(" WHERE {}", clauses.join(" AND "))
    }
}
fn time_filters(
    query: &ScopedSnapshotQuery,
    field: &str,
    coarse: Option<&str>,
    clauses: &mut Vec<String>,
    parameters: &mut Vec<SqlValue>,
) {
    for (bound, operator) in [(query.started_at_us, ">="), (query.ended_at_us, "<")] {
        if let Some(bound) = bound {
            if let Some(coarse) = coarse
                && let Some(instant) = DateTime::<Utc>::from_timestamp_micros(bound)
                && (0..=9999).contains(&instant.year())
            {
                add(
                    clauses,
                    parameters,
                    coarse,
                    operator,
                    instant.to_rfc3339_opts(SecondsFormat::Micros, true).into(),
                );
            }
            add(
                clauses,
                parameters,
                &format!("scoped_epoch({field})"),
                operator,
                bound.into(),
            );
        }
    }
}
fn stream(row: &rusqlite::Row<'_>) -> Result<Value> {
    let capabilities: String = row.get("capabilities_json")?;
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,
        "device":row.get::<_,String>("device")?,"role":row.get::<_,String>("role")?,
        "level_unit":row.get::<_,String>("level_unit")?,"label":row.get::<_,String>("label")?,
        "port":row.get::<_,Option<String>>("port")?,"model":row.get::<_,Option<String>>("model")?,
        "serial_number":row.get::<_,Option<String>>("serial_number")?,"firmware":row.get::<_,Option<String>>("firmware")?,
        "state":row.get::<_,String>("state")?,"settings":rows::object(row.get("settings_json")?)?,
        "capabilities":crate::parse_json_value(capabilities.as_bytes(),super::MAX_RECORD)?}),
    )
}

pub(super) fn read_snapshot(
    db: &mut Connection,
    query: ScopedSnapshotQuery,
    limits: SnapshotExportLimits,
    shared: &Arc<Shared>,
    receiver: mpsc::Receiver<Reply>,
) -> Result<()> {
    let state = shared.clone();
    db.progress_handler(1000, Some(move || check(&state).is_err()))?;
    db.busy_timeout(limits.read_timeout)?;
    deadline(limits, shared)?;
    check(shared)?;
    db.execute_batch("PRAGMA query_only=ON; PRAGMA cache_size=-4096; PRAGMA temp_store=FILE; PRAGMA mmap_size=0; PRAGMA trusted_schema=OFF; BEGIN")?;
    schema::functions(db)?;
    db.create_scalar_function(
        "scoped_epoch",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |context| {
            let raw = context.get::<String>(0)?;
            let normalized =
                validate::utc(&raw).map_err(|e| rusqlite::Error::UserFunctionError(Box::new(e)))?;
            DateTime::parse_from_rfc3339(&normalized)
                .map(|v| v.timestamp_micros())
                .map_err(|e| rusqlite::Error::UserFunctionError(Box::new(e)))
        },
    )?;
    schema::versions(db)?;
    if let Some(session) = &query.session_id
        && !rows::exists(db, "sessions", session)?
    {
        return Err(ArchiveError::NotFound);
    }
    if let Some(id) = &query.stream_id {
        let owner: Option<String> = db
            .query_row("SELECT session_id FROM streams WHERE id=?", [id], |r| {
                r.get(0)
            })
            .optional()?;
        if owner.is_none()
            || query
                .session_id
                .as_ref()
                .is_some_and(|id| Some(id) != owner.as_ref())
        {
            return Err(invalid(
                "scoped snapshot stream does not belong to the selected session",
            ));
        }
    }
    let info = ScopedSnapshotInfo {
        boundary: db.query_row("SELECT COALESCE(MAX(sequence),0) FROM events", [], |r| {
            r.get::<_, i64>(0)
        })? as u64,
    };
    let (stream_clauses, stream_parameters) = scope(&query, "s", "id");
    let (mut sweep_clauses, mut sweep_parameters) = scope(&query, "s", "stream_id");
    time_filters(
        &query,
        "i.started_at",
        Some("i.started_at"),
        &mut sweep_clauses,
        &mut sweep_parameters,
    );
    if let Some(lower) = query.min_frequency_hz {
        add(
            &mut sweep_clauses,
            &mut sweep_parameters,
            "i.max_hz",
            ">=",
            lower.into(),
        );
    }
    if let Some(upper) = query.max_frequency_hz {
        add(
            &mut sweep_clauses,
            &mut sweep_parameters,
            "i.min_hz",
            "<=",
            upper.into(),
        );
    }
    let (mut observation_clauses, mut observation_parameters) = scope(&query, "o", "stream_id");
    time_filters(
        &query,
        "o.started_at",
        None,
        &mut observation_clauses,
        &mut observation_parameters,
    );
    if let Some(lower) = query.min_frequency_hz {
        add(
            &mut observation_clauses,
            &mut observation_parameters,
            "o.frequency_hz",
            ">=",
            lower.into(),
        );
    }
    if let Some(upper) = query.max_frequency_hz {
        add(
            &mut observation_clauses,
            &mut observation_parameters,
            "o.frequency_hz",
            "<=",
            upper.into(),
        );
    }
    let mut streams = db.prepare(&format!(
        "SELECT s.* FROM streams s{} ORDER BY s.id COLLATE BINARY",
        where_sql(&stream_clauses)
    ))?;
    let mut sweeps=db.prepare(&format!("SELECT s.id FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id{} ORDER BY s.id COLLATE BINARY",where_sql(&sweep_clauses)))?;
    let mut observations = db.prepare(&format!(
        "SELECT o.id FROM observations o{} ORDER BY o.id COLLATE BINARY",
        where_sql(&observation_clauses)
    ))?;
    let mut stream_rows = streams.query(params_from_iter(stream_parameters.iter()))?;
    let mut sweep_rows = sweeps.query(params_from_iter(sweep_parameters.iter()))?;
    let mut observation_rows =
        observations.query(params_from_iter(observation_parameters.iter()))?;
    db.set_limit(Limit::SQLITE_LIMIT_LENGTH, limits.max_record_bytes as i32)?;
    check(shared)?;
    shared.publish(Ok(Ready::Scoped(info)));
    let (mut stream_done, mut sweep_done, mut failed) = (false, false, None);
    while let Ok(reply) = receiver.recv() {
        if shared.cancelled.load(Ordering::Acquire) {
            let _ = reply.send(Err(ArchiveError::Closed));
            continue;
        }
        if let Some(failure) = failed {
            let _ = reply.send(Err(Failure::error(failure)));
            continue;
        }
        deadline(limits, shared)?;
        let value = (|| -> Result<Option<ScopedSnapshotRecord>> {
            check(shared)?;
            if !stream_done {
                if let Some(row) = stream_rows.next()? {
                    let value = stream(row)?;
                    bounded(&value, limits.max_record_bytes)?;
                    check(shared)?;
                    return Ok(Some(ScopedSnapshotRecord {
                        kind: ScopedSnapshotKind::Stream,
                        value,
                    }));
                }
                stream_done = true;
            }
            if !sweep_done {
                while let Some(row) = sweep_rows.next()? {
                    check(shared)?;
                    let value = rows::get_sweep(db, &row.get::<_, String>(0)?)?
                        .ok_or(ArchiveError::NotFound)?;
                    if query.min_frequency_hz.is_some() || query.max_frequency_hz.is_some() {
                        let selected = value["frequencies_hz"]
                            .as_array()
                            .ok_or_else(|| ArchiveError::Corrupt("scoped sweep array".into()))?
                            .iter()
                            .any(|hz| {
                                hz.as_i64().is_some_and(|hz| {
                                    query.min_frequency_hz.is_none_or(|low| hz >= low)
                                        && query.max_frequency_hz.is_none_or(|high| hz <= high)
                                })
                            });
                        if !selected {
                            check(shared)?;
                            continue;
                        }
                    }
                    bounded(&value, limits.max_record_bytes)?;
                    check(shared)?;
                    return Ok(Some(ScopedSnapshotRecord {
                        kind: ScopedSnapshotKind::Sweep,
                        value,
                    }));
                }
                sweep_done = true;
            }
            let Some(row) = observation_rows.next()? else {
                check(shared)?;
                return Ok(None);
            };
            let value = rows::get_observation(db, &row.get::<_, String>(0)?)?
                .ok_or(ArchiveError::NotFound)?;
            bounded(&value, limits.max_record_bytes)?;
            check(shared)?;
            Ok(Some(ScopedSnapshotRecord {
                kind: ScopedSnapshotKind::Observation,
                value,
            }))
        })();
        let value = value.map(|v| v.map(Record::Scoped)).map_err(|error| {
            let failure = Failure::from_error(&error);
            failed = Some(failure);
            failure.error()
        });
        let _ = reply.send(value);
    }
    Ok(())
}
