//! Per-channel duty deliveries use the canonical writer, transaction and lease protocol.
//! Transport credentials never enter a mutation, a journal row, or a digest.
use crate::{
    Archive, ArchiveError, NotificationCompletion, NotificationRow, Result, outbox, validate,
};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

#[derive(Clone, Copy, Debug, Serialize, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum DutyChannel {
    Telegram,
    Webhook,
}
impl DutyChannel {
    pub const ALL: [Self; 2] = [Self::Telegram, Self::Webhook];
    pub fn name(self) -> &'static str {
        match self {
            Self::Telegram => "telegram",
            Self::Webhook => "webhook",
        }
    }
}
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DutyKind {
    NewUnknown,
    CoverageLost,
    RecordingLost,
    Impulse,
    Repeat,
    Test,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct DutyOccurrence {
    /// Stable source identity. An event has one immediate alert and can have many later observations.
    pub id: String,
    pub project_id: String,
    pub at: String,
    pub kind: DutyKind,
    pub event_id: Option<String>,
    pub frequency_hz: Option<u64>,
    pub delta: Option<f64>,
    pub delta_unit: Option<String>,
}
impl DutyOccurrence {
    pub(crate) fn validate(&mut self) -> Result<()> {
        self.at = validate::utc(&self.at)?;
        if self.id.is_empty()
            || self.id.len() > 512
            || self.project_id.is_empty()
            || self.project_id.len() > 256
            || self
                .event_id
                .as_ref()
                .is_some_and(|id| id.len() > 256 || id.chars().any(char::is_control))
            || self.delta.is_some_and(|d| !d.is_finite())
            || self.delta_unit.as_deref().is_some_and(|s| {
                !matches!(
                    s,
                    "dB" | "S-meter"
                        | "S-meter units"
                        | "raw_s_meter"
                        | "s_meter_raw"
                        | "s_meter_count"
                )
            })
        {
            return Err(crate::error::invalid("duty occurrence"));
        }
        Ok(())
    }
    fn payload(&self) -> Value {
        json!({"kind":self.kind,"project_id":self.project_id,"occurred_at":self.at,"event_id":self.event_id,
            "frequency_hz":self.frequency_hz,"delta":self.delta,"delta_unit":self.delta_unit})
    }
    pub fn delivery_id(&self, channel: DutyChannel) -> String {
        stable_id(
            &self.project_id,
            channel,
            &format!("{:?}/{}", self.kind, self.id),
        )
    }
}
fn stable_id(project: &str, channel: DutyChannel, source: &str) -> String {
    let mut hash = Sha256::new();
    hash.update(project.as_bytes());
    hash.update([0]);
    hash.update(channel.name().as_bytes());
    hash.update([0]);
    hash.update(source.as_bytes());
    hash.finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}
pub struct ClaimedDutyDelivery {
    pub row: NotificationRow,
    pub body: Vec<u8>,
    pub revision: String,
}
pub(crate) fn install(db: &Connection) -> Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS ori_duty_deliveries(
        id TEXT PRIMARY KEY,project_id TEXT NOT NULL,channel TEXT NOT NULL,revision TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL,
        state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT,error TEXT,lease_until TEXT,acknowledged_at TEXT);
        CREATE INDEX IF NOT EXISTS ori_duty_due ON ori_duty_deliveries(channel,state,next_at,lease_until,created_at,id);
        CREATE INDEX IF NOT EXISTS ori_duty_journal ON ori_duty_deliveries(project_id,created_at DESC,id DESC);
        CREATE TABLE IF NOT EXISTS ori_duty_cooldowns(project_id TEXT NOT NULL,channel TEXT NOT NULL,until_at TEXT NOT NULL,PRIMARY KEY(project_id,channel));
        CREATE TABLE IF NOT EXISTS ori_duty_digest_items(project_id TEXT NOT NULL,channel TEXT NOT NULL,hour TEXT NOT NULL,
        id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(project_id,channel,hour,id));
        CREATE INDEX IF NOT EXISTS ori_duty_hours ON ori_duty_digest_items(channel,hour,project_id);")?;
    Ok(())
}
pub(crate) fn make_portable(db: &Connection) -> Result<()> {
    if db.table_exists(None, "ori_duty_deliveries")? {
        db.execute_batch(
            "DELETE FROM ori_duty_digest_items; DELETE FROM ori_duty_cooldowns;
            UPDATE ori_duty_deliveries SET state='disabled',lease_until=NULL,next_at=NULL
            WHERE state IN ('pending','delivering'); UPDATE ori_duty_deliveries SET revision='';",
        )?;
    }
    Ok(())
}
fn enqueue(
    db: &Connection,
    id: &str,
    ch: DutyChannel,
    project: &str,
    revision: &str,
    at: &str,
    mut payload: Value,
) -> Result<()> {
    payload["notification_id"] = json!(id);
    db.execute("INSERT OR IGNORE INTO ori_duty_deliveries(id,channel,project_id,revision,created_at,payload,state,next_at)
        VALUES(?,?,?,?,?,?,'pending',?)",params![id,ch.name(),project,revision,at,serde_json::to_string(&payload)?,at])?;
    Ok(())
}
fn project_channels(db: &Connection, project: &str) -> Result<Value> {
    let raw: Option<String> = db
        .query_row(
            "SELECT json_extract(payload,'$.notification_channels') FROM monitoring_records
        WHERE kind='project' AND id=?",
            [project],
            |r| r.get(0),
        )
        .optional()?
        .flatten();
    raw.map(crate::rows::object)
        .transpose()
        .map(|v| v.unwrap_or(Value::Null))
}
pub(crate) fn record(
    db: &Connection,
    item: &DutyOccurrence,
    only: Option<DutyChannel>,
) -> Result<()> {
    let channels = project_channels(db, &item.project_id)?;
    for channel in DutyChannel::ALL {
        if only.is_some_and(|selected| selected != channel)
            || channels[channel.name()]["enabled"] != true
        {
            continue;
        }
        let id = item.delivery_id(channel);
        if matches!(item.kind, DutyKind::Impulse | DutyKind::Repeat) {
            db.execute("INSERT OR IGNORE INTO ori_duty_digest_items(project_id,channel,hour,id,kind,payload)
                VALUES(?,?,?,?,?,?)",params![item.project_id,channel.name(),&item.at[..13],id,
                    if item.kind==DutyKind::Impulse {"impulse"} else {"repeat"},serde_json::to_string(&item.payload())?])?;
        } else {
            enqueue(
                db,
                &id,
                channel,
                &item.project_id,
                channels[channel.name()]["revision"].as_str().unwrap_or(""),
                &item.at,
                item.payload(),
            )?;
        }
    }
    Ok(())
}
/// Convert only fixed fault categories into outgoing text. Legacy error strings can contain device details.
pub(crate) fn record_fault(db: &Connection, n: &NotificationRow) -> Result<()> {
    let kind = match n.payload["kind"].as_str() {
        Some("recording_lost" | "recording_error" | "audio_recording_failed") => {
            DutyKind::RecordingLost
        }
        Some(
            "coverage_error" | "data_gap" | "device_unavailable" | "restart_error" | "sleep_gap",
        ) => DutyKind::CoverageLost,
        _ => return Ok(()),
    };
    record(
        db,
        &DutyOccurrence {
            id: n.id.clone(),
            project_id: n.project_id.clone(),
            at: n.created_at.clone(),
            kind,
            event_id: n.payload["event_id"].as_str().map(str::to_owned),
            frequency_hz: None,
            delta: None,
            delta_unit: None,
        },
        None,
    )
}
fn make_digest(db: &Connection, ch: DutyChannel, now: &str) -> Result<()> {
    let group = db
        .query_row(
            "SELECT project_id,hour FROM ori_duty_digest_items WHERE channel=? AND hour<?
        ORDER BY hour,project_id LIMIT 1",
            params![ch.name(), &now[..13]],
            |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)),
        )
        .optional()?;
    if let Some((project, hour)) = group {
        let (impulses, repeats): (i64, i64) = db.query_row(
            "SELECT sum(kind='impulse'),sum(kind='repeat') FROM ori_duty_digest_items
            WHERE project_id=? AND channel=? AND hour=?",
            params![project, ch.name(), hour],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let first: String = db.query_row(
            "SELECT payload FROM ori_duty_digest_items WHERE project_id=? AND channel=? AND hour=?
            ORDER BY id LIMIT 1",
            params![project, ch.name(), hour],
            |r| r.get(0),
        )?;
        let mut body = crate::rows::object(first)?;
        body["kind"] = json!("hourly_digest");
        body["hour_start"] = json!(format!("{hour}:00:00.000000Z"));
        body["impulses"] = json!(impulses);
        body["repeats"] = json!(repeats);
        enqueue(
            db,
            &stable_id(&project, ch, &format!("digest/{hour}")),
            ch,
            &project,
            project_channels(db, &project)?[ch.name()]["revision"]
                .as_str()
                .unwrap_or(""),
            now,
            body,
        )?;
        db.execute(
            "DELETE FROM ori_duty_digest_items WHERE project_id=? AND channel=? AND hour=?",
            params![project, ch.name(), hour],
        )?;
    }
    Ok(())
}
pub(crate) fn claim(
    db: &Connection,
    ch: DutyChannel,
    now: &str,
    lease: &str,
    max: usize,
) -> Result<Option<ClaimedDutyDelivery>> {
    make_digest(db, ch, now)?;
    db.execute("UPDATE ori_duty_deliveries SET state='failed',error='last_attempt_interrupted',lease_until=NULL
        WHERE channel=? AND state='delivering' AND attempts>=5 AND lease_until<=?",params![ch.name(),now])?;
    let item=db.query_row("SELECT id,project_id,revision FROM ori_duty_deliveries d WHERE channel=? AND attempts<5
        AND NOT EXISTS (SELECT 1 FROM ori_duty_cooldowns c WHERE c.project_id=d.project_id AND c.channel=d.channel AND c.until_at>?)
        AND ((state='pending' AND next_at<=?) OR (state='delivering' AND lease_until<=?))
        ORDER BY created_at,id LIMIT 1",params![ch.name(),now,now,now],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?))).optional()?;
    let Some((id, project, revision)) = item else {
        return Ok(None);
    };
    if project_channels(db, &project)?[ch.name()]["enabled"] != true {
        db.execute("UPDATE ori_duty_deliveries SET state='disabled',lease_until=NULL,next_at=NULL WHERE id=?",[id])?;
        return Ok(None);
    }
    db.execute("UPDATE ori_duty_deliveries SET attempts=attempts+1,state='delivering',lease_until=? WHERE id=?",params![lease,id])?;
    let mut statement = db.prepare("SELECT * FROM ori_duty_deliveries WHERE id=?")?;
    let mut rows = statement.query([id])?;
    let stored = outbox::decode(rows.next()?.ok_or(ArchiveError::NotFound)?, max)?;
    Ok(Some(ClaimedDutyDelivery {
        row: stored.row,
        body: stored.body,
        revision,
    }))
}
pub(crate) fn disable(db: &Connection, project: &str, channel: DutyChannel) -> Result<()> {
    db.execute("UPDATE ori_duty_deliveries SET state='disabled',lease_until=NULL,next_at=NULL WHERE project_id=? AND channel=?
        AND state IN ('pending','delivering')",params![project,channel.name()])?;
    db.execute(
        "DELETE FROM ori_duty_cooldowns WHERE project_id=? AND channel=?",
        params![project, channel.name()],
    )?;
    db.execute(
        "DELETE FROM ori_duty_digest_items WHERE project_id=? AND channel=?",
        params![project, channel.name()],
    )?;
    Ok(())
}
impl Archive {
    pub async fn claim_duty_delivery(
        &self,
        channel: DutyChannel,
        now: &str,
        lease_until: &str,
    ) -> Result<Option<ClaimedDutyDelivery>> {
        match self
            .inner
            .outbox(outbox::OutboxOperation::DutyClaim {
                channel,
                now: now.into(),
                lease_until: lease_until.into(),
            })
            .await?
        {
            outbox::OutboxReply::DutyClaim(value) => Ok(value.map(|claim| *claim)),
            _ => Err(ArchiveError::WorkerStopped),
        }
    }
    pub async fn finish_duty_delivery(
        &self,
        row: &NotificationRow,
        completion: NotificationCompletion,
    ) -> Result<bool> {
        match self
            .inner
            .outbox(outbox::OutboxOperation::Finish {
                duty: true,
                id: row.id.clone(),
                attempts: row.attempts,
                lease_until: row
                    .lease_until
                    .clone()
                    .ok_or_else(|| crate::error::invalid("duty lease"))?,
                completion,
            })
            .await?
        {
            outbox::OutboxReply::Changed(v) => Ok(v),
            _ => Err(ArchiveError::WorkerStopped),
        }
    }
    pub async fn duty_delivery_journal(&self, project: &str, limit: usize) -> Result<Vec<Value>> {
        if !(1..=100).contains(&limit) {
            return Err(crate::error::invalid("duty journal limit"));
        }
        let project = project.to_owned();
        self.inner.read(move|db| {
            let mut stmt=db.prepare("SELECT id,channel,json_extract(payload,'$.kind'),state,attempts,next_at,error,created_at
                FROM ori_duty_deliveries WHERE project_id=? ORDER BY created_at DESC,id DESC LIMIT ?")?;
            let rows=stmt.query_map(params![project,limit as i64],|r| Ok(json!({"id":r.get::<_,String>(0)?,
                "channel":r.get::<_,String>(1)?,"kind":r.get::<_,String>(2)?,"state":r.get::<_,String>(3)?,
                "attempts":r.get::<_,u32>(4)?,"next_at":r.get::<_,Option<String>>(5)?,"error":r.get::<_,Option<String>>(6)?,
                "created_at":r.get::<_,String>(7)?})))?;
            Ok(rows.collect::<std::result::Result<Vec<_>,_>>()?)
        }).await
    }
}
