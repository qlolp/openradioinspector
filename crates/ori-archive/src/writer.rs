use crate::{ArchiveError, CommittedBatch, EventDraft, Mutation, Result, rows, schema, validate};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

fn string<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().expect("validated string")
}
fn optional<'a>(value: &'a Value, key: &str) -> Option<&'a str> {
    value.get(key).and_then(Value::as_str)
}
fn encode(value: &Value) -> Result<String> {
    Ok(serde_json::to_string(value)?)
}
fn owner(db: &Connection, table: &str, id: &str, session: &str) -> Result<()> {
    let sql = match table {
        "streams" => "SELECT session_id FROM streams WHERE id=?",
        "candidates" => "SELECT session_id FROM candidates WHERE id=?",
        "investigations" => "SELECT session_id FROM investigations WHERE id=?",
        _ => return Err(crate::error::invalid("owner table")),
    };
    if let Some(existing) = db
        .query_row(sql, [id], |r| r.get::<_, String>(0))
        .optional()?
        && existing != session
    {
        return Err(ArchiveError::Conflict);
    }
    Ok(())
}
fn stream_owner(db: &Connection, sid: &str, stream: &str, unit: Option<&str>) -> Result<()> {
    let existing = db
        .query_row(
            "SELECT session_id,level_unit FROM streams WHERE id=?",
            [stream],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()?;
    match existing {
        Some((owner, rawunit)) if owner == sid && unit.is_none_or(|u| u == rawunit) => Ok(()),
        _ => Err(ArchiveError::Conflict),
    }
}
fn event(db: &Connection, draft: EventDraft, output: &mut CommittedBatch) -> Result<()> {
    let sequence: i64 =
        db.query_row("SELECT COALESCE(MAX(sequence),0)+1 FROM events", [], |r| {
            r.get(0)
        })?;
    if sequence < 1 {
        return Err(ArchiveError::Corrupt("event sequence exhausted".into()));
    }
    db.execute("INSERT INTO events(sequence,event_type,occurred_at,session_id,payload_json) VALUES(?,?,?,?,?)",params![sequence,draft.event_type,draft.occurred_at,draft.session_id,encode(&draft.payload)?])?;
    let value = json!({"sequence":sequence,"event_type":draft.event_type,"occurred_at":draft.occurred_at,"session_id":draft.session_id,"payload":draft.payload});
    output.first_sequence.get_or_insert(sequence as u64);
    output.last_sequence = Some(sequence as u64);
    let bytes = serde_json::to_vec(&value)?.len();
    if output.events_complete
        && output.events.len() < 256
        && bytes
            <= output
                .capture_budget
                .saturating_sub(output.captured_event_bytes)
    {
        output.captured_event_bytes += bytes;
        output.events.push(value);
    } else {
        output.events_complete = false;
    }
    Ok(())
}
fn session(db: &Connection, value: &Value, create: bool) -> Result<Value> {
    let id = string(value, "id");
    if create && rows::exists(db, "sessions", id)? {
        return Err(ArchiveError::Conflict);
    }
    db.execute("INSERT INTO sessions(id,contract_version,name,state,created_at,started_at,ended_at,metadata_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET contract_version=excluded.contract_version,name=excluded.name,state=excluded.state,created_at=excluded.created_at,started_at=excluded.started_at,ended_at=excluded.ended_at,metadata_json=excluded.metadata_json",params![id,value["contract_version"].as_i64(),string(value,"name"),string(value,"state"),string(value,"created_at"),optional(value,"started_at"),optional(value,"ended_at"),encode(&value["metadata"])?])?;
    for stream in value["streams"].as_array().expect("validated streams") {
        owner(db, "streams", string(stream, "id"), id)?;
        db.execute("INSERT INTO streams(id,session_id,device,role,level_unit,label,port,model,serial_number,firmware,state,capabilities_json,settings_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET device=excluded.device,role=excluded.role,level_unit=excluded.level_unit,label=excluded.label,port=excluded.port,model=excluded.model,serial_number=excluded.serial_number,firmware=excluded.firmware,state=excluded.state,capabilities_json=excluded.capabilities_json,settings_json=excluded.settings_json",params![string(stream,"id"),id,string(stream,"device"),string(stream,"role"),string(stream,"level_unit"),string(stream,"label"),optional(stream,"port"),optional(stream,"model"),optional(stream,"serial_number"),optional(stream,"firmware"),string(stream,"state"),encode(&stream["capabilities"] )?,encode(&stream["settings"] )?])?;
    }
    rows::session(db, id)?.ok_or(ArchiveError::NotFound)
}
fn sweep(db: &Connection, value: &Value) -> Result<u64> {
    let id = string(value, "id");
    let sid = string(value, "session_id");
    let stream = string(value, "stream_id");
    stream_owner(db, sid, stream, Some(string(value, "level_unit")))?;
    let start = string(&value["interval"], "started_at");
    let end = string(&value["interval"], "ended_at");
    db.execute("INSERT INTO sweeps(id,session_id,stream_id,started_at,ended_at,frequencies_json,levels_json,level_unit,settings_json) VALUES(?,?,?,?,?,?,?,?,?)",params![id,sid,stream,start,end,encode(&value["frequencies_hz"] )?,encode(&value["levels"] )?,string(value,"level_unit"),encode(&value["settings"] )?])?;
    let frequencies = value["frequencies_hz"]
        .as_array()
        .expect("validated frequencies");
    db.execute(
        "INSERT INTO monitoring_sweep_index VALUES(?,?,?,?,?,?)",
        params![
            id,
            sid,
            start,
            end,
            frequencies.first().and_then(Value::as_i64),
            frequencies.last().and_then(Value::as_i64)
        ],
    )?;
    db.execute(
        "INSERT INTO sweep_ingestion(sweep_id,session_id,stream_id) VALUES(?,?,?)",
        params![id, sid, stream],
    )?;
    Ok(db.last_insert_rowid() as u64)
}
fn observation(db: &Connection, value: &Value) -> Result<()> {
    let id = string(value, "id");
    let sid = string(value, "session_id");
    let stream = string(value, "stream_id");
    stream_owner(db, sid, stream, Some(string(value, "level_unit")))?;
    let level = value["level"].as_f64();
    db.execute("INSERT INTO observations(id,session_id,stream_id,started_at,ended_at,frequency_hz,mode,level,level_unit,squelch_open,decoded_mode,audio_uri,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",params![id,sid,stream,string(&value["interval"],"started_at"),string(&value["interval"],"ended_at"),value["frequency_hz"].as_i64(),string(value,"mode"),level,string(value,"level_unit"),value["squelch_open"].as_bool(),optional(value,"decoded_mode"),optional(value,"audio_uri"),encode(&value["metadata"] )?])?;
    if let Some(level) = level {
        db.execute(
            "INSERT INTO ori_observation_level_bits(observation_id,level_bits_le) VALUES(?,?)",
            params![id, &level.to_bits().to_le_bytes()[..]],
        )?;
    }
    Ok(())
}
fn candidate(db: &Connection, value: &Value) -> Result<()> {
    let id = string(value, "id");
    let sid = string(value, "session_id");
    owner(db, "candidates", id, sid)?;
    stream_owner(
        db,
        sid,
        string(value, "source_stream_id"),
        Some(string(value, "level_unit")),
    )?;
    db.execute("INSERT INTO candidates(id,session_id,source_stream_id,started_at,ended_at,center_frequency_hz,bandwidth_hz,peak_level,level_unit,score,observation_ids_json,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at,ended_at=excluded.ended_at,center_frequency_hz=excluded.center_frequency_hz,bandwidth_hz=excluded.bandwidth_hz,peak_level=excluded.peak_level,level_unit=excluded.level_unit,score=excluded.score,observation_ids_json=excluded.observation_ids_json,state=excluded.state",params![id,sid,string(value,"source_stream_id"),string(&value["interval"],"started_at"),string(&value["interval"],"ended_at"),value["center_frequency_hz"].as_i64(),value["bandwidth_hz"].as_i64(),value["peak_level"].as_f64(),string(value,"level_unit"),value["score"].as_f64(),encode(&value["observation_ids"] )?,string(value,"state")])?;
    crate::candidate_precision::save(db, value)?;
    Ok(())
}
fn investigation(db: &Connection, sid: &str, value: &Value, source: &Value) -> Result<()> {
    let id = string(value, "id");
    owner(db, "investigations", id, sid)?;
    stream_owner(db, sid, string(value, "target_stream_id"), None)?;
    candidate(db, source)?;
    db.execute("INSERT INTO investigations(id,session_id,candidate_id,target_stream_id,target_frequency_hz,state,priority,mode,created_at,started_at,ended_at,result_observation_ids_json,error,listening_started_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET target_frequency_hz=excluded.target_frequency_hz,state=excluded.state,priority=excluded.priority,mode=excluded.mode,started_at=excluded.started_at,ended_at=excluded.ended_at,result_observation_ids_json=excluded.result_observation_ids_json,error=excluded.error,listening_started_at=excluded.listening_started_at",params![id,sid,string(value,"candidate_id"),string(value,"target_stream_id"),value["target_frequency_hz"].as_i64(),string(value,"state"),value["priority"].as_i64(),optional(value,"mode"),string(value,"created_at"),optional(value,"started_at"),optional(value,"ended_at"),encode(&value["result_observation_ids"] )?,optional(value,"error"),optional(value,"listening_started_at")])?;
    Ok(())
}

pub(crate) fn apply(
    db: &mut Connection,
    mutations: Vec<Mutation>,
    limit: Option<u64>,
    capture_budget: usize,
    lifecycle: &crate::session_lifecycle::Registry,
) -> Result<CommittedBatch> {
    #[cfg(test)]
    lifecycle.before_apply();
    let measurement = mutations.iter().any(|m| {
        matches!(
            m,
            Mutation::Sweeps(_) | Mutation::Observations(_) | Mutation::Artifact(_)
        )
    });
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if measurement {
        schema::capacity_check(&tx, limit, true)?;
    }
    let mut result = CommittedBatch {
        items: Vec::new(),
        events: Vec::new(),
        ingestion_boundary: None,
        first_sequence: None,
        last_sequence: None,
        events_complete: true,
        reservation: None,
        captured_event_bytes: 0,
        capture_budget,
    };
    let mut lifecycle_changes = crate::session_lifecycle::Changes::default();
    for mutation in mutations {
        let before = lifecycle.before(&tx, &mutation)?;
        match mutation {
            Mutation::StopSession { value, at } => {
                let sid = string(&value, "id");
                let current = rows::session(&tx, sid)?.ok_or(ArchiveError::NotFound)?;
                if current["state"] != "running" {
                    return Err(ArchiveError::Conflict);
                }
                let saved = session(&tx, &value, false)?;
                event(
                    &tx,
                    EventDraft {
                        event_type: "session.stopped".into(),
                        occurred_at: at.clone(),
                        session_id: Some(sid.into()),
                        payload: json!({"session":saved}),
                    },
                    &mut result,
                )?;
                result.items.push(saved);
                let mut after = 0_i64;
                loop {
                    let mut statement=tx.prepare("SELECT rowid,* FROM investigations WHERE session_id=? AND rowid>? AND state IN ('queued','tuning','listening','decoding') ORDER BY rowid LIMIT 128")?;
                    let mut records = statement.query(params![sid, after])?;
                    let mut jobs = Vec::new();
                    while let Some(row) = records.next()? {
                        jobs.push((row.get::<_, i64>("rowid")?, rows::investigation(row)?));
                    }
                    drop(records);
                    drop(statement);
                    if jobs.is_empty() {
                        break;
                    }
                    for (rowid, mut job) in jobs {
                        after = rowid;
                        let mut statement =
                            tx.prepare(&format!("{} WHERE c.id=?", rows::CANDIDATE_SELECT))?;
                        let mut records = statement.query([string(&job, "candidate_id")])?;
                        let mut source = records
                            .next()?
                            .map(rows::candidate)
                            .transpose()?
                            .ok_or(ArchiveError::Conflict)?;
                        drop(records);
                        drop(statement);
                        source["state"] = json!("queued");
                        job["state"] = json!("cancelled");
                        job["ended_at"] = json!(at);
                        job["error"] = Value::Null;
                        investigation(&tx, sid, &job, &source)?;
                        event(
                            &tx,
                            EventDraft {
                                event_type: "investigation.cancelled".into(),
                                occurred_at: at.clone(),
                                session_id: Some(sid.into()),
                                payload: json!({"investigation":job,"candidate":source}),
                            },
                            &mut result,
                        )?;
                    }
                }
            }
            Mutation::ResetRuntimeStreamStates => {
                let reset = tx.execute(
                    "UPDATE streams SET state='disconnected' WHERE state!='disconnected'",
                    [],
                )?;
                result.items.push(json!({"reset_streams":reset}));
            }
            Mutation::CreateSession(value) => {
                let saved = session(&tx, &value, true)?;
                event(
                    &tx,
                    EventDraft::now(
                        "session.created",
                        Some(string(&saved, "id").into()),
                        json!({"session":saved}),
                    ),
                    &mut result,
                )?;
                result.items.push(saved);
            }
            Mutation::SaveSession {
                value,
                event: draft,
            } => {
                result.items.push(session(&tx, &value, false)?);
                if let Some(draft) = draft {
                    event(&tx, draft, &mut result)?;
                }
            }
            Mutation::Sweeps(values) => {
                for value in values {
                    result.ingestion_boundary = Some(sweep(&tx, &value)?);
                    event(
                        &tx,
                        EventDraft::now(
                            "sweep.received",
                            Some(string(&value, "session_id").into()),
                            json!({"sweep":value}),
                        ),
                        &mut result,
                    )?;
                    result.items.push(value);
                }
            }
            Mutation::Observations(values) => {
                for value in values {
                    observation(&tx, &value)?;
                    event(
                        &tx,
                        EventDraft::now(
                            "observation.received",
                            Some(string(&value, "session_id").into()),
                            json!({"observation":value}),
                        ),
                        &mut result,
                    )?;
                    result.items.push(value);
                }
            }
            Mutation::Event(draft) => event(&tx, draft, &mut result)?,
            Mutation::Monitoring { kind, value } => {
                let id = string(&value, "id");
                let project = value
                    .get("project_id")
                    .and_then(Value::as_str)
                    .unwrap_or(id);
                let created = validate::utc(string(&value, "created_at"))?;
                tx.execute("INSERT INTO monitoring_records(kind,id,project_id,created_at,payload) VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload",params![kind,id,project,created,encode(&value)?])?;
                if kind == "project" {
                    for sid in value["session_ids"]
                        .as_array()
                        .expect("validated membership")
                    {
                        let sid = sid.as_str().expect("validated member");
                        if !rows::exists(&tx, "sessions", sid)? {
                            return Err(ArchiveError::Conflict);
                        }
                        tx.execute(
                            "INSERT OR IGNORE INTO monitoring_sessions VALUES(?,?)",
                            params![id, sid],
                        )?;
                    }
                }
                result.items.push(value);
            }
            Mutation::Candidate {
                value,
                event: draft,
            } => {
                candidate(&tx, &value)?;
                result.items.push(value);
                if let Some(draft) = draft {
                    event(&tx, draft, &mut result)?;
                }
            }
            Mutation::Investigation {
                session_id,
                value,
                candidate: source,
                event: draft,
            } => {
                investigation(&tx, &session_id, &value, &source)?;
                result.items.push(value);
                if let Some(draft) = draft {
                    event(&tx, draft, &mut result)?;
                }
            }
            Mutation::MeasurementResult {
                value,
                event: draft,
            } => {
                tx.execute("INSERT INTO measurement_results(id,session_id,stream_id,methodology,result_code,frequency_hz,started_at,ended_at,values_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",params![string(&value,"id"),string(&value,"session_id"),string(&value,"stream_id"),string(&value,"methodology"),string(&value,"result_code"),value["frequency_hz"].as_i64(),string(&value["interval"],"started_at"),string(&value["interval"],"ended_at"),encode(&value["values"] )?,string(&value,"created_at")])?;
                result.items.push(value);
                if let Some(draft) = draft {
                    event(&tx, draft, &mut result)?;
                }
            }
            Mutation::Reference {
                value,
                event: draft,
            } => {
                let sid = string(&value, "session_id");
                let name = string(&value, "name");
                let mut stmt =
                    tx.prepare("SELECT * FROM sweep_references WHERE session_id=? AND name=?")?;
                let mut cursor = stmt.query(params![sid, name])?;
                if let Some(row) = cursor.next()? {
                    let existing = rows::reference(row)?;
                    if existing["sweep_id"] != value["sweep_id"] {
                        return Err(ArchiveError::Conflict);
                    }
                    result.items.push(existing);
                    continue;
                }
                let count: i64 = tx.query_row(
                    "SELECT COUNT(*) FROM sweep_references WHERE session_id=?",
                    [sid],
                    |r| r.get(0),
                )?;
                if count >= 100 {
                    return Err(ArchiveError::Conflict);
                }
                tx.execute("INSERT INTO sweep_references(id,session_id,stream_id,sweep_id,name,created_at,started_at,ended_at,level_unit,points) VALUES(?,?,?,?,?,?,?,?,?,?)",params![string(&value,"id"),sid,string(&value,"stream_id"),string(&value,"sweep_id"),name,string(&value,"created_at"),string(&value["interval"],"started_at"),string(&value["interval"],"ended_at"),string(&value,"level_unit"),value["points"].as_i64()])?;
                result.items.push(value);
                event(&tx, draft, &mut result)?;
            }
            Mutation::Artifact(item) => {
                stream_owner(&tx, &item.session_id, &item.stream_id, None)?;
                tx.execute("INSERT INTO instrument_artifacts(id,session_id,stream_id,schema_version,kind,mime_type,filename,sha256,created_at,payload,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",params![item.id,item.session_id,item.stream_id,item.schema_version,item.kind,item.mime_type,item.filename,item.sha256,item.created_at,item.payload,encode(&item.metadata)?])?;
                result.items.push(json!({"id":item.id,"session_id":item.session_id,"stream_id":item.stream_id,"schema_version":item.schema_version,"kind":item.kind,"mime_type":item.mime_type,"filename":item.filename,"sha256":item.sha256,"created_at":item.created_at,"size_bytes":item.payload.len(),"metadata":item.metadata}));
            }
            Mutation::Webhook {
                project_id,
                url,
                secret,
            } => {
                tx.execute("INSERT INTO monitoring_webhooks VALUES(?,?,?) ON CONFLICT(project_id) DO UPDATE SET url=excluded.url,secret=excluded.secret",params![project_id,url,secret])?;
            }
            Mutation::Notification(value) => {
                tx.execute("INSERT INTO monitoring_notifications(id,project_id,created_at,payload,state,attempts,next_at,error,lease_until,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,state=excluded.state,attempts=excluded.attempts,next_at=excluded.next_at,error=excluded.error,lease_until=excluded.lease_until,acknowledged_at=excluded.acknowledged_at",params![string(&value,"id"),string(&value,"project_id"),string(&value,"created_at"),encode(&value["payload"] )?,string(&value,"state"),value["attempts"].as_i64(),optional(&value,"next_at"),optional(&value,"error"),optional(&value,"lease_until"),optional(&value,"acknowledged_at")])?;
                result.items.push(value);
            }
            Mutation::EnqueueNotification(value) => {
                let actual = crate::outbox::insert(&tx, &value, capture_budget)?;
                result.items.push(serde_json::to_value(actual)?);
            }
        }
        lifecycle_changes.after(&tx, before)?;
    }
    if measurement {
        schema::capacity_check(&tx, limit, false)?;
    }
    let pending = lifecycle.prepare(lifecycle_changes)?;
    #[cfg(test)]
    lifecycle.before_commit(&tx);
    if let Err(error) = tx.commit() {
        pending.rollback(db)?;
        return Err(error.into());
    }
    #[cfg(test)]
    lifecycle.after_commit();
    pending.publish();
    Ok(result)
}

pub(crate) fn outbox(
    db: &mut Connection,
    operation: crate::outbox::OutboxOperation,
    maximum: usize,
) -> Result<crate::outbox::OutboxReply> {
    crate::outbox::apply_operation(db, operation, maximum)
}
