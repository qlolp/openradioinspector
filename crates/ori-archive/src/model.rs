use crate::{Result, error::invalid};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{path::PathBuf, sync::Arc, time::Duration};

#[derive(Clone, Debug)]
pub struct ArchiveConfig {
    pub path: PathBuf,
    pub max_database_bytes: Option<u64>,
    pub writer_queue_capacity: usize,
    pub writer_queue_bytes: usize,
    pub readers: usize,
    pub reader_queue_capacity: usize,
    pub max_batch_bytes: usize,
    pub max_response_bytes: usize,
    pub read_timeout: Duration,
    pub busy_timeout: Duration,
    pub backup_timeout: Duration,
}
impl ArchiveConfig {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            path: path.into(),
            max_database_bytes: None,
            writer_queue_capacity: 64,
            writer_queue_bytes: 32 * 1024 * 1024,
            readers: 2,
            reader_queue_capacity: 32,
            max_batch_bytes: 16 * 1024 * 1024,
            max_response_bytes: 16 * 1024 * 1024,
            read_timeout: Duration::from_secs(2),
            busy_timeout: Duration::from_secs(5),
            backup_timeout: Duration::from_secs(30),
        }
    }
    pub(crate) fn validate(&self) -> Result<()> {
        if self.path.as_os_str().is_empty() || self.path == std::path::Path::new(":memory:") {
            return Err(invalid("a persistent archive path is required"));
        }
        if self.max_database_bytes == Some(0)
            || !(1..=2).contains(&self.readers)
            || !(1..=1024).contains(&self.writer_queue_capacity)
            || !(1..=1024).contains(&self.reader_queue_capacity)
            || self.writer_queue_bytes == 0
            || self.max_batch_bytes == 0
            || self.max_response_bytes == 0
            || self.read_timeout.is_zero()
            || self.read_timeout > Duration::from_secs(5)
            || self.backup_timeout.is_zero()
            || self.busy_timeout > Duration::from_secs(10)
        {
            return Err(invalid(
                "archive queue, reader, capacity or deadline limits",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EventDraft {
    pub event_type: String,
    pub occurred_at: String,
    pub session_id: Option<String>,
    pub payload: Value,
}
impl EventDraft {
    pub fn now(event_type: impl Into<String>, session_id: Option<String>, payload: Value) -> Self {
        Self {
            event_type: event_type.into(),
            occurred_at: chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, true),
            session_id,
            payload,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Artifact {
    pub id: String,
    pub session_id: String,
    pub stream_id: String,
    pub schema_version: i64,
    pub kind: String,
    pub mime_type: String,
    pub filename: String,
    pub sha256: String,
    pub created_at: String,
    pub payload: Vec<u8>,
    pub metadata: Value,
}

/// Fixed operations only. A vector is one all-or-nothing SQLite transaction.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum Mutation {
    ResetRuntimeStreamStates,
    StopSession {
        value: Value,
        at: String,
    },
    CreateSession(Value),
    SaveSession {
        value: Value,
        event: Option<EventDraft>,
    },
    Sweeps(Vec<Value>),
    Observations(Vec<Value>),
    Event(EventDraft),
    Monitoring {
        kind: String,
        value: Value,
    },
    Candidate {
        value: Value,
        event: Option<EventDraft>,
    },
    Investigation {
        session_id: String,
        value: Value,
        candidate: Value,
        event: Option<EventDraft>,
    },
    MeasurementResult {
        value: Value,
        event: Option<EventDraft>,
    },
    Reference {
        value: Value,
        event: EventDraft,
    },
    Artifact(Artifact),
    Webhook {
        project_id: String,
        url: String,
        secret: String,
    },
    Notification(Value),
}

#[derive(Debug, Serialize)]
pub struct CommittedBatch {
    pub items: Vec<Value>,
    pub events: Vec<Value>,
    pub ingestion_boundary: Option<u64>,
    pub first_sequence: Option<u64>,
    pub last_sequence: Option<u64>,
    pub events_complete: bool,
    #[serde(skip)]
    pub(crate) reservation: Option<Arc<crate::actor::ByteReservation>>,
    #[serde(skip)]
    pub(crate) captured_event_bytes: usize,
    #[serde(skip)]
    pub(crate) capture_budget: usize,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct CommitNotice {
    pub first_sequence: u64,
    pub last_sequence: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct MeasurementCursor {
    pub started_at: String,
    pub id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Page<T> {
    pub items: Vec<T>,
    pub next_cursor: Option<MeasurementCursor>,
}
#[derive(Clone, Debug)]
pub struct SweepQuery {
    pub session_id: Option<String>,
    pub stream_id: Option<String>,
    pub project_id: Option<String>,
    pub event_id: Option<String>,
    pub start: Option<String>,
    pub end: Option<String>,
    pub min_hz: Option<i64>,
    pub max_hz: Option<i64>,
    pub after: Option<MeasurementCursor>,
    pub limit: usize,
}
impl SweepQuery {
    pub fn session(id: impl Into<String>) -> Self {
        Self {
            session_id: Some(id.into()),
            stream_id: None,
            project_id: None,
            event_id: None,
            start: None,
            end: None,
            min_hz: None,
            max_hz: None,
            after: None,
            limit: 32,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SweepContext {
    pub anchor: Value,
    pub before: Vec<Value>,
    pub after: Vec<Value>,
}
#[derive(Clone, Debug)]
pub struct MonitoringQuery {
    pub project_id: Option<String>,
    pub after: String,
    pub state: Option<String>,
    pub limit: usize,
    pub recent: bool,
}
impl Default for MonitoringQuery {
    fn default() -> Self {
        Self {
            project_id: None,
            after: String::new(),
            state: None,
            limit: 200,
            recent: false,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct QueueStatus {
    pub queued_operations: usize,
    pub retained_bytes: usize,
    pub writer_capacity: usize,
    pub writer_byte_limit: usize,
    pub closing: bool,
}
