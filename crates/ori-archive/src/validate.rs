use crate::{Artifact, EventDraft, Mutation, Result, error::invalid};
use chrono::{DateTime, SecondsFormat, Timelike, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub(crate) const KINDS: &[&str] = &[
    "project",
    "baseline",
    "allowed",
    "event",
    "event-source",
    "quality",
    "source-search",
    "position-mark",
    "scan-plan",
];
pub(crate) fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.trim().is_empty())
        .ok_or_else(|| invalid(key))
}
pub(crate) fn integer(value: &Value, key: &str) -> Result<i64> {
    value
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| invalid(key))
}
pub(crate) fn hz(value: &Value, key: &str) -> Result<i64> {
    let result = integer(value, key)?;
    if result < 0 {
        return Err(invalid(key));
    }
    Ok(result)
}
pub(crate) fn object(value: &Value, key: &str) -> Result<()> {
    if !value.get(key).is_some_and(Value::is_object) {
        return Err(invalid(key));
    }
    Ok(())
}
pub(crate) fn strings(value: &Value, key: &str) -> Result<()> {
    if !value
        .get(key)
        .and_then(Value::as_array)
        .is_some_and(|a| a.iter().all(Value::is_string))
    {
        return Err(invalid(key));
    }
    Ok(())
}
pub(crate) fn utc(raw: &str) -> Result<String> {
    let parsed = DateTime::parse_from_rfc3339(raw)
        .or_else(|_| DateTime::parse_from_str(raw, "%Y-%m-%d %H:%M:%S%.f%:z"))
        .map_err(|_| invalid("UTC timestamp"))?;
    let dt = parsed.with_timezone(&Utc);
    let dt = dt
        .with_nanosecond(dt.nanosecond() / 1000 * 1000)
        .ok_or_else(|| invalid("UTC precision"))?;
    Ok(dt.to_rfc3339_opts(SecondsFormat::Micros, true))
}
fn time(value: &mut Value, key: &str, optional: bool) -> Result<()> {
    if optional && value.get(key).is_none_or(Value::is_null) {
        value[key] = Value::Null;
        return Ok(());
    }
    value[key] = json!(utc(text(value, key)?)?);
    Ok(())
}
fn interval(value: &mut Value) -> Result<()> {
    let range = value
        .get_mut("interval")
        .filter(|x| x.is_object())
        .ok_or_else(|| invalid("interval"))?;
    time(range, "started_at", false)?;
    time(range, "ended_at", false)?;
    if range["ended_at"].as_str() < range["started_at"].as_str() {
        return Err(invalid("ended_at precedes started_at"));
    }
    Ok(())
}
fn default(value: &mut Value, key: &str, fallback: Value) {
    if value.get(key).is_none() {
        value[key] = fallback;
    }
}
fn choice(value: &Value, key: &str, choices: &[&str]) -> Result<()> {
    if !choices.contains(&text(value, key)?) {
        return Err(invalid(key));
    }
    Ok(())
}
fn finite_optional(value: &Value, key: &str) -> Result<()> {
    if let Some(number) = value.get(key).filter(|x| !x.is_null())
        && !number.as_f64().is_some_and(f64::is_finite)
    {
        return Err(invalid(key));
    }
    Ok(())
}

