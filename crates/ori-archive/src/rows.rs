use crate::{ArchiveError, Artifact, Result, validate};
use rusqlite::{Connection, OptionalExtension, Row};
use serde_json::{Value, json};

pub(crate) fn object(raw: String) -> Result<Value> {
    let value = crate::parse_json_value(raw.as_bytes(), 32 * 1024 * 1024)?;
    if !value.is_object() {
        return Err(ArchiveError::Corrupt("expected JSON object".into()));
    }
    Ok(value)
}
fn json_column(row: &Row<'_>, key: &str) -> Result<Value> {
    let raw = row.get::<_, String>(key)?;
    crate::parse_json_value(raw.as_bytes(), 32 * 1024 * 1024)
}
fn time(row: &Row<'_>, key: &str) -> Result<String> {
    validate::utc(&row.get::<_, String>(key)?)
}
fn optional_time(row: &Row<'_>, key: &str) -> Result<Option<String>> {
    row.get::<_, Option<String>>(key)?
        .map(|s| validate::utc(&s))
        .transpose()
}
pub(crate) fn sweep(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"stream_id":row.get::<_,String>("stream_id")?,"interval":{"started_at":time(row,"started_at")?,"ended_at":time(row,"ended_at")?},"frequencies_hz":json_column(row,"frequencies_json")?,"levels":json_column(row,"levels_json")?,"level_unit":row.get::<_,String>("level_unit")?,"settings":object(row.get("settings_json")?)?}),
    )
}
pub(crate) fn observation(row: &Row<'_>) -> Result<Value> {
    let stored: Option<f64> = row.get("level")?;
    let bits: Option<Vec<u8>> = row.get("level_bits_le")?;
    let level = if let Some(bytes) = bits {
        let array: [u8; 8] = bytes
            .try_into()
            .map_err(|_| ArchiveError::Corrupt("observation precision length".into()))?;
        let precise = f64::from_le_bytes(array);
        if !precise.is_finite() || stored != Some(precise) {
            return Err(ArchiveError::Corrupt(
                "observation precision mismatch".into(),
            ));
        }
        Some(precise)
    } else {
        stored
    };
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"stream_id":row.get::<_,String>("stream_id")?,"interval":{"started_at":time(row,"started_at")?,"ended_at":time(row,"ended_at")?},"frequency_hz":row.get::<_,i64>("frequency_hz")?,"mode":row.get::<_,String>("mode")?,"level":level,"level_unit":row.get::<_,String>("level_unit")?,"squelch_open":row.get::<_,Option<bool>>("squelch_open")?,"decoded_mode":row.get::<_,Option<String>>("decoded_mode")?,"audio_uri":row.get::<_,Option<String>>("audio_uri")?,"metadata":object(row.get("metadata_json")?)?}),
    )
}
pub(crate) fn event(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"sequence":row.get::<_,i64>("sequence")?,"event_type":row.get::<_,String>("event_type")?,"occurred_at":time(row,"occurred_at")?,"session_id":row.get::<_,Option<String>>("session_id")?,"payload":object(row.get("payload_json")?)?}),
    )
}
pub(crate) fn session(db: &Connection, id: &str) -> Result<Option<Value>> {
    let mut stmt = db.prepare("SELECT * FROM sessions WHERE id=?")?;
    let mut rows = stmt.query([id])?;
    let Some(row) = rows.next()? else {
        return Ok(None);
    };
    let mut streams = Vec::new();
    let mut streams_stmt = db.prepare("SELECT * FROM streams WHERE session_id=? ORDER BY rowid")?;
    let mut cursor = streams_stmt.query([id])?;
    while let Some(stream) = cursor.next()? {
        streams.push(json!({"id":stream.get::<_,String>("id")?,"session_id":stream.get::<_,String>("session_id")?,"device":stream.get::<_,String>("device")?,"role":stream.get::<_,String>("role")?,"level_unit":stream.get::<_,String>("level_unit")?,"label":stream.get::<_,String>("label")?,"port":stream.get::<_,Option<String>>("port")?,"model":stream.get::<_,Option<String>>("model")?,"serial_number":stream.get::<_,Option<String>>("serial_number")?,"firmware":stream.get::<_,Option<String>>("firmware")?,"state":stream.get::<_,String>("state")?,"capabilities":json_column(stream,"capabilities_json")?,"settings":object(stream.get("settings_json")?)?}));
    }
    Ok(Some(
        json!({"id":row.get::<_,String>("id")?,"contract_version":row.get::<_,i64>("contract_version")?,"name":row.get::<_,String>("name")?,"state":row.get::<_,String>("state")?,"created_at":time(row,"created_at")?,"started_at":optional_time(row,"started_at")?,"ended_at":optional_time(row,"ended_at")?,"metadata":object(row.get("metadata_json")?)?,"streams":streams}),
    ))
}
pub(crate) fn get_sweep(db: &Connection, id: &str) -> Result<Option<Value>> {
    let mut stmt = db.prepare("SELECT * FROM sweeps WHERE id=?")?;
    let mut rows = stmt.query([id])?;
    rows.next()?.map(sweep).transpose()
}
pub(crate) fn get_observation(db: &Connection, id: &str) -> Result<Option<Value>> {
    let mut stmt=db.prepare("SELECT o.*,p.level_bits_le FROM observations o LEFT JOIN ori_observation_level_bits p ON p.observation_id=o.id WHERE o.id=?")?;
    let mut rows = stmt.query([id])?;
    rows.next()?.map(observation).transpose()
}
pub(crate) fn candidate(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"source_stream_id":row.get::<_,String>("source_stream_id")?,"interval":{"started_at":time(row,"started_at")?,"ended_at":time(row,"ended_at")?},"center_frequency_hz":row.get::<_,i64>("center_frequency_hz")?,"bandwidth_hz":row.get::<_,Option<i64>>("bandwidth_hz")?,"peak_level":row.get::<_,Option<f64>>("peak_level")?,"score":row.get::<_,Option<f64>>("score")?,"level_unit":row.get::<_,String>("level_unit")?,"observation_ids":json_column(row,"observation_ids_json")?,"state":row.get::<_,String>("state")?}),
    )
}
pub(crate) fn investigation(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"id":row.get::<_,String>("id")?,"candidate_id":row.get::<_,String>("candidate_id")?,"target_stream_id":row.get::<_,String>("target_stream_id")?,"target_frequency_hz":row.get::<_,i64>("target_frequency_hz")?,"state":row.get::<_,String>("state")?,"priority":row.get::<_,i64>("priority")?,"mode":row.get::<_,Option<String>>("mode")?,"created_at":time(row,"created_at")?,"started_at":optional_time(row,"started_at")?,"ended_at":optional_time(row,"ended_at")?,"listening_started_at":optional_time(row,"listening_started_at")?,"result_observation_ids":json_column(row,"result_observation_ids_json")?,"error":row.get::<_,Option<String>>("error")?}),
    )
}
pub(crate) fn measurement(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"stream_id":row.get::<_,String>("stream_id")?,"methodology":row.get::<_,String>("methodology")?,"result_code":row.get::<_,String>("result_code")?,"frequency_hz":row.get::<_,i64>("frequency_hz")?,"interval":{"started_at":time(row,"started_at")?,"ended_at":time(row,"ended_at")?},"values":object(row.get("values_json")?)?,"created_at":time(row,"created_at")?}),
    )
}
pub(crate) fn reference(row: &Row<'_>) -> Result<Value> {
    Ok(
        json!({"id":row.get::<_,String>("id")?,"session_id":row.get::<_,String>("session_id")?,"stream_id":row.get::<_,String>("stream_id")?,"sweep_id":row.get::<_,String>("sweep_id")?,"name":row.get::<_,String>("name")?,"created_at":time(row,"created_at")?,"interval":{"started_at":time(row,"started_at")?,"ended_at":time(row,"ended_at")?},"level_unit":row.get::<_,String>("level_unit")?,"points":row.get::<_,i64>("points")?}),
    )
}
pub(crate) fn artifact(row: &Row<'_>) -> Result<Artifact> {
    let mut item = Artifact {
        id: row.get("id")?,
        session_id: row.get("session_id")?,
        stream_id: row.get("stream_id")?,
        schema_version: row.get("schema_version")?,
        kind: row.get("kind")?,
        mime_type: row.get("mime_type")?,
        filename: row.get("filename")?,
        sha256: row.get("sha256")?,
        created_at: row.get("created_at")?,
        payload: row.get("payload")?,
        metadata: object(row.get("metadata_json")?)?,
    };
    validate::artifact(&mut item)
        .map_err(|_| ArchiveError::Corrupt("closed artifact payload or metadata".into()))?;
    Ok(item)
}
pub(crate) fn exists(db: &Connection, table: &str, id: &str) -> Result<bool> {
    let sql = match table {
        "sessions" => "SELECT 1 FROM sessions WHERE id=?",
        "streams" => "SELECT 1 FROM streams WHERE id=?",
        _ => return Err(crate::error::invalid("registry table")),
    };
    Ok(db
        .query_row(sql, [id], |r| r.get::<_, i64>(0))
        .optional()?
        .is_some())
}
