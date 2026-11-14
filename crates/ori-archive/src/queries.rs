use crate::*;
use chrono::{DateTime, SecondsFormat, TimeDelta, Utc};
use rusqlite::{
    Connection, OptionalExtension, named_params, params, params_from_iter, types::Value as SqlValue,
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::HashMap;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct BaselineSelection {
    pub sweep_ids: Vec<String>,
    pub count: u64,
    pub first_started_at: Option<String>,
    pub last_ended_at: Option<String>,
    pub elapsed_seconds: f64,
    pub sweep_period_seconds: Option<f64>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct CapturePosition {
    pub id: String,
    pub rowid: u64,
    pub session_id: String,
    pub stream_id: String,
    pub started_at: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RecordPage {
    pub items: Vec<Value>,
    pub next_cursor: Option<String>,
}

fn round_ratio(numerator: u128, denominator: u128) -> u128 {
    let quotient = numerator / denominator;
    let remainder = numerator % denominator;
    quotient
        + u128::from(
            remainder * 2 > denominator || (remainder * 2 == denominator && quotient & 1 != 0),
        )
}

/// CPython timedelta multiplication uses float.as_integer_ratio(), then rounds
/// the exact integer-microsecond ratio to nearest/even (no double rounding).
fn sample_offset_micros(span: i64, index: usize) -> i64 {
    debug_assert!(index <= 235);
    if index == 0 {
        return 0;
    }
    if index == 235 {
        return span;
    }
    let fraction = (index as f64 / 235.0).to_bits();
    let numerator = ((fraction & ((1_u64 << 52) - 1)) | (1_u64 << 52)) as u128;
    let shift = 1075 - ((fraction >> 52) & 0x7ff) as u32;
    let rounded = round_ratio(span.unsigned_abs() as u128 * numerator, 1_u128 << shift) as i128;
    (if span < 0 { -rounded } else { rounded }) as i64
}

/// Correctly rounded integer division, matching timedelta.total_seconds().
fn micros_seconds(micros: i64) -> f64 {
    if micros == 0 {
        return 0.0;
    }
    let numerator = micros.unsigned_abs() as u128;
    let denominator = 1_000_000_u128;
    let mut exponent =
        (128 - numerator.leading_zeros()) as i32 - (128 - denominator.leading_zeros()) as i32;
    let below = if exponent >= 0 {
        numerator < denominator << exponent
    } else {
        numerator << -exponent < denominator
    };
    if below {
        exponent -= 1;
    }
    let mut significand = round_ratio(numerator << (52 - exponent), denominator);
    if significand == 1_u128 << 53 {
        significand >>= 1;
        exponent += 1;
    }
    let bits = (((exponent + 1023) as u64) << 52) | (significand as u64 - (1_u64 << 52));
    let result = f64::from_bits(bits);
    if micros < 0 { -result } else { result }
}

fn datetime(stamp: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(stamp)
        .map(|time| time.with_timezone(&Utc))
        .map_err(|_| error::invalid("source timestamp"))
}
fn duration_micros(start: DateTime<Utc>, end: DateTime<Utc>) -> Result<i64> {
    (end - start)
        .num_microseconds()
        .ok_or_else(|| error::invalid("source time span"))
}
fn charge<T: Serialize>(value: &T, remaining: &mut usize) -> Result<()> {
    *remaining = remaining
        .checked_sub(serde_json::to_vec(value)?.len())
        .ok_or(ArchiveError::QueryBudget)?;
    Ok(())
}
fn request_ids(ids: &[String], maximum: usize) -> Result<()> {
    if ids
        .iter()
        .try_fold(0_usize, |used, id| used.checked_add(id.len()))
        .is_none_or(|used| used > maximum)
    {
        return Err(error::invalid("source ID byte budget"));
    }
    Ok(())
}
fn position(row: &rusqlite::Row<'_>) -> Result<CapturePosition> {
    let rowid = u64::try_from(row.get::<_, i64>("capture_order")?)
        .map_err(|_| ArchiveError::Corrupt("negative legacy ROWID".into()))?;
    Ok(CapturePosition {
        id: row.get("id")?,
        rowid,
        session_id: row.get("session_id")?,
        stream_id: row.get("stream_id")?,
        started_at: validate::utc(&row.get::<_, String>("started_at")?)?,
    })
}

fn context_sources(
    db: &Connection,
    session: &str,
    id: &str,
    before: usize,
    after: usize,
    maximum: usize,
    scientific_gate: bool,
) -> Result<Option<SweepContext>> {
    let Some(mut anchor) = rows::get_sweep(db, id)? else {
        return Ok(None);
    };
    if anchor["session_id"] != session {
        return Ok(None);
    }
    if scientific_gate {
        validate::sweep(&mut anchor)?;
        if anchor["level_unit"] != "dBm" {
            return Err(ArchiveError::Corrupt(
                "context requires original dBm sweeps".into(),
            ));
        }
    }
    let mut remaining = maximum;
    charge(&anchor, &mut remaining)?;
    let stamp = anchor["interval"]["started_at"]
        .as_str()
        .ok_or_else(|| error::invalid("context timestamp"))?;
    let stream = anchor["stream_id"]
        .as_str()
        .ok_or_else(|| error::invalid("context stream"))?;
    let mut neighbours = |count: usize, preceding: bool| -> Result<Vec<Value>> {
        if count == 0 {
            return Ok(Vec::new());
        }
        let (comparison, direction, prefix) = if preceding {
            ("<", "DESC", "<=")
        } else {
            (">", "ASC", ">=")
        };
        let sql = format!(
            "SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND s.stream_id=? AND i.started_at{prefix}? AND (i.started_at,context_utf8(i.sweep_id)){comparison}(?,?) ORDER BY i.started_at {direction},context_utf8(i.sweep_id) {direction} LIMIT ?"
        );
        let mut statement = db.prepare(&sql)?;
        let mut rows = statement.query(params![
            session,
            stream,
            stamp,
            stamp,
            id.as_bytes(),
            count as i64
        ])?;
        let mut values = Vec::new();
        while let Some(row) = rows.next()? {
            let mut source = rows::sweep(row)?;
            if scientific_gate {
                validate::sweep(&mut source)?;
                if source["level_unit"] != "dBm" {
                    return Err(ArchiveError::Corrupt("context unit mismatch".into()));
                }
            }
            push(&mut values, source, &mut remaining)?;
        }
        if preceding {
            values.reverse();
        }
        Ok(values)
    };
    let before = neighbours(before, true)?;
    let after = neighbours(after, false)?;
    Ok(Some(SweepContext {
        anchor,
        before,
        after,
    }))
}

fn validate_context(id: &str, before: usize, after: usize) -> Result<()> {
    if before > 16 || after > 16 || id.trim().is_empty() || id.chars().count() > 128 {
        return Err(error::invalid("context count or source ID"));
    }
    Ok(())
}

struct Sample {
    id: String,
    started_at: DateTime<Utc>,
    ended_at: DateTime<Utc>,
}
fn sample(row: &rusqlite::Row<'_>, maximum: usize) -> Result<Sample> {
    let id: String = row.get("id")?;
    if id.len() > maximum {
        return Err(ArchiveError::QueryBudget);
    }
    Ok(Sample {
        id,
        started_at: datetime(&validate::utc(&row.get::<_, String>("started_at")?)?)?,
        ended_at: datetime(&validate::utc(&row.get::<_, String>("ended_at")?)?)?,
    })
}
fn baseline_sources(
    db: &Connection,
    session: &str,
    stream: &str,
    maximum: usize,
    band: Option<&str>,
    plan: Option<&str>,
) -> Result<BaselineSelection> {
    let scope = " AND (:band IS NULL OR json_extract(s.settings_json,'$.scan_band_id')=:band) AND (:plan IS NULL OR json_extract(s.settings_json,'$.scan_plan_id')=:plan)";
    let (count, first, last) = db.query_row(&format!("SELECT COUNT(*),MIN(i.started_at),MAX(i.ended_at) FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=:session AND s.stream_id=:stream{scope}"),
        named_params!{":session":session,":stream":stream,":band":band,":plan":plan}, |row| Ok((row.get::<_, i64>(0)?, row.get::<_, Option<String>>(1)?, row.get::<_, Option<String>>(2)?)))?;
    let first = first.map(|value| validate::utc(&value)).transpose()?;
    let last = last.map(|value| validate::utc(&value)).transpose()?;
    let elapsed_seconds = match (&first, &last) {
        (Some(first), Some(last)) => {
            micros_seconds(duration_micros(datetime(first)?, datetime(last)?)?)
        }
        (None, None) if count == 0 => 0.0,
        _ => {
            return Err(ArchiveError::Corrupt(
                "baseline collection time bounds".into(),
            ));
        }
    };
    // Deliberately mirror SQLiteSessionStore.list_sweeps' original recent-source
    // ordering, including historical raw offset text/native UTF-16 ID ties.
    let mut statement = db.prepare(&format!("SELECT id,started_at,ended_at,CASE WHEN instr(started_at,'.')=0 THEN replace(started_at,'Z','.000000Z') ELSE started_at END AS sort_started_at FROM sweeps s WHERE session_id=:session AND stream_id=:stream{scope} ORDER BY sort_started_at DESC,id DESC LIMIT 20"))?;
    let mut rows = statement
        .query(named_params! {":session":session,":stream":stream,":band":band,":plan":plan})?;
    let mut recent = Vec::new();
    let mut remaining = maximum;
    while let Some(row) = rows.next()? {
        let source = sample(row, maximum)?;
        charge(&source.id, &mut remaining)?;
        recent.push(source);
    }
    recent.reverse();
    let mut result = BaselineSelection {
        sweep_ids: Vec::new(),
        count: u64::try_from(count)
            .map_err(|_| ArchiveError::Corrupt("negative source count".into()))?,
        first_started_at: first,
        last_ended_at: last,
        elapsed_seconds,
        sweep_period_seconds: None,
    };
    if recent.len() < 20 {
        result.sweep_ids = recent.into_iter().map(|source| source.id).collect();
        return Ok(result);
    }
    let mut periods = recent
        .windows(2)
        .map(|pair| duration_micros(pair[0].started_at, pair[1].started_at).map(micros_seconds))
        .collect::<Result<Vec<_>>>()?;
    periods.retain(|period| *period > 0.0);
    periods.sort_by(f64::total_cmp);
    result.sweep_period_seconds = Some(if periods.is_empty() {
        1.0
    } else if periods.len().is_multiple_of(2) {
        (periods[periods.len() / 2 - 1] + periods[periods.len() / 2]) / 2.0
    } else {
        periods[periods.len() / 2]
    });
    let last = recent.last().expect("twenty sources");
    let target = last
        .ended_at
        .checked_sub_signed(TimeDelta::seconds(60))
        .ok_or_else(|| error::invalid("baseline target time"))?;
    let mut statement = db.prepare(&format!("SELECT s.id,i.started_at,i.ended_at FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=:session AND s.stream_id=:stream AND i.started_at<=:target{scope} ORDER BY i.started_at DESC,s.id DESC LIMIT 1"))?;
    let mut rows = statement.query(named_params!{":session":session,":stream":stream,":band":band,":plan":plan,":target":target.to_rfc3339_opts(SecondsFormat::Micros, true)})?;
    let older = rows.next()?.map(|row| sample(row, maximum)).transpose()?;
    let start = older.map_or(recent[0].started_at, |older| {
        recent[0].started_at.min(older.started_at)
    });
    let end = last.started_at;
    let span = duration_micros(start, end)?;
    let mut selected = HashMap::new();
    let mut remaining = maximum;
    for source in &recent {
        charge(&source.id, &mut remaining)?;
        selected.insert(source.id.clone(), source.started_at);
    }
    let mut statement = db.prepare(&format!("SELECT s.id,i.started_at,i.ended_at FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=:session AND s.stream_id=:stream AND i.started_at>=:start AND i.started_at<=:end{scope} ORDER BY i.started_at,s.id LIMIT 1"))?;
    let end_stamp = end.to_rfc3339_opts(SecondsFormat::Micros, true);
    for index in 0..236 {
        let stamp = start
            .checked_add_signed(TimeDelta::microseconds(sample_offset_micros(span, index)))
            .ok_or_else(|| error::invalid("baseline target time"))?
            .to_rfc3339_opts(SecondsFormat::Micros, true);
        let mut rows = statement.query(named_params!{":session":session,":stream":stream,":band":band,":plan":plan,":start":stamp,":end":end_stamp})?;
        if let Some(row) = rows.next()? {
            let source = sample(row, maximum)?;
            if let std::collections::hash_map::Entry::Vacant(entry) = selected.entry(source.id) {
                charge(entry.key(), &mut remaining)?;
                entry.insert(source.started_at);
            }
        }
    }
    let mut selected: Vec<_> = selected.into_iter().collect();
    selected
        .sort_by(|(id_a, time_a), (id_b, time_b)| time_a.cmp(time_b).then_with(|| id_a.cmp(id_b)));
    result.sweep_ids = selected.into_iter().map(|(id, _)| id).collect();
    Ok(result)
}

fn text(value: impl Into<String>) -> SqlValue {
    SqlValue::Text(value.into())
}
fn cursor(value: &Value, time_field: &str) -> MeasurementCursor {
    MeasurementCursor {
        started_at: value[time_field]
            .as_str()
            .expect("decoded timestamp")
            .into(),
        id: value["id"].as_str().expect("decoded source ID").into(),
    }
}
pub(crate) fn push(output: &mut Vec<Value>, value: Value, remaining: &mut usize) -> Result<()> {
    let bytes = serde_json::to_vec(&value)?.len();
    if bytes > *remaining {
        return Err(ArchiveError::QueryBudget);
    }
    *remaining -= bytes;
    output.push(value);
    Ok(())
}
pub(crate) fn collect(
    mut rows: rusqlite::Rows<'_>,
    decode: fn(&rusqlite::Row<'_>) -> Result<Value>,
    maximum: usize,
) -> Result<Vec<Value>> {
    let mut remaining = maximum;
    let mut values = Vec::new();
    while let Some(row) = rows.next()? {
        push(&mut values, decode(row)?, &mut remaining)?;
    }
    Ok(values)
}
fn query_sweeps(db: &Connection, query: &SweepQuery, maximum: usize) -> Result<Page<Value>> {
    if query.session_id.is_none() && query.project_id.is_none() {
        return Err(error::invalid(
            "a session or project history scope is required",
        ));
    }
    if query.min_hz.is_some_and(|hz| hz < 0)
        || query.max_hz.is_some_and(|hz| hz > 9_007_199_254_740_991)
        || matches!((query.min_hz,query.max_hz),(Some(a),Some(b)) if a>b)
    {
        return Err(error::invalid("history frequency bounds"));
    }
    let start = query
        .start
        .as_deref()
        .map(validate::utc)
        .transpose()?
        .unwrap_or_else(|| "0000".into());
    let end = query
        .end
        .as_deref()
        .map(validate::utc)
        .transpose()?
        .unwrap_or_else(|| "9999".into());
    if start > end {
        return Err(error::invalid("history time bounds"));
    }
    let mut clauses = vec!["i.started_at>=?", "i.started_at<=?"];
    let mut arguments = vec![text(start), text(end)];
    if let Some(sid) = &query.session_id {
        clauses.push("i.session_id=?");
        arguments.push(text(sid));
    }
    if let Some(stream) = &query.stream_id {
        clauses.push("s.stream_id=?");
        arguments.push(text(stream));
    }
    if let Some(project) = &query.project_id {
        clauses.push("EXISTS(SELECT 1 FROM monitoring_sessions p WHERE p.project_id=? AND p.session_id=i.session_id)");
        arguments.push(text(project));
    }
    if let Some(event) = &query.event_id {
        let project = query
            .project_id
            .as_deref()
            .ok_or_else(|| error::invalid("event history requires a project scope"))?;
        let owned = db
            .query_row(
                "SELECT 1 FROM monitoring_records WHERE kind='event' AND id=? AND project_id=?",
                params![event, project],
                |row| row.get::<_, i64>(0),
            )
            .optional()?
            .is_some();
        if !owned {
            return Err(ArchiveError::NotFound);
        }
        clauses.push("EXISTS(SELECT 1 FROM monitoring_records e WHERE e.kind='event-source' AND e.project_id=? AND json_extract(e.payload,'$.event_id')=? AND json_extract(e.payload,'$.sweep_id')=s.id)");
        arguments.push(text(project));
        arguments.push(text(event));
    }
    if let Some(minimum) = query.min_hz {
        clauses.push("i.max_hz>=?");
        arguments.push(SqlValue::Integer(minimum));
    }
    if let Some(maximum) = query.max_hz {
        clauses.push("i.min_hz<=?");
        arguments.push(SqlValue::Integer(maximum));
    }
    if let Some(after) = &query.after {
        let stamp = validate::utc(&after.started_at)?;
        clauses.push("i.started_at<=? AND (i.started_at,context_utf8(i.sweep_id))<(?,?)");
        arguments.push(text(&stamp));
        arguments.push(text(stamp));
        arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));
    }
    arguments.push(SqlValue::Integer(query.limit as i64 + 1));
    let sql = format!(
        "SELECT s.* FROM monitoring_sweep_index i JOIN sweeps s ON s.id=i.sweep_id WHERE {} ORDER BY i.started_at DESC,context_utf8(i.sweep_id) DESC LIMIT ?",
        clauses.join(" AND ")
    );
    let mut statement = db.prepare(&sql)?;
    let mut items = collect(
        statement.query(params_from_iter(arguments.iter()))?,
        rows::sweep,
        maximum,
    )?;
    let more = items.len() > query.limit;
    items.truncate(query.limit);
    let next_cursor = if more {
        items.last().map(|value| MeasurementCursor {
            started_at: value["interval"]["started_at"]
                .as_str()
                .expect("decoded timestamp")
                .into(),
            id: value["id"].as_str().expect("decoded ID").into(),
        })
    } else {
        None
    };
    Ok(Page { items, next_cursor })
}
fn observations(
    db: &Connection,
    sid: &str,
    stream: Option<&str>,
    limit: usize,
    maximum: usize,
) -> Result<Vec<Value>> {
    let mut arguments = vec![text(sid)];
    let stream_clause = if let Some(stream) = stream {
        arguments.push(text(stream));
        " AND o.stream_id=?"
    } else {
        ""
    };
    arguments.push(SqlValue::Integer(limit as i64));
    let mut statement=db.prepare(&format!("SELECT o.*,p.level_bits_le FROM observations o LEFT JOIN ori_observation_level_bits p ON p.observation_id=o.id WHERE o.session_id=?{stream_clause} ORDER BY context_utc(o.started_at) DESC,context_utf8(o.id) DESC LIMIT ?"))?;
    let mut values = collect(
        statement.query(params_from_iter(arguments.iter()))?,
        rows::observation,
        maximum,
    )?;
    values.reverse();
    Ok(values)
}
impl Archive {
    /// Session and found same-session sources from one immutable read snapshot.
    /// Omit missing/foreign IDs so the scientific worker retains validation priority.
    pub async fn analysis_inputs(
        &self,
        session: &str,
        sweep_ids: Vec<String>,
        candidate_ids: Vec<String>,
    ) -> Result<(Value, Vec<Value>, Vec<Value>)> {
        if sweep_ids.len() > 33 || candidate_ids.len() > 1000 {
            return Err(error::invalid("analysis source count"));
        }
        let maximum = self.inner.config.max_response_bytes;
        request_ids(&sweep_ids, maximum)?;
        request_ids(&candidate_ids, maximum)?;
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                let selected = rows::session(db, &session)?.ok_or(ArchiveError::NotFound)?;
                let mut remaining = maximum;
                charge(&selected, &mut remaining)?;
                let mut sweeps = Vec::new();
                let mut candidates = Vec::new();
                let mut statement =
                    db.prepare("SELECT * FROM sweeps WHERE id=? AND session_id=?")?;
                for id in sweep_ids {
                    let mut rows = statement.query(params![id, session])?;
                    if let Some(row) = rows.next()? {
                        push(&mut sweeps, rows::sweep(row)?, &mut remaining)?;
                    }
                }
                let mut statement = db.prepare(&format!(
                    "{} WHERE c.id=? AND c.session_id=?",
                    rows::CANDIDATE_SELECT
                ))?;
                for id in candidate_ids {
                    let mut rows = statement.query(params![id, session])?;
                    if let Some(row) = rows.next()? {
                        push(&mut candidates, rows::candidate(row)?, &mut remaining)?;
                    }
                }
                Ok((selected, sweeps, candidates))
            })
            .await
    }

    /// Actual nearest neighbors, retaining raw scientific incompatibilities.
    pub async fn analysis_context(
        &self,
        session: &str,
        id: &str,
        before: usize,
        after: usize,
    ) -> Result<Option<(Value, SweepContext)>> {
        validate_context(id, before, after)?;
        let session = session.to_owned();
        let id = id.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let selected = rows::session(db, &session)?.ok_or(ArchiveError::NotFound)?;
                let mut remaining = maximum;
                charge(&selected, &mut remaining)?;
                Ok(
                    context_sources(db, &session, &id, before, after, remaining, false)?
                        .map(|context| (selected, context)),
                )
            })
            .await
    }

    /// Whole-source summaries; the caller freezes filters and boundary in its opaque cursor.
    pub async fn sweep_summaries(&self, query: SweepQuery, boundary: u64) -> Result<Page<Value>> {
        validate_limit(query.limit, 100)?;
        if boundary > i64::MAX as u64 {
            return Err(error::invalid("summary ingestion boundary"));
        }
        let sid = query
            .session_id
            .clone()
            .ok_or_else(|| error::invalid("summary session scope"))?;
        if query.project_id.is_some() || query.event_id.is_some() {
            return Err(error::invalid("summary project/event filters unsupported"));
        }
        if query.min_hz.is_some_and(|hz| hz < 0)
            || query.max_hz.is_some_and(|hz| hz < 0)
            || matches!((query.min_hz,query.max_hz),(Some(a),Some(b)) if a>b)
        {
            return Err(error::invalid("summary frequency bounds"));
        }
        let start = query.start.as_deref().map(validate::utc).transpose()?;
        let end = query.end.as_deref().map(validate::utc).transpose()?;
        if matches!((&start,&end),(Some(a),Some(b)) if a>=b) {
            return Err(error::invalid("summary time bounds"));
        }
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let current=db.query_row("SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion WHERE session_id=?",[&sid],|row|row.get::<_,i64>(0))?;
            if boundary>current as u64{return Err(error::invalid("summary boundary exceeds archive state"));}
            let mut clauses=vec!["m.session_id=?","i.sequence<=?","s.id=m.sweep_id","i.sweep_id=m.sweep_id"];
            let mut arguments=vec![text(sid),SqlValue::Integer(boundary as i64)];
            if let Some(stream)=query.stream_id{clauses.push("s.stream_id=?");arguments.push(text(stream));}
            if let Some(start)=start{clauses.push("m.started_at>=?");arguments.push(text(start));}
            if let Some(end)=end{clauses.push("m.started_at<?");arguments.push(text(end));}
            if let Some(minimum)=query.min_hz{clauses.push("m.max_hz>=?");arguments.push(SqlValue::Integer(minimum));}
            if let Some(maximum)=query.max_hz{clauses.push("m.min_hz<=?");arguments.push(SqlValue::Integer(maximum));}
            if let Some(after)=query.after{let stamp=validate::utc(&after.started_at)?;clauses.push("(m.started_at<? OR (m.started_at=? AND context_utf8(m.sweep_id)<?))");arguments.push(text(&stamp));arguments.push(text(stamp));arguments.push(SqlValue::Blob(after.id.into_bytes()));}
            arguments.push(SqlValue::Integer(query.limit as i64+1));
            let sql=format!("SELECT m.sweep_id FROM monitoring_sweep_index m INDEXED BY monitoring_sweep_index_scope_time CROSS JOIN sweeps s CROSS JOIN sweep_ingestion i WHERE {} ORDER BY m.started_at DESC,context_utf8(m.sweep_id) DESC LIMIT ?",clauses.join(" AND "));
            let ids=db.prepare(&sql)?.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;
            let more=ids.len()>query.limit;let mut items=Vec::new();let mut remaining=maximum;
            for id in ids.into_iter().take(query.limit){
                let mut source=rows::get_sweep(db,&id)?.ok_or(ArchiveError::NotFound)?;validate::sweep(&mut source)?;
                let levels=source["levels"].as_array().expect("validated levels");let frequencies=source["frequencies_hz"].as_array().expect("validated frequencies");
                let mut peak=0;for i in 1..levels.len(){if levels[i].as_f64().unwrap()>levels[peak].as_f64().unwrap(){peak=i;}}
                let summary=json!({"id":source["id"],"session_id":source["session_id"],"stream_id":source["stream_id"],"interval":source["interval"],"level_unit":source["level_unit"],"point_count":levels.len(),"min_frequency_hz":frequencies.first(),"max_frequency_hz":frequencies.last(),"peak_frequency_hz":frequencies[peak],"peak_level":levels[peak]});
                push(&mut items,summary,&mut remaining)?;
            }
            let next_cursor=if more{items.last().map(|value|MeasurementCursor{started_at:value["interval"]["started_at"].as_str().unwrap().into(),id:value["id"].as_str().unwrap().into()})}else{None};
            Ok(Page{items,next_cursor})
        }).await
    }
    pub async fn sweep_page(&self, query: SweepQuery) -> Result<Page<Value>> {
        validate_limit(query.limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| query_sweeps(db, &query, maximum))
            .await
    }
    pub async fn list_sessions(
        &self,
        limit: usize,
        after: Option<MeasurementCursor>,
    ) -> Result<Page<Value>> {
        validate_limit(limit, 1000)?;
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let mut arguments=Vec::new();let clause=if let Some(after)=after{arguments.push(text(validate::utc(&after.started_at)?));arguments.push(SqlValue::Blob(after.id.as_bytes().to_vec()));"WHERE (context_utc(created_at),context_utf8(id))>(?,?)"}else{""};arguments.push(SqlValue::Integer(limit as i64+1));
            let mut statement=db.prepare(&format!("SELECT id FROM sessions {clause} ORDER BY context_utc(created_at),context_utf8(id) LIMIT ?"))?;
            let ids=statement.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;let more=ids.len()>limit;let mut items=Vec::new();let mut remaining=maximum;
            for id in ids.into_iter().take(limit){push(&mut items,rows::session(db,&id)?.ok_or(ArchiveError::NotFound)?,&mut remaining)?;}
            let next_cursor=if more{items.last().map(|v|cursor(v,"created_at"))}else{None};Ok(Page{items,next_cursor})
        }).await
    }
    pub async fn list_sweeps(
        &self,
        session: &str,
        stream: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        let mut query = SweepQuery::session(session);
        query.stream_id = stream.map(str::to_owned);
        query.limit = limit;
        let mut values = self.sweep_page(query).await?.items;
        values.reverse();
        Ok(values)
    }
    pub async fn list_observations(
        &self,
        session: &str,
        stream: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(limit, 1000)?;
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| observations(db, &session, stream.as_deref(), limit, maximum))
            .await
    }
    pub async fn sweep_context(
        &self,
        session: &str,
        id: &str,
        before: usize,
        after: usize,
    ) -> Result<Option<SweepContext>> {
        validate_context(id, before, after)?;
        let session = session.to_owned();
        let id = id.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| context_sources(db, &session, &id, before, after, maximum, true))
            .await
    }
    pub async fn baseline_selection(
        &self,
        session: &str,
        stream: &str,
    ) -> Result<BaselineSelection> {
        let session = session.to_owned();
        let stream = stream.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| baseline_sources(db, &session, &stream, maximum, None, None))
            .await
    }

    /// Scope all baseline statistics and samples to the immutable plan and band.
    /// The legacy ordering and bounded uniform-window sampling remain identical.
    pub async fn baseline_selection_for_scan_band(
        &self,
        session: &str,
        stream: &str,
        plan: &str,
        band: &str,
    ) -> Result<BaselineSelection> {
        if plan.is_empty() || plan.len() > 200 || band.is_empty() || band.len() > 64 {
            return Err(error::invalid("scan baseline scope"));
        }
        let (session, stream, plan, band) = (
            session.to_owned(),
            stream.to_owned(),
            plan.to_owned(),
            band.to_owned(),
        );
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                baseline_sources(db, &session, &stream, maximum, Some(&band), Some(&plan))
            })
            .await
    }

    pub async fn capture_positions(
        &self,
        ids: &[String],
    ) -> Result<HashMap<String, CapturePosition>> {
        if ids.len() > 256 {
            return Err(error::invalid("capture position batch"));
        }
        let maximum = self.inner.config.max_response_bytes;
        request_ids(ids, maximum)?;
        let ids = ids.to_vec();
        self.inner.read(move |db| {
            let mut output = HashMap::new();
            if ids.is_empty() { return Ok(output); }
            let placeholders = vec!["?"; ids.len()].join(",");
            let mut statement = db.prepare(&format!("SELECT rowid AS capture_order,id,session_id,stream_id,started_at FROM sweeps WHERE id IN ({placeholders})"))?;
            let mut rows = statement.query(params_from_iter(ids.iter()))?;
            let mut remaining = maximum;
            while let Some(row) = rows.next()? {
                let source = position(row)?;
                charge(&source, &mut remaining)?;
                output.insert(source.id.clone(), source);
            }
            Ok(output)
        }).await
    }

    /// Resume the legacy detector ROWID cursor; never reinterpret ingestion sequence.
    pub async fn legacy_pending_sweeps(
        &self,
        session: &str,
        stream: &str,
        after_rowid: u64,
        limit: usize,
    ) -> Result<Vec<CapturePosition>> {
        validate_limit(limit, 64)?;
        if after_rowid > i64::MAX as u64 {
            return Err(error::invalid("legacy ROWID cursor"));
        }
        let session = session.to_owned();
        let stream = stream.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            // Preserve ROWID range traversal. A stream/time index can instead
            // sort all pending captures before LIMIT, defeating cursor progress.
            let mut statement = db.prepare("SELECT rowid AS capture_order,id,session_id,stream_id,started_at FROM sweeps NOT INDEXED WHERE session_id=? AND stream_id=? AND rowid>? ORDER BY rowid LIMIT ?")?;
            let mut rows = statement.query(params![session, stream, after_rowid as i64, limit as i64])?;
            let mut output = Vec::new();
            let mut remaining = maximum;
            while let Some(row) = rows.next()? {
                let source = position(row)?;
                charge(&source, &mut remaining)?;
                output.push(source);
            }
            Ok(output)
        }).await
    }

    pub async fn open_events(
        &self,
        project: &str,
        session: &str,
        baseline: &str,
    ) -> Result<Vec<Value>> {
        let project = project.to_owned();
        let session = session.to_owned();
        let baseline = baseline.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut statement = db.prepare("SELECT payload FROM monitoring_records WHERE kind='event' AND project_id=? AND json_extract(payload,'$.state')='open' AND json_extract(payload,'$.run_session_id')=? AND json_extract(payload,'$.baseline_id')=? ORDER BY id LIMIT 1000")?;
            let mut rows = statement.query(params![project, session, baseline])?;
            let mut remaining = maximum;
            let mut output = Vec::new();
            while let Some(row) = rows.next()? { push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?; }
            Ok(output)
        }).await
    }

    pub async fn latest_scan_baseline(
        &self,
        project: &str,
        plan: &str,
        band: &str,
    ) -> Result<Option<Value>> {
        let (project, plan, band) = (project.to_owned(), plan.to_owned(), band.to_owned());
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
            let raw:Option<String>=db.query_row("SELECT payload FROM monitoring_records WHERE kind='baseline' AND project_id=? AND json_extract(payload,'$.scan_plan_id')=? AND json_extract(payload,'$.scan_band_id')=? ORDER BY created_at DESC,id DESC LIMIT 1",params![project,plan,band],|row|row.get(0)).optional()?;
            raw.map(|raw|{if raw.len()>maximum{return Err(ArchiveError::QueryBudget)}rows::object(raw)}).transpose()
        }).await
    }

    pub async fn position_marks(
        &self,
        project: &str,
        search_session: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>> {
        validate_limit(limit, 256)?;
        let project = project.to_owned();
        let search_session = search_session.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut arguments = vec![text(project)];
            let clause = if let Some(session) = search_session {
                arguments.push(text(session));
                " AND json_extract(payload,'$.search_session_id')=?"
            } else { "" };
            arguments.push(SqlValue::Integer(limit as i64));
            let mut statement = db.prepare(&format!("SELECT payload FROM monitoring_records WHERE kind='position-mark' AND project_id=?{clause} ORDER BY created_at DESC,id DESC LIMIT ?"))?;
            let mut rows = statement.query(params_from_iter(arguments.iter()))?;
            let mut remaining = maximum;
            let mut output = Vec::new();
            while let Some(row) = rows.next()? { push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?; }
            Ok(output)
        }).await
    }

    pub async fn pending_receiver_events(
        &self,
        project: &str,
        session: &str,
    ) -> Result<Vec<Value>> {
        let project = project.to_owned();
        let session = session.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut statement = db.prepare("SELECT payload FROM monitoring_records WHERE kind='event' AND project_id=? AND json_extract(payload,'$.analysis.state') IS NULL AND json_extract(payload,'$.source_refs[#-1].session_id')=? AND json_extract(payload,'$.disposition')='unreviewed' AND json_extract(payload,'$.kind') NOT IN ('broadband_change','dense_change','authorized_changed') ORDER BY json_extract(payload,'$.last_seen') DESC,id DESC LIMIT 1")?;
            let mut rows = statement.query(params![project, session])?;
            let mut remaining = maximum;
            let mut output = Vec::new();
            while let Some(row) = rows.next()? { push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?; }
            Ok(output)
        }).await
    }

    pub async fn event_sources(
        &self,
        project: &str,
        event: &str,
        after: &str,
        limit: usize,
    ) -> Result<RecordPage> {
        validate_limit(limit, 64)?;
        let project = project.to_owned();
        let event = event.to_owned();
        let after = after.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            if db.query_row("SELECT 1 FROM monitoring_records WHERE kind='event' AND project_id=? AND id=?", params![project, event], |row| row.get::<_, i64>(0)).optional()?.is_none() { return Err(ArchiveError::NotFound); }
            let mut statement = db.prepare("SELECT payload FROM monitoring_records WHERE kind='event-source' AND project_id=? AND json_extract(payload,'$.event_id')=? AND id>? ORDER BY id LIMIT ?")?;
            let mut rows = statement.query(params![project, event, after, limit as i64+1])?;
            let mut remaining = maximum;
            let mut output = Vec::new();
            while let Some(row) = rows.next()? { push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?; }
            let more = output.len() > limit;
            output.truncate(limit);
            let next_cursor = if more { output.last().and_then(|item| item["id"].as_str()).map(str::to_owned) } else { None };
            Ok(RecordPage { items: output, next_cursor })
        }).await
    }

    pub async fn measurement_owners(
        &self,
        ids: Vec<String>,
    ) -> Result<HashMap<String, Vec<(String, String)>>> {
        if ids.len() > 1000 {
            return Err(error::invalid("measurement ownership batch"));
        }
        self.inner.read(move|db|{
            let mut output=ids.iter().map(|id|(id.clone(),Vec::new())).collect::<HashMap<_,_>>();
            for ids in ids.chunks(400){if ids.is_empty(){continue;}let placeholders=std::iter::repeat_n("?",ids.len()).collect::<Vec<_>>().join(",");for table in ["sweeps","observations"]{let mut statement=db.prepare(&format!("SELECT id,session_id,stream_id FROM {table} WHERE id IN ({placeholders})"))?;let mut rows=statement.query(params_from_iter(ids.iter()))?;while let Some(row)=rows.next()?{let id:String=row.get(0)?;let owner=(row.get(1)?,row.get(2)?);let list=output.get_mut(&id).expect("requested owner");if !list.contains(&owner){list.push(owner);}}}}Ok(output)
        }).await
    }
    pub async fn sweep_ingestion_boundary(&self, session: &str) -> Result<u64> {
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                Ok(db.query_row(
                    "SELECT COALESCE(MAX(sequence),0) FROM sweep_ingestion WHERE session_id=?",
                    [session],
                    |row| row.get::<_, i64>(0),
                )? as u64)
            })
            .await
    }
    pub async fn sweeps_after_ingestion(
        &self,
        session: &str,
        stream: Option<&str>,
        after: u64,
        limit: usize,
    ) -> Result<Vec<(u64, Value)>> {
        validate_limit(limit, 1000)?;
        if after > i64::MAX as u64 {
            return Err(error::invalid("ingestion cursor"));
        }
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{let mut arguments=vec![text(session),SqlValue::Integer(after as i64)];let clause=if let Some(stream)=stream{arguments.push(text(stream));" AND i.stream_id=?"}else{""};arguments.push(SqlValue::Integer(limit as i64));let mut statement=db.prepare(&format!("SELECT i.sequence,s.* FROM sweep_ingestion i JOIN sweeps s ON s.id=i.sweep_id WHERE i.session_id=? AND i.sequence>?{clause} ORDER BY i.sequence LIMIT ?"))?;let mut rows=statement.query(params_from_iter(arguments.iter()))?;let mut remaining=maximum;let mut output=Vec::new();while let Some(row)=rows.next()?{let value=rows::sweep(row)?;let bytes=serde_json::to_vec(&value)?.len();if bytes>remaining{return Err(ArchiveError::QueryBudget);}remaining-=bytes;output.push((row.get::<_,i64>("sequence")? as u64,value));}Ok(output)}).await
    }
    pub async fn monitoring(&self, kind: &str, id: &str) -> Result<Option<Value>> {
        if !validate::KINDS.contains(&kind) {
            return Err(error::invalid("monitoring kind"));
        }
        let kind = kind.to_owned();
        let id = id.to_owned();
        self.inner
            .read(move |db| {
                db.query_row(
                    "SELECT payload FROM monitoring_records WHERE kind=? AND id=?",
                    params![kind, id],
                    |row| row.get::<_, String>(0),
                )
                .optional()?
                .map(rows::object)
                .transpose()
            })
            .await
    }
    pub async fn put_monitoring(&self, kind: &str, value: Value) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::Monitoring {
            kind: kind.into(),
            value,
        }])
        .await
    }
    pub async fn list_monitoring(&self, kind: &str, query: MonitoringQuery) -> Result<Vec<Value>> {
        if !validate::KINDS.contains(&kind) {
            return Err(error::invalid("monitoring kind"));
        }
        validate_limit(query.limit, 1000)?;
        let kind = kind.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| {
                let mut arguments = vec![text(kind.clone())];
                let mut clauses = vec!["kind=?"];
                if let Some(project) = query.project_id {
                    clauses.push("project_id=?");
                    arguments.push(text(project));
                }
                if let Some(state) = query.state {
                    clauses.push("json_extract(payload,'$.state')=?");
                    arguments.push(text(state));
                }
                if !query.after.is_empty() {
                    if query.recent {
                        let created = db
                            .query_row(
                                "SELECT created_at FROM monitoring_records WHERE kind=? AND id=?",
                                params![kind, query.after],
                                |row| row.get::<_, String>(0),
                            )
                            .optional()?
                            .ok_or_else(|| error::invalid("monitoring cursor"))?;
                        clauses.push("(created_at,context_utf8(id))<(?,?)");
                        arguments.push(text(created));
                        arguments.push(SqlValue::Blob(query.after.as_bytes().to_vec()));
                    } else {
                        clauses.push("context_utf8(id)>?");
                        arguments.push(SqlValue::Blob(query.after.as_bytes().to_vec()));
                    }
                }
                arguments.push(SqlValue::Integer(query.limit as i64));
                let order = if query.recent {
                    "created_at DESC,context_utf8(id) DESC"
                } else {
                    "context_utf8(id)"
                };
                let mut statement = db.prepare(&format!(
                    "SELECT payload FROM monitoring_records WHERE {} ORDER BY {order} LIMIT ?",
                    clauses.join(" AND ")
                ))?;
                let mut rows = statement.query(params_from_iter(arguments.iter()))?;
                let mut remaining = maximum;
                let mut output = Vec::new();
                while let Some(row) = rows.next()? {
                    push(&mut output, rows::object(row.get(0)?)?, &mut remaining)?;
                }
                Ok(output)
            })
            .await
    }
    pub async fn monitoring_member(&self, project: &str, session: &str) -> Result<bool> {
        let project = project.to_owned();
        let session = session.to_owned();
        self.inner
            .read(move |db| {
                Ok(db
                    .query_row(
                        "SELECT 1 FROM monitoring_sessions WHERE project_id=? AND session_id=?",
                        params![project, session],
                        |row| row.get::<_, i64>(0),
                    )
                    .optional()?
                    .is_some())
            })
            .await
    }
    pub async fn capacity(&self) -> Result<Value> {
        let maximum = self.inner.config.max_database_bytes;
        self.inner.read(move|db|{let size=schema::database_bytes(db)?;let state=match maximum{None=>"unlimited",Some(limit)if size>=limit=>"full",Some(limit)if (size as u128)*10>=(limit as u128)*9=>"warning",Some(_)=>"ok"};Ok(json!({"storage_mode":"sqlite","database_bytes":size,"max_database_bytes":maximum,"remaining_bytes":maximum.map(|limit|limit.saturating_sub(size)),"state":state,"warning_threshold":0.9,"measurement_writes_blocked":state=="full","automatic_deletion":false}))}).await
    }
    pub async fn registry_counts(&self) -> Result<Value> {
        self.inner.read(move|db|Ok(db.query_row("SELECT (SELECT COUNT(*) FROM sessions),(SELECT COUNT(*) FROM sessions WHERE state='running'),(SELECT COUNT(*) FROM candidates),(SELECT COUNT(*) FROM investigations WHERE state='queued'),(SELECT COUNT(*) FROM investigations WHERE state IN ('tuning','listening','decoding')),(SELECT COALESCE(MAX(sequence),0) FROM events)",[],|row|Ok(json!({"sessions":row.get::<_,i64>(0)?,"running_sessions":row.get::<_,i64>(1)?,"candidates":row.get::<_,i64>(2)?,"queued_investigations":row.get::<_,i64>(3)?,"active_investigations":row.get::<_,i64>(4)?,"last_event_id":row.get::<_,i64>(5)?})))?)).await
    }
    pub async fn analytics_summary(&self, session: &str, stream: Option<&str>) -> Result<Value> {
        let session = session.to_owned();
        let stream = stream.map(str::to_owned);
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move|db|{
        let mut arguments=vec![text(session.clone())];let clause=if let Some(stream)=&stream{arguments.push(text(stream));" AND stream_id=?"}else{""};
        let totals=|table:&str|->Result<Value>{let sql=format!("SELECT COUNT(*),MIN(context_utc(started_at)),MAX(context_utc(started_at)) FROM {table} WHERE session_id=?{clause}");let (count,first,last)=db.query_row(&sql,params_from_iter(arguments.iter()),|row|Ok((row.get::<_,i64>(0)?,row.get::<_,Option<String>>(1)?,row.get::<_,Option<String>>(2)?)))?;let mut statement=db.prepare(&format!("SELECT DISTINCT level_unit FROM {table} WHERE session_id=?{clause} ORDER BY context_utf8(level_unit)"))?;let units=statement.query_map(params_from_iter(arguments.iter()),|row|row.get::<_,String>(0))?.collect::<std::result::Result<Vec<_>,_>>()?;Ok(json!({"count":count,"first_started_at":first,"last_started_at":last,"level_units":units}))};
        let mut sweeps=totals("sweeps")?;let mut receiver=totals("observations")?;let mut query=SweepQuery::session(&session);query.stream_id=stream.clone();query.limit=1;let latest=query_sweeps(db,&query,maximum)?.items.into_iter().next();
        let peak=if let Some(value)=&latest{let values=value["levels"].as_array().ok_or_else(||ArchiveError::Corrupt("sweep levels".into()))?;let mut index=0;for i in 1..values.len(){if values[i].as_f64().ok_or_else(||ArchiveError::Corrupt("sweep level".into()))?>values[index].as_f64().ok_or_else(||ArchiveError::Corrupt("sweep level".into()))?{index=i;}}json!({"sweep_id":value["id"],"frequency_hz":value["frequencies_hz"][index],"level":values[index],"level_unit":value["level_unit"]})}else{Value::Null};
        sweeps["latest"]=latest.unwrap_or(Value::Null);sweeps["latest_peak"]=peak;receiver["latest"]=observations(db,&session,stream.as_deref(),1,maximum)?.into_iter().next().unwrap_or(Value::Null);
        let (known,open)=db.query_row(&format!("SELECT COALESCE(SUM(squelch_open IS NOT NULL),0),COALESCE(SUM(squelch_open=1),0) FROM observations WHERE session_id=?{clause}"),params_from_iter(arguments.iter()),|row|Ok((row.get::<_,i64>(0)?,row.get::<_,i64>(1)?)))?;
        receiver["squelch_known"]=json!(known);receiver["squelch_open"]=json!(open);receiver["squelch_open_ratio"]=if known==0{Value::Null}else{json!((open as f64)/(known as f64))};Ok(json!({"session_id":session,"stream_id":stream,"archive_wide":true,"sweeps":sweeps,"observations":receiver}))
    }).await
    }
}

#[cfg(test)]
mod guard_sampling_tests {
    use super::*;

    #[test]
    fn python_timedelta_float_targets_and_total_seconds_keep_exact_rounding() {
        let oracle: Value = serde_json::from_str(include_str!(
            "../tests/fixtures/guard_selection_rounding.json"
        ))
        .unwrap();
        for row in oracle.as_array().unwrap() {
            let micros = row["micros"].as_i64().unwrap();
            let index = row["index"].as_u64().unwrap() as usize;
            assert_eq!(
                sample_offset_micros(micros, index),
                row["rounded"].as_i64().unwrap(),
                "{row}"
            );
            assert_eq!(
                micros_seconds(micros).to_bits(),
                row["seconds"].as_f64().unwrap().to_bits(),
                "{row}"
            );
        }
    }
}