pub(crate) fn session(value: &mut Value) -> Result<()> {
    text(value, "id")?;
    text(value, "name")?;
    default(value, "state", json!("draft"));
    choice(value, "state", &["draft", "running", "stopped", "failed"])?;
    default(value, "contract_version", json!(1));
    if integer(value, "contract_version")? != 1 {
        return Err(invalid("contract_version"));
    }
    time(value, "created_at", false)?;
    time(value, "started_at", true)?;
    time(value, "ended_at", true)?;
    default(value, "metadata", json!({}));
    object(value, "metadata")?;
    let id = text(value, "id")?.to_owned();
    let streams = value
        .get_mut("streams")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| invalid("streams"))?;
    if streams.len() > 128 {
        return Err(invalid("stream count"));
    }
    let mut ids = std::collections::HashSet::new();
    let mut ports = std::collections::HashSet::new();
    for stream in streams {
        if !stream.is_object() {
            return Err(invalid("stream"));
        }
        let stream_id = text(stream, "id")?.to_owned();
        if !ids.insert(stream_id) {
            return Err(invalid("duplicate stream ID"));
        }
        if let Some(sid) = stream.get("session_id")
            && sid.as_str() != Some(&id)
        {
            return Err(invalid("stream session owner"));
        }
        stream["session_id"] = json!(id);
        text(stream, "device")?;
        choice(
            stream,
            "role",
            &["spectrum_analyzer", "receiver", "simulator"],
        )?;
        text(stream, "level_unit")?;
        default(stream, "state", json!("disconnected"));
        choice(
            stream,
            "state",
            &[
                "disconnected",
                "connecting",
                "ready",
                "running",
                "degraded",
                "error",
            ],
        )?;
        default(stream, "label", json!(""));
        if !stream["label"].is_string() {
            return Err(invalid("label"));
        }
        for key in ["port", "model", "serial_number", "firmware"] {
            default(stream, key, Value::Null);
            if !stream[key].is_null() && !stream[key].is_string() {
                return Err(invalid(key));
            }
        }
        if let Some(port) = stream["port"].as_str().filter(|p| !p.is_empty())
            && !ports.insert(port.to_owned())
        {
            return Err(invalid("duplicate port"));
        }
        default(stream, "capabilities", json!([]));
        strings(stream, "capabilities")?;
        default(stream, "settings", json!({}));
        object(stream, "settings")?;
    }
    Ok(())
}
pub(crate) fn sweep(value: &mut Value) -> Result<()> {
    for key in ["id", "session_id", "stream_id", "level_unit"] {
        text(value, key)?;
    }
    interval(value)?;
    default(value, "settings", json!({}));
    object(value, "settings")?;
    let frequencies = value["frequencies_hz"]
        .as_array()
        .ok_or_else(|| invalid("frequencies_hz"))?;
    let levels = value["levels"]
        .as_array()
        .ok_or_else(|| invalid("levels"))?;
    if frequencies.is_empty() || frequencies.len() != levels.len() {
        return Err(invalid("sweep dimensions"));
    }
    let mut last = -1;
    for frequency in frequencies {
        let next = frequency
            .as_i64()
            .filter(|n| *n >= 0)
            .ok_or_else(|| invalid("integer frequencies_hz"))?;
        if next < last {
            return Err(invalid("frequency order"));
        }
        last = next;
    }
    if !levels
        .iter()
        .all(|number| number.as_f64().is_some_and(f64::is_finite))
    {
        return Err(invalid("finite raw levels"));
    }
    Ok(())
}
pub(crate) fn observation(value: &mut Value) -> Result<()> {
    for key in ["id", "session_id", "stream_id", "level_unit", "mode"] {
        text(value, key)?;
    }
    interval(value)?;
    hz(value, "frequency_hz")?;
    default(value, "metadata", json!({}));
    object(value, "metadata")?;
    for key in ["level", "squelch_open", "decoded_mode", "audio_uri"] {
        default(value, key, Value::Null);
    }
    finite_optional(value, "level")?;
    if !value["squelch_open"].is_null() && !value["squelch_open"].is_boolean() {
        return Err(invalid("squelch_open"));
    }
    for key in ["decoded_mode", "audio_uri"] {
        if !value[key].is_null() && !value[key].is_string() {
            return Err(invalid(key));
        }
    }
    Ok(())
}
pub(crate) fn event(event: &mut EventDraft) -> Result<()> {
    if event.event_type.trim().is_empty() || !event.payload.is_object() {
        return Err(invalid("event"));
    }
    event.occurred_at = utc(&event.occurred_at)?;
    Ok(())
}
pub(crate) fn artifact(value: &mut Artifact) -> Result<()> {
    if [&value.id, &value.session_id, &value.stream_id]
        .iter()
        .any(|s| s.trim().is_empty())
        || value.schema_version != 1
        || value.payload.len() > 32 * 1024 * 1024
        || !value.metadata.is_object()
    {
        return Err(invalid("artifact"));
    }
    if ![
        "screen",
        "sd_file",
        "raw_scan",
        "zero_span",
        "processed_trace",
        "measurement",
        "diagnostic",
        "audio",
    ]
    .contains(&value.kind.as_str())
        || ![
            "image/png",
            "application/octet-stream",
            "application/json",
            "text/csv",
            "audio/wav",
        ]
        .contains(&value.mime_type.as_str())
        || value.filename.len() > 80
        || value.filename.is_empty()
        || !value.filename.as_bytes()[0].is_ascii_alphanumeric()
        || !value
            .filename
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
        || value.filename.contains("..")
    {
        return Err(invalid("artifact kind, MIME or filename"));
    }
    if Sha256::digest(&value.payload)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>()
        != value.sha256
    {
        return Err(invalid("artifact SHA-256"));
    }
    value.created_at = utc(&value.created_at)?;
    Ok(())
}
pub(crate) fn candidate(value: &mut Value) -> Result<()> {
    for key in ["id", "session_id", "source_stream_id", "level_unit"] {
        text(value, key)?;
    }
    interval(value)?;
    if hz(value, "center_frequency_hz")? == 0 {
        return Err(invalid("center_frequency_hz"));
    }
    default(value, "state", json!("new"));
    choice(
        value,
        "state",
        &["new", "queued", "investigating", "resolved", "dismissed"],
    )?;
    for key in ["bandwidth_hz", "peak_level", "score"] {
        default(value, key, Value::Null);
    }
    if !value["bandwidth_hz"].is_null() && hz(value, "bandwidth_hz")? == 0 {
        return Err(invalid("bandwidth_hz"));
    }
    finite_optional(value, "peak_level")?;
    finite_optional(value, "score")?;
    default(value, "observation_ids", json!([]));
    strings(value, "observation_ids")?;
    Ok(())
}
pub(crate) fn mutations(values: &mut [Mutation]) -> Result<()> {
    if values.is_empty() || values.len() > 256 {
        return Err(invalid("mutation batch count"));
    }
    for mutation in values {
        match mutation {
            Mutation::ResetRuntimeStreamStates => {}
            Mutation::StopSession { value, at } => {
                if !value.is_object() {
                    return Err(invalid("session object"));
                }
                *at = utc(at)?;
                value["state"] = json!("stopped");
                value["ended_at"] = json!(at);
                session(value)?;
                if value["started_at"]
                    .as_str()
                    .is_some_and(|start| start > at.as_str())
                {
                    return Err(invalid("stop before session start"));
                }
            }
            Mutation::CreateSession(value) | Mutation::SaveSession { value, .. } => session(value)?,
            Mutation::Sweeps(items) => {
                if items.is_empty() || items.len() > 128 {
                    return Err(invalid("source batch count"));
                }
                let mut ids = std::collections::HashSet::new();
                for value in items {
                    if !ids.insert(text(value, "id")?.to_owned()) {
                        return Err(invalid("duplicate source ID"));
                    }
                    sweep(value)?;
                }
            }
            Mutation::Observations(items) => {
                if items.is_empty() || items.len() > 128 {
                    return Err(invalid("source batch count"));
                }
                let mut ids = std::collections::HashSet::new();
                for value in items {
                    if !ids.insert(text(value, "id")?.to_owned()) {
                        return Err(invalid("duplicate source ID"));
                    }
                    observation(value)?;
                }
            }
            Mutation::Event(value) => event(value)?,
            Mutation::Monitoring { kind, value } => {
                if !KINDS.contains(&kind.as_str()) {
                    return Err(invalid("monitoring kind"));
                }
                text(value, "id")?;
                utc(text(value, "created_at")?)?;
                if value.get("project_id").is_some() {
                    text(value, "project_id")?;
                }
                if kind == "project" {
                    strings(value, "session_ids")?;
                }
            }
            Mutation::Candidate { value, .. } => candidate(value)?,
            Mutation::Investigation {
                session_id,
                value,
                candidate: source,
                ..
            } => {
                candidate(source)?;
                if text(source, "session_id")? != session_id
                    || text(value, "candidate_id")? != text(source, "id")?
                {
                    return Err(invalid("investigation candidate owner"));
                }
                for key in ["id", "candidate_id", "target_stream_id"] {
                    text(value, key)?;
                }
                if hz(value, "target_frequency_hz")? == 0 {
                    return Err(invalid("target_frequency_hz"));
                }
                default(value, "state", json!("queued"));
                choice(
                    value,
                    "state",
                    &[
                        "queued",
                        "tuning",
                        "listening",
                        "decoding",
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                    ],
                )?;
                default(value, "priority", json!(0));
                if integer(value, "priority")? < 0 {
                    return Err(invalid("priority"));
                }
                time(value, "created_at", false)?;
                for key in ["started_at", "ended_at", "listening_started_at"] {
                    time(value, key, true)?;
                }
                for key in ["mode", "error"] {
                    default(value, key, Value::Null);
                    if !value[key].is_null() && !value[key].is_string() {
                        return Err(invalid(key));
                    }
                }
                default(value, "result_observation_ids", json!([]));
                strings(value, "result_observation_ids")?;
            }
            Mutation::MeasurementResult { value, .. } => {
                for key in ["id", "session_id", "stream_id", "methodology"] {
                    text(value, key)?;
                }
                choice(value, "result_code", &["norm", "not_norm", "not_measured"])?;
                hz(value, "frequency_hz")?;
                interval(value)?;
                time(value, "created_at", false)?;
                object(value, "values")?;
            }
            Mutation::Reference { value, .. } => {
                for key in ["id", "session_id", "stream_id", "sweep_id", "name"] {
                    text(value, key)?;
                }
                interval(value)?;
                time(value, "created_at", false)?;
                if value["level_unit"] != "dBm" || !(1..=16384).contains(&integer(value, "points")?)
                {
                    return Err(invalid("reference raw shape"));
                }
            }
            Mutation::Artifact(value) => artifact(value)?,
            Mutation::Webhook {
                project_id,
                url,
                secret,
            } => {
                if project_id.is_empty() || url.len() > 8192 || secret.len() > 4096 {
                    return Err(invalid("webhook"));
                }
            }
            Mutation::Notification(value) => {
                for key in ["id", "project_id", "state"] {
                    text(value, key)?;
                }
                time(value, "created_at", false)?;
                object(value, "payload")?;
                if integer(value, "attempts")? < 0 {
                    return Err(invalid("notification attempts"));
                }
                for key in ["next_at", "lease_until", "acknowledged_at"] {
                    time(value, key, true)?;
                }
                default(value, "error", Value::Null);
                if !value["error"].is_null() && !value["error"].is_string() {
                    return Err(invalid("error"));
                }
            }
            Mutation::EnqueueNotification(value) => crate::outbox::validate_notification(value)?,
        }
        match mutation {
            Mutation::SaveSession {
                event: Some(draft), ..
            }
            | Mutation::Candidate {
                event: Some(draft), ..
            }
            | Mutation::Investigation {
                event: Some(draft), ..
            }
            | Mutation::MeasurementResult {
                event: Some(draft), ..
            }
            | Mutation::Reference { event: draft, .. } => event(draft)?,
            _ => {}
        }
    }
    Ok(())
}
