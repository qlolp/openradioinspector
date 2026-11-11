//! Canonical outbox data and atomic storage operations; transport stays in guard/core.
use crate::{
    Archive, ArchiveError, CommittedBatch, MeasurementCursor, Mutation, Page, Result, rows,
    validate,
};
use rusqlite::{Connection, OptionalExtension, Row, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct NotificationRow {
    pub id: String,
    pub project_id: String,
    pub created_at: String,
    pub payload: Value,
    pub state: String,
    pub attempts: u32,
    pub next_at: Option<String>,
    pub error: Option<String>,
    pub lease_until: Option<String>,
    pub acknowledged_at: Option<String>,
}

/// Credentials deliberately implement neither Debug nor Serialize.
pub struct WebhookConfig {
    url: String,
    secret: String,
}
impl WebhookConfig {
    pub fn url(&self) -> &str {
        &self.url
    }
    pub fn secret(&self) -> &str {
        &self.secret
    }
}
pub struct ClaimedNotification {
    pub row: NotificationRow,
    pub config: WebhookConfig,
    body: Vec<u8>,
}
impl ClaimedNotification {
    /// Exact stored UTF-8 bytes, suitable for the existing Python HMAC contract.
    pub fn body(&self) -> &[u8] {
        &self.body
    }
}
pub enum NotificationClaim {
    Empty,
    Disabled { notification_id: String },
    Deliver(Box<ClaimedNotification>),
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum NotificationCompletion {
    Sent,
    Retry { next_at: String, error: String },
    Failed { error: String },
}

#[derive(Serialize)]
pub(crate) enum OutboxOperation {
    Claim {
        now: String,
        lease_until: String,
    },
    Finish {
        id: String,
        attempts: u32,
        lease_until: String,
        completion: NotificationCompletion,
    },
    Acknowledge {
        project: String,
        id: String,
        at: String,
    },
}
pub(crate) enum OutboxReply {
    Claim(NotificationClaim),
    Changed(bool),
}
struct StoredNotification {
    row: NotificationRow,
    body: Vec<u8>,
}

fn nonempty(value: &str, field: &str) -> Result<()> {
    if value.trim().is_empty() {
        return Err(crate::error::invalid(field));
    }
    Ok(())
}
fn optional_time(value: &Option<String>) -> Result<()> {
    if let Some(value) = value {
        validate::utc(value)?;
    }
    Ok(())
}
pub(crate) fn validate_notification(row: &mut NotificationRow) -> Result<()> {
    nonempty(&row.id, "notification ID")?;
    nonempty(&row.project_id, "notification project")?;
    if !["pending", "delivering", "disabled", "sent", "failed"].contains(&row.state.as_str())
        || !row.payload.is_object()
    {
        return Err(crate::error::invalid("notification shape"));
    }
    row.created_at = validate::utc(&row.created_at)?;
    for value in [
        &mut row.next_at,
        &mut row.lease_until,
        &mut row.acknowledged_at,
    ]
    .into_iter()
    .flatten()
    {
        *value = validate::utc(value)?;
    }
    // Strictly validate metadata, including reserved keys/finite floats, through
    // the same decoder as source rows. No historical payload text is rewritten.
    row.payload = crate::parse_json_value(&serde_json::to_vec(&row.payload)?, 32 * 1024 * 1024)?;
    Ok(())
}
pub(crate) fn validate_operation(operation: &mut OutboxOperation) -> Result<()> {
    match operation {
        OutboxOperation::Claim { now, lease_until } => {
            *now = validate::utc(now)?;
            *lease_until = validate::utc(lease_until)?;
            if lease_until <= now {
                return Err(crate::error::invalid("notification lease must be future"));
            }
        }
        OutboxOperation::Finish {
            id,
            lease_until,
            completion,
            ..
        } => {
            nonempty(id, "notification ID")?;
            // Preserve the exact claimed token for SQL CAS after validating it.
            validate::utc(lease_until)?;
            if let NotificationCompletion::Retry { next_at, .. } = completion {
                *next_at = validate::utc(next_at)?;
            }
        }
        OutboxOperation::Acknowledge { project, id, at } => {
            nonempty(project, "notification project")?;
            nonempty(id, "notification ID")?;
            *at = validate::utc(at)?;
        }
    }
    Ok(())
}

fn decode(row: &Row<'_>, maximum: usize) -> Result<StoredNotification> {
    let raw: String = row.get("payload")?;
    if raw.len() > maximum {
        return Err(ArchiveError::QueryBudget);
    }
    let value = NotificationRow {
        id: row.get("id")?,
        project_id: row.get("project_id")?,
        created_at: row.get("created_at")?,
        payload: rows::object(raw.clone())?,
        state: row.get("state")?,
        attempts: u32::try_from(row.get::<_, i64>("attempts")?)
            .map_err(|_| ArchiveError::Corrupt("notification attempts".into()))?,
        next_at: row.get("next_at")?,
        error: row.get("error")?,
        lease_until: row.get("lease_until")?,
        acknowledged_at: row.get("acknowledged_at")?,
    };
    validate::utc(&value.created_at)?;
    optional_time(&value.next_at)?;
    optional_time(&value.lease_until)?;
    optional_time(&value.acknowledged_at)?;
    if serde_json::to_vec(&value)?.len() > maximum {
        return Err(ArchiveError::QueryBudget);
    }
    Ok(StoredNotification {
        row: value,
        body: raw.into_bytes(),
    })
}
fn stored(db: &Connection, id: &str, maximum: usize) -> Result<Option<StoredNotification>> {
    let mut statement = db.prepare("SELECT * FROM monitoring_notifications WHERE id=?")?;
    let mut rows = statement.query([id])?;
    rows.next()?.map(|row| decode(row, maximum)).transpose()
}
fn config(db: &Connection, project: &str, maximum: usize) -> Result<Option<WebhookConfig>> {
    let item = db
        .query_row(
            "SELECT url,secret FROM monitoring_webhooks WHERE project_id=?",
            [project],
            |row| {
                Ok(WebhookConfig {
                    url: row.get(0)?,
                    secret: row.get(1)?,
                })
            },
        )
        .optional()?;
    if item
        .as_ref()
        .is_some_and(|value| value.url.len().saturating_add(value.secret.len()) > maximum)
    {
        return Err(ArchiveError::QueryBudget);
    }
    Ok(item)
}

pub(crate) fn insert(
    db: &Connection,
    value: &NotificationRow,
    maximum: usize,
) -> Result<NotificationRow> {
    let owner = db
        .query_row(
            "SELECT project_id FROM monitoring_notifications WHERE id=?",
            [&value.id],
            |row| row.get::<_, String>(0),
        )
        .optional()?;
    if owner.is_some_and(|owner| owner != value.project_id) {
        return Err(ArchiveError::Conflict);
    }
    db.execute("INSERT OR IGNORE INTO monitoring_notifications(id,project_id,created_at,payload,state,attempts,next_at,error,lease_until,acknowledged_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        params![value.id,value.project_id,value.created_at,serde_json::to_string(&value.payload)?,value.state,value.attempts,
            value.next_at,value.error,value.lease_until,value.acknowledged_at])?;
    Ok(stored(db, &value.id, maximum)?
        .ok_or(ArchiveError::NotFound)?
        .row)
}

pub(crate) fn apply_operation(
    db: &mut Connection,
    operation: OutboxOperation,
    maximum: usize,
) -> Result<OutboxReply> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let reply = match operation {
        OutboxOperation::Claim { now, lease_until } => {
            tx.execute("UPDATE monitoring_notifications SET state='failed',error='last_attempt_interrupted',lease_until=NULL WHERE state='delivering' AND attempts>=5 AND lease_until<=?", [&now])?;
            let source = tx.query_row("SELECT id,project_id,attempts FROM monitoring_notifications WHERE attempts<5 AND ((state='pending' AND next_at<=?) OR (state='delivering' AND lease_until<=?)) ORDER BY created_at,id LIMIT 1",
                params![now,now], |row| Ok((row.get::<_, String>(0)?,row.get::<_, String>(1)?,row.get::<_, i64>(2)?))).optional()?;
            if let Some((id, project, attempts)) = source {
                let payload = tx
                    .query_row(
                        "SELECT payload FROM monitoring_records WHERE kind='project' AND id=?",
                        [&project],
                        |row| row.get::<_, String>(0),
                    )
                    .optional()?
                    .ok_or(ArchiveError::NotFound)?;
                let enabled = rows::object(payload)?["webhook_enabled"].as_bool() == Some(true);
                let configured = if enabled {
                    config(&tx, &project, maximum)?
                } else {
                    None
                };
                if let Some(config) = configured {
                    let attempts = u32::try_from(attempts)
                        .ok()
                        .and_then(|value| value.checked_add(1))
                        .ok_or_else(|| ArchiveError::Corrupt("notification attempts".into()))?;
                    tx.execute("UPDATE monitoring_notifications SET state='delivering',attempts=?,lease_until=? WHERE id=?", params![attempts,lease_until,id])?;
                    let item = stored(&tx, &id, maximum)?.ok_or(ArchiveError::NotFound)?;
                    let bytes = serde_json::to_vec(&item.row)?
                        .len()
                        .checked_add(item.body.len())
                        .and_then(|bytes| bytes.checked_add(config.url.len()))
                        .and_then(|bytes| bytes.checked_add(config.secret.len()))
                        .ok_or(ArchiveError::QueryBudget)?;
                    if bytes > maximum {
                        return Err(ArchiveError::QueryBudget);
                    }
                    OutboxReply::Claim(NotificationClaim::Deliver(Box::new(ClaimedNotification {
                        row: item.row,
                        body: item.body,
                        config,
                    })))
                } else {
                    tx.execute(
                        "UPDATE monitoring_notifications SET state='disabled' WHERE id=?",
                        [&id],
                    )?;
                    OutboxReply::Claim(NotificationClaim::Disabled {
                        notification_id: id,
                    })
                }
            } else {
                OutboxReply::Claim(NotificationClaim::Empty)
            }
        }
        OutboxOperation::Finish {
            id,
            attempts,
            lease_until,
            completion,
        } => {
            let (state, next_at, error) = match completion {
                NotificationCompletion::Sent => ("sent", None, None),
                NotificationCompletion::Retry { next_at, error } => {
                    ("pending", Some(next_at), Some(error))
                }
                NotificationCompletion::Failed { error } => ("failed", None, Some(error)),
            };
            let changed = tx.execute("UPDATE monitoring_notifications SET state=?,next_at=?,error=?,lease_until=NULL WHERE id=? AND attempts=? AND state='delivering' AND lease_until=?", params![state,next_at,error,id,attempts,lease_until])?;
            OutboxReply::Changed(changed == 1)
        }
        OutboxOperation::Acknowledge { project, id, at } => {
            let changed = tx.execute("UPDATE monitoring_notifications SET acknowledged_at=COALESCE(acknowledged_at,?) WHERE id=? AND project_id=?", params![at,id,project])?;
            OutboxReply::Changed(changed == 1)
        }
    };
    tx.commit()?;
    Ok(reply)
}

impl Archive {
    pub async fn enqueue_notification(&self, value: NotificationRow) -> Result<CommittedBatch> {
        self.apply(vec![Mutation::EnqueueNotification(value)]).await
    }
    pub async fn notification(&self, id: &str) -> Result<Option<NotificationRow>> {
        let id = id.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read(move |db| Ok(stored(db, &id, maximum)?.map(|item| item.row)))
            .await
    }
    pub async fn notification_page(
        &self,
        project: &str,
        after: Option<MeasurementCursor>,
        limit: usize,
    ) -> Result<Page<NotificationRow>> {
        crate::validate_limit(limit, 100)?;
        if let Some(after) = &after {
            validate::utc(&after.started_at)?;
        }
        let project = project.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner.read(move |db| {
            let mut statement = db.prepare("SELECT * FROM monitoring_notifications WHERE project_id=? AND (created_at,id)<(?,?) ORDER BY created_at DESC,id DESC LIMIT ?")?;
            let (stamp, id) = after.map_or_else(|| ("9999".into(), "~".into()), |value| (value.started_at, value.id));
            let mut rows = statement.query(params![project,stamp,id,limit as i64+1])?;
            let mut items = Vec::new();
            let mut remaining = maximum;
            while let Some(row) = rows.next()? {
                let value = decode(row, maximum)?.row;
                remaining = remaining.checked_sub(serde_json::to_vec(&value)?.len()).ok_or(ArchiveError::QueryBudget)?;
                items.push(value);
            }
            let more = items.len() > limit;
            items.truncate(limit);
            let next_cursor = if more { items.last().map(|row| MeasurementCursor { started_at: row.created_at.clone(), id: row.id.clone() }) } else { None };
            Ok(Page { items, next_cursor })
        }).await
    }
    pub async fn webhook_config(&self, project: &str) -> Result<Option<WebhookConfig>> {
        let project = project.to_owned();
        let maximum = self.inner.config.max_response_bytes;
        self.inner
            .read_secret(move |db| config(db, &project, maximum))
            .await
    }
    pub async fn claim_notification(
        &self,
        now: &str,
        lease_until: &str,
    ) -> Result<NotificationClaim> {
        match self
            .inner
            .outbox(OutboxOperation::Claim {
                now: now.into(),
                lease_until: lease_until.into(),
            })
            .await?
        {
            OutboxReply::Claim(value) => Ok(value),
            _ => Err(ArchiveError::WorkerStopped),
        }
    }
    pub async fn finish_notification(
        &self,
        id: &str,
        attempts: u32,
        lease_until: &str,
        completion: NotificationCompletion,
    ) -> Result<bool> {
        match self
            .inner
            .outbox(OutboxOperation::Finish {
                id: id.into(),
                attempts,
                lease_until: lease_until.into(),
                completion,
            })
            .await?
        {
            OutboxReply::Changed(value) => Ok(value),
            _ => Err(ArchiveError::WorkerStopped),
        }
    }
    pub async fn acknowledge_notification(
        &self,
        project: &str,
        id: &str,
        at: &str,
    ) -> Result<bool> {
        match self
            .inner
            .outbox(OutboxOperation::Acknowledge {
                project: project.into(),
                id: id.into(),
                at: at.into(),
            })
            .await?
        {
            OutboxReply::Changed(value) => Ok(value),
            _ => Err(ArchiveError::WorkerStopped),
        }
    }
}
